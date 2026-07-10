# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Verified bundled Skills and explicit installation-time trust decisions."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from heartwood.skills import (
    LocalSkillVerifier,
    SkillManifest,
    SkillVerificationError,
    build_skill_approval_record,
)


class SkillSettingsError(ValueError):
    """Raised when a Skill cannot be inspected, installed, or removed safely."""


@dataclass(frozen=True, slots=True)
class SkillSummary:
    """API-safe Skill metadata for researcher interfaces."""

    name: str
    skill_id: str
    description: str
    trust_tier: str
    source: str
    approval_summary: str
    declared_tools: tuple[str, ...]
    requires_network: bool

    @classmethod
    def from_manifest(cls, manifest: SkillManifest, *, source: str) -> SkillSummary:
        """Build a summary from a verified local manifest."""
        return cls(
            name=manifest.name,
            skill_id=manifest.skill_id,
            description=manifest.description,
            trust_tier=manifest.metadata.trust_tier,
            source=source,
            approval_summary=manifest.approval_summary,
            declared_tools=manifest.declared_tools,
            requires_network=manifest.metadata.requires_network,
        )

    def safe_dict(self) -> dict[str, object]:
        """Return JSON-compatible metadata."""
        return {
            "name": self.name,
            "skill_id": self.skill_id,
            "description": self.description,
            "trust_tier": self.trust_tier,
            "source": self.source,
            "approval_summary": self.approval_summary,
            "declared_tools": list(self.declared_tools),
            "requires_network": self.requires_network,
        }


class SkillManager:
    """List bundled Skills and manage explicitly approved local extensions."""

    def __init__(self, *, bundled_dir: Path, installed_dir: Path, audit_path: Path) -> None:
        self.bundled_dir = bundled_dir.resolve()
        self.installed_dir = installed_dir.resolve()
        self.audit_path = audit_path.resolve()

    def summaries(self) -> tuple[SkillSummary, ...]:
        """Return all verified bundled and installed Skills."""
        return (
            *self._summaries_in(self.bundled_dir, source="bundled", require_verified=True),
            *self._summaries_in(self.installed_dir, source="installed", require_verified=False),
        )

    def inspect(self, source: Path) -> SkillSummary:
        """Verify and summarize one mounted Skill source without installing it."""
        manifest = self._source_manifest(source)
        return SkillSummary.from_manifest(manifest, source="candidate")

    def install(
        self,
        source: Path,
        *,
        approved: bool,
        actor_id: str = "human",
    ) -> SkillSummary:
        """Install one verified source after recording an explicit trust decision."""
        manifest = self._source_manifest(source)
        if any(
            summary.name == manifest.name
            for summary in self._summaries_in(
                self.bundled_dir,
                source="bundled",
                require_verified=True,
            )
        ):
            msg = f"bundled Skill cannot be replaced by an extension: {manifest.name}"
            raise SkillSettingsError(msg)
        if not approved:
            self._record_decision(manifest, approved=False, actor_id=actor_id)
            msg = f"installation approval is required: {manifest.approval_summary}"
            raise SkillSettingsError(msg)
        _validate_install_name(manifest.name)
        destination = (self.installed_dir / manifest.name).resolve()
        if destination.parent != self.installed_dir:
            msg = "installed Skill destination escapes persistent Skill storage"
            raise SkillSettingsError(msg)
        if destination.exists():
            msg = f"installed Skill already exists: {manifest.name}"
            raise SkillSettingsError(msg)
        source_root = source.resolve()
        if self.installed_dir == source_root or self.installed_dir in source_root.parents:
            msg = "Skill source cannot be inside persistent Skill storage"
            raise SkillSettingsError(msg)
        _reject_symlinks(source)
        self._record_decision(manifest, approved=True, actor_id=actor_id)
        self.installed_dir.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=f".{manifest.name}.", dir=self.installed_dir))
        try:
            shutil.copytree(source_root, temporary, dirs_exist_ok=True)
            installed = LocalSkillVerifier(
                self.installed_dir,
                require_verified_tier=False,
            ).load_manifest(temporary)
            temporary.replace(destination)
        except (OSError, SkillVerificationError) as error:
            msg = f"unable to install Skill {manifest.name}: {error}"
            raise SkillSettingsError(msg) from error
        finally:
            shutil.rmtree(temporary, ignore_errors=True)
        return SkillSummary.from_manifest(installed, source="installed")

    def remove(self, name: str) -> None:
        """Remove one installed extension without touching bundled Skills."""
        _validate_install_name(name)
        destination = (self.installed_dir / name).resolve()
        if destination.parent != self.installed_dir or not destination.is_dir():
            msg = f"installed Skill does not exist: {name}"
            raise SkillSettingsError(msg)
        shutil.rmtree(destination)

    def _source_manifest(self, source: Path) -> SkillManifest:
        _reject_symlinks(source)
        source_root = source.resolve()
        if not source_root.is_dir():
            msg = f"Skill source directory does not exist: {source}"
            raise SkillSettingsError(msg)
        try:
            return LocalSkillVerifier(
                source_root.parent,
                require_verified_tier=False,
            ).load_manifest(source_root)
        except SkillVerificationError as error:
            raise SkillSettingsError(str(error)) from error

    def _summaries_in(
        self,
        root: Path,
        *,
        source: str,
        require_verified: bool,
    ) -> tuple[SkillSummary, ...]:
        if not root.is_dir():
            return ()
        verifier = LocalSkillVerifier(root, require_verified_tier=require_verified)
        summaries: list[SkillSummary] = []
        for path in sorted(root.iterdir()):
            if not path.is_dir():
                continue
            try:
                manifest = verifier.load_manifest(path)
            except SkillVerificationError as error:
                msg = f"invalid {source} Skill {path.name}: {error}"
                raise SkillSettingsError(msg) from error
            summaries.append(SkillSummary.from_manifest(manifest, source=source))
        return tuple(summaries)

    def _record_decision(
        self,
        manifest: SkillManifest,
        *,
        approved: bool,
        actor_id: str,
    ) -> None:
        record = build_skill_approval_record(
            manifest,
            session_id="skill-installation",
            actor_id=actor_id,
            occurred_at=datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            decision="approved" if approved else "denied",
        )
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            self.audit_path,
            os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as file:
            file.write(json.dumps(record.model_dump(mode="json"), sort_keys=True) + "\n")


def _validate_install_name(name: str) -> None:
    if not name or not name.replace("-", "").replace("_", "").isalnum():
        msg = "Skill name must contain only letters, numbers, hyphens, or underscores"
        raise SkillSettingsError(msg)


def _reject_symlinks(root: Path) -> None:
    if root.is_symlink() or any(path.is_symlink() for path in root.rglob("*")):
        msg = "Skill sources containing symbolic links cannot be installed"
        raise SkillSettingsError(msg)
