# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Shared projection and lifecycle for trusted Agent Skill artifacts."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal, Protocol

from heartwood.audit import AuditIntegrityError, AuditLog
from heartwood.persistence import NativeLockUnavailableError, native_file_lock
from heartwood.schemas import JsonValue
from heartwood.skills import (
    CatalogEntry,
    InstalledSkillRecord,
    LocalSkillVerifier,
    SkillArtifactStore,
    SkillCatalogClient,
    SkillCatalogError,
    SkillCatalogSnapshot,
    SkillManifest,
    SkillSourceProfile,
    SkillSourceRegistry,
    SkillStoreError,
    SkillVerificationError,
)


class SkillSettingsError(ValueError):
    """Raised when a Skill cannot be discovered, verified, or activated safely."""


_DATA_ACCESS_SUMMARIES: Final[
    dict[Literal["none", "reads-phi", "writes-outside-boundary"], str]
] = {
    "none": "No row-level PHI access declared",
    "reads-phi": "Reads potentially identifiable row-level data",
    "writes-outside-boundary": "Declares writes outside the project boundary",
}


class _CatalogClient(Protocol):
    def refresh(self) -> SkillCatalogSnapshot:
        raise NotImplementedError

    def download(self, entry: CatalogEntry) -> Path:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class SkillSummary:
    """API-safe Skill metadata shared by every researcher interface."""

    name: str
    skill_id: str
    version: str
    description: str
    source: Literal["bundled", "catalog", "installed", "local-candidate"]
    source_id: str
    review: Literal["repository-reviewed", "local-unreviewed"]
    status: Literal["available", "active", "revoked", "unsupported"]
    approval_summary: str
    declared_tools: tuple[str, ...]
    requires_network: bool
    phi_risk: Literal["none", "reads-phi", "writes-outside-boundary"]
    data_access_summary: str
    dataset_types: tuple[str, ...]
    controlled_data_ready: bool
    tree_sha256: str
    source_revision: str | None = None
    archive_size: int | None = None
    revocation_reason: str | None = None
    compatibility_reason: str | None = None

    @property
    def installable(self) -> bool:
        """Return whether this projection can be activated from a signed source."""
        return self.source == "catalog" and self.status == "available"

    @classmethod
    def from_manifest(
        cls,
        manifest: SkillManifest,
        *,
        source: Literal["bundled", "local-candidate"],
    ) -> SkillSummary:
        """Build a projection from a locally verified Agent Skill tree."""
        return cls(
            name=manifest.name,
            skill_id=manifest.skill_id,
            version=manifest.version,
            description=manifest.description,
            source=source,
            source_id="heartwood" if source == "bundled" else "local",
            review=manifest.review,
            status="active" if source == "bundled" else "available",
            approval_summary=manifest.approval_summary,
            declared_tools=manifest.declared_tools,
            requires_network=manifest.requires_network,
            phi_risk=manifest.policy.phi_risk,
            data_access_summary=_DATA_ACCESS_SUMMARIES[manifest.policy.phi_risk],
            dataset_types=manifest.policy.dataset_types,
            controlled_data_ready=False,
            tree_sha256=manifest.tree_sha256,
        )

    @classmethod
    def from_catalog(
        cls,
        entry: CatalogEntry,
        *,
        source_id: str,
        platform_id: str,
        controlled_data_ready: bool,
    ) -> SkillSummary:
        """Build a projection from a TUF-verified catalog entry."""
        compatible = "generic" in entry.policy.platforms or platform_id in entry.policy.platforms
        if entry.revoked:
            status: Literal["available", "revoked", "unsupported"] = "revoked"
        elif compatible:
            status = "available"
        else:
            status = "unsupported"
        return cls(
            name=entry.name,
            skill_id=entry.policy.skill_id,
            version=entry.policy.version,
            description=entry.description,
            source="catalog",
            source_id=source_id,
            review=entry.review,
            status=status,
            approval_summary=entry.policy.approval_summary,
            declared_tools=entry.allowed_tools,
            requires_network=entry.policy.requires_network,
            phi_risk=entry.policy.phi_risk,
            data_access_summary=_DATA_ACCESS_SUMMARIES[entry.policy.phi_risk],
            dataset_types=entry.policy.dataset_types,
            controlled_data_ready=controlled_data_ready,
            tree_sha256=entry.tree_sha256,
            source_revision=entry.source_revision,
            archive_size=entry.archive_size,
            revocation_reason=entry.revocation_reason,
            compatibility_reason=(
                None if compatible else f"Not supported on the {platform_id} platform"
            ),
        )

    @classmethod
    def from_installed(
        cls,
        record: InstalledSkillRecord,
        manifest: SkillManifest,
    ) -> SkillSummary:
        """Build a projection from an activated content-addressed artifact."""
        return cls(
            name=record.name,
            skill_id=record.skill_id,
            version=record.version,
            description=manifest.description,
            source="installed",
            source_id=record.source_id,
            review=record.review,
            status=record.status,
            approval_summary=manifest.approval_summary,
            declared_tools=manifest.declared_tools,
            requires_network=manifest.requires_network,
            phi_risk=manifest.policy.phi_risk,
            data_access_summary=_DATA_ACCESS_SUMMARIES[manifest.policy.phi_risk],
            dataset_types=manifest.policy.dataset_types,
            controlled_data_ready=record.controlled_data_ready,
            tree_sha256=record.tree_sha256,
            source_revision=record.source_revision,
            revocation_reason=record.revocation_reason,
        )

    def safe_dict(self) -> dict[str, object]:
        """Return the stable JSON projection used by CLI, browser, and notebook clients."""
        return {
            "name": self.name,
            "skill_id": self.skill_id,
            "version": self.version,
            "description": self.description,
            "source": self.source,
            "source_id": self.source_id,
            "review": self.review,
            "status": self.status,
            "approval_summary": self.approval_summary,
            "declared_tools": list(self.declared_tools),
            "requires_network": self.requires_network,
            "phi_risk": self.phi_risk,
            "data_access_summary": self.data_access_summary,
            "dataset_types": list(self.dataset_types),
            "controlled_data_ready": self.controlled_data_ready,
            "tree_sha256": self.tree_sha256,
            "source_revision": self.source_revision,
            "archive_size": self.archive_size,
            "revocation_reason": self.revocation_reason,
            "compatibility_reason": self.compatibility_reason,
            "installable": self.installable,
        }


class SkillManager:
    """Own trusted source refresh, content-addressed activation, and audit records."""

    def __init__(
        self,
        *,
        bundled_dir: Path,
        store: SkillArtifactStore,
        source_registry: SkillSourceRegistry,
        cache_dir: Path,
        audit_path: Path,
        platform_id: str,
        client_factory: Callable[[SkillSourceProfile, Path], _CatalogClient] | None = None,
    ) -> None:
        self.bundled_dir = bundled_dir.resolve()
        self.store = store
        self.lifecycle_lock_path = store.root / ".lifecycle.lock"
        self.source_registry = source_registry
        self.audit_path = audit_path.resolve()
        self.platform_id = platform_id
        factory = SkillCatalogClient if client_factory is None else client_factory
        profiles = source_registry.enabled_sources()
        self._profiles = {profile.source_id: profile for profile in profiles}
        self._clients: dict[str, _CatalogClient] = {
            profile.source_id: factory(profile, cache_dir) for profile in profiles
        }
        self._snapshots: dict[str, SkillCatalogSnapshot] = {}

    def summaries(self) -> tuple[SkillSummary, ...]:
        """Return one deterministic projection of bundled, installed, and catalog Skills."""
        bundled = self._bundled_summaries()
        installed = self._installed_summaries()
        active_names = {summary.name for summary in (*bundled, *installed)}
        active_ids = {summary.skill_id for summary in (*bundled, *installed)}
        catalog = tuple(
            summary
            for source_id in sorted(self._snapshots)
            for entry in self._snapshots[source_id].entries
            if (
                (summary := self._catalog_summary(entry, source_id=source_id)).name
                not in active_names
                and summary.skill_id not in active_ids
            )
        )
        return tuple(sorted((*bundled, *installed, *catalog), key=_summary_order))

    def refresh(self, source_id: str | None = None) -> tuple[SkillSummary, ...]:
        """Refresh configured TUF sources and apply signed revocation state."""
        with self._lifecycle_lock():
            return self._refresh(source_id)

    def _refresh(self, source_id: str | None = None) -> tuple[SkillSummary, ...]:
        """Refresh source state while holding the project Skill lifecycle lock."""
        source_ids = (source_id,) if source_id is not None else tuple(sorted(self._clients))
        if source_id is not None and source_id not in self._clients:
            raise SkillSettingsError(f"Skill source is not configured: {source_id}")
        snapshots: list[SkillCatalogSnapshot] = []
        try:
            for current_source_id in source_ids:
                snapshot = self._clients[current_source_id].refresh()
                if snapshot.source_id != current_source_id:
                    raise SkillSettingsError(
                        "Skill source returned metadata for a different source identifier"
                    )
                snapshots.append(snapshot)
        except SkillCatalogError as error:
            raise SkillSettingsError(str(error)) from error
        for snapshot in snapshots:
            self.store.apply_catalog_snapshot(snapshot)
            self._snapshots[snapshot.source_id] = snapshot
        return self.summaries()

    def inspect_catalog(self, name: str, *, source_id: str | None = None) -> SkillSummary:
        """Refresh and summarize one signed catalog entry without downloading its archive."""
        with self._lifecycle_lock():
            return self._inspect_catalog(name, source_id=source_id)

    def _inspect_catalog(self, name: str, *, source_id: str | None = None) -> SkillSummary:
        """Inspect one catalog entry while holding the Skill lifecycle lock."""
        source_id = self._resolve_source_id(source_id)
        snapshot = self._refresh_source(source_id)
        try:
            entry = snapshot.entry(name)
        except SkillCatalogError as error:
            raise SkillSettingsError(str(error)) from error
        return self._catalog_summary(entry, source_id=source_id)

    def install_catalog(
        self,
        name: str,
        *,
        source_id: str | None,
        expected_tree_sha256: str,
        approved: bool,
        actor_id: str = "human",
    ) -> SkillSummary:
        """Refresh, verify, download, and atomically activate one signed Skill."""
        with self._lifecycle_lock():
            return self._install_catalog(
                name,
                source_id=source_id,
                expected_tree_sha256=expected_tree_sha256,
                approved=approved,
                actor_id=actor_id,
            )

    def _install_catalog(
        self,
        name: str,
        *,
        source_id: str | None,
        expected_tree_sha256: str,
        approved: bool,
        actor_id: str,
    ) -> SkillSummary:
        """Install one catalog Skill while holding the lifecycle lock."""
        source_id = self._resolve_source_id(source_id)
        snapshot = self._refresh_source(source_id)
        try:
            entry = snapshot.entry(name)
        except SkillCatalogError as error:
            raise SkillSettingsError(str(error)) from error
        summary = self._catalog_summary(entry, source_id=source_id)
        self._assert_identity_available(summary)
        if entry.revoked:
            raise SkillSettingsError(f"Skill has been revoked: {entry.name}")
        if summary.status == "unsupported":
            raise SkillSettingsError(summary.compatibility_reason or "Skill is not supported")
        if entry.tree_sha256 != expected_tree_sha256:
            raise SkillSettingsError(
                "Skill content changed after review; inspect the current signed revision again"
            )
        if not approved:
            self._record_decision(summary, approved=False, actor_id=actor_id)
            raise SkillSettingsError("Skill installation requires explicit approval")
        self._record_decision(summary, approved=True, actor_id=actor_id)
        try:
            archive = self._clients[source_id].download(entry)
            record = self.store.install_catalog(
                entry,
                archive,
                source_id=source_id,
                controlled_data_ready=self._profiles[source_id].controlled_data_ready(entry),
            )
            manifest = self.store.manifest(record)
        except (SkillCatalogError, SkillStoreError) as error:
            self._record_installation_result(summary, activated=False, actor_id=actor_id)
            raise SkillSettingsError(str(error)) from error
        installed = SkillSummary.from_installed(record, manifest)
        self._record_installation_result(installed, activated=True, actor_id=actor_id)
        return installed

    def inspect_local(self, source: Path) -> SkillSummary:
        """Inspect an advanced local source without treating it as repository-reviewed."""
        try:
            manifest = LocalSkillVerifier(source.resolve().parent).load_manifest(source)
        except SkillVerificationError as error:
            raise SkillSettingsError(str(error)) from error
        return SkillSummary.from_manifest(manifest, source="local-candidate")

    def install_local(
        self,
        source: Path,
        *,
        expected_tree_sha256: str,
        approved: bool,
        actor_id: str = "human",
    ) -> SkillSummary:
        """Activate an explicitly approved local, unreviewed Agent Skill tree."""
        with self._lifecycle_lock():
            return self._install_local(
                source,
                expected_tree_sha256=expected_tree_sha256,
                approved=approved,
                actor_id=actor_id,
            )

    def _install_local(
        self,
        source: Path,
        *,
        expected_tree_sha256: str,
        approved: bool,
        actor_id: str,
    ) -> SkillSummary:
        """Install one local Skill while holding the lifecycle lock."""
        summary = self.inspect_local(source)
        self._assert_identity_available(summary)
        if summary.tree_sha256 != expected_tree_sha256:
            raise SkillSettingsError("Local Skill changed after review; inspect it again")
        if not approved:
            self._record_decision(summary, approved=False, actor_id=actor_id)
            raise SkillSettingsError("Local Skill installation requires explicit approval")
        self._record_decision(summary, approved=True, actor_id=actor_id)
        try:
            record = self.store.install_local(source)
            manifest = self.store.manifest(record)
        except SkillStoreError as error:
            self._record_installation_result(summary, activated=False, actor_id=actor_id)
            raise SkillSettingsError(str(error)) from error
        installed = SkillSummary.from_installed(record, manifest)
        self._record_installation_result(installed, activated=True, actor_id=actor_id)
        return installed

    def remove(self, name: str) -> None:
        """Deactivate one installed Skill while retaining its immutable artifact."""
        with self._lifecycle_lock():
            self._remove(name)

    def _remove(self, name: str) -> None:
        """Deactivate one installed Skill while holding the lifecycle lock."""
        try:
            removed = self.store.remove(name)
        except SkillStoreError as error:
            raise SkillSettingsError(str(error)) from error
        summary = SkillSummary.from_installed(removed, self.store.manifest(removed))
        self._record_removal(summary)

    def active_skill_roots(self) -> tuple[Path, ...]:
        """Return verified installed roots consumable by OpenHands."""
        with self._lifecycle_lock():
            return self._active_skill_roots()

    def _active_skill_roots(self) -> tuple[Path, ...]:
        """Return active roots while holding the lifecycle lock."""
        try:
            records = self.store.records()
            active_sources = sorted(
                {
                    record.source_id
                    for record in records
                    if record.source_kind == "catalog" and record.status == "active"
                }
            )
            for source_id in active_sources:
                if source_id not in self._clients:
                    raise SkillSettingsError(
                        f"Installed Skill source is no longer configured: {source_id}"
                    )
                self._refresh_source(source_id)
            self._verify_active_installation_audit(records)
            return self.store.active_skill_roots()
        except SkillStoreError as error:
            raise SkillSettingsError(str(error)) from error

    def _catalog_summary(self, entry: CatalogEntry, *, source_id: str) -> SkillSummary:
        return SkillSummary.from_catalog(
            entry,
            source_id=source_id,
            platform_id=self.platform_id,
            controlled_data_ready=self._profiles[source_id].controlled_data_ready(entry),
        )

    @contextmanager
    def _lifecycle_lock(self) -> Iterator[None]:
        try:
            with native_file_lock(self.lifecycle_lock_path):
                yield
        except NativeLockUnavailableError as error:
            raise SkillSettingsError("Project storage cannot lock Skill state safely") from error

    def _assert_identity_available(self, candidate: SkillSummary) -> None:
        for bundled in self._bundled_summaries():
            if candidate.name == bundled.name or candidate.skill_id == bundled.skill_id:
                raise SkillSettingsError(
                    f"Skill identity conflicts with bundled Skill {bundled.name}"
                )
        try:
            records = self.store.records()
        except SkillStoreError as error:
            raise SkillSettingsError(str(error)) from error
        for record in records:
            exact_retry = (
                candidate.name == record.name
                and candidate.skill_id == record.skill_id
                and candidate.version == record.version
                and candidate.tree_sha256 == record.tree_sha256
                and candidate.source_id == record.source_id
                and candidate.source_revision == record.source_revision
            )
            if exact_retry:
                continue
            if candidate.name == record.name:
                raise SkillSettingsError(f"Installed Skill already exists: {candidate.name}")
            if candidate.skill_id == record.skill_id:
                raise SkillSettingsError(
                    f"Skill identifier is already active as {record.name}: {candidate.skill_id}"
                )

    def _refresh_source(self, source_id: str) -> SkillCatalogSnapshot:
        self._refresh(source_id)
        return self._snapshots[source_id]

    def _resolve_source_id(self, source_id: str | None) -> str:
        if source_id is not None:
            if source_id not in self._clients:
                raise SkillSettingsError(f"Skill source is not configured: {source_id}")
            return source_id
        configured = tuple(sorted(self._clients))
        if len(configured) == 1:
            return configured[0]
        if not configured:
            raise SkillSettingsError("No signed Skill source is configured")
        raise SkillSettingsError("Choose a Skill source: " + ", ".join(configured))

    def _bundled_summaries(self) -> tuple[SkillSummary, ...]:
        if not self.bundled_dir.is_dir():
            return ()
        verifier = LocalSkillVerifier(
            self.bundled_dir,
            review="repository-reviewed",
            require_repository_review=True,
        )
        summaries: list[SkillSummary] = []
        for path in sorted(self.bundled_dir.iterdir()):
            if not path.is_dir():
                continue
            try:
                manifest = verifier.load_manifest(path)
            except SkillVerificationError as error:
                raise SkillSettingsError(f"Invalid bundled Skill {path.name}: {error}") from error
            summaries.append(SkillSummary.from_manifest(manifest, source="bundled"))
        return tuple(summaries)

    def _installed_summaries(self) -> tuple[SkillSummary, ...]:
        try:
            records = self.store.records()
            self._verify_active_installation_audit(records)
            return tuple(
                SkillSummary.from_installed(record, self.store.manifest(record))
                for record in records
            )
        except SkillStoreError as error:
            raise SkillSettingsError(str(error)) from error

    def _verify_active_installation_audit(
        self,
        records: tuple[InstalledSkillRecord, ...],
    ) -> None:
        active = tuple(record for record in records if record.status == "active")
        if not active:
            return
        try:
            audit = AuditLog(self.audit_path)
            events = audit.read()
            audit.verify(events)
        except (AuditIntegrityError, NativeLockUnavailableError) as error:
            raise SkillSettingsError(f"Unable to verify Skill audit state: {error}") from error
        for record in active:
            latest_event = next(
                (
                    event.event_type
                    for event in reversed(events)
                    if event.event_type in {"skill.activated", "skill.deactivated"}
                    and event.payload.get("skill_id") == record.skill_id
                    and event.payload.get("tree_sha256") == record.tree_sha256
                    and event.payload.get("source_id") == record.source_id
                    and event.payload.get("source_revision") == record.source_revision
                ),
                None,
            )
            if latest_event != "skill.activated":
                raise SkillSettingsError(
                    f"Active Skill is missing a verified activation event: {record.name}"
                )

    def _record_decision(
        self,
        summary: SkillSummary,
        *,
        approved: bool,
        actor_id: str,
    ) -> None:
        self._append_audit(
            summary,
            event="installation-decision",
            actor_id=actor_id,
            decision="approved" if approved else "denied",
        )

    def _record_removal(self, summary: SkillSummary, *, actor_id: str = "human") -> None:
        self._append_audit(summary, event="deactivated", actor_id=actor_id, decision=None)

    def _record_installation_result(
        self,
        summary: SkillSummary,
        *,
        activated: bool,
        actor_id: str,
    ) -> None:
        self._append_audit(
            summary,
            event="activated" if activated else "installation-failed",
            actor_id=actor_id,
            decision=None,
        )

    def _append_audit(
        self,
        summary: SkillSummary,
        *,
        event: Literal["installation-decision", "activated", "installation-failed", "deactivated"],
        actor_id: str,
        decision: Literal["approved", "denied"] | None,
    ) -> None:
        occurred_at = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        payload: dict[str, JsonValue] = {
            "actor_id": actor_id,
            "name": summary.name,
            "skill_id": summary.skill_id,
            "version": summary.version,
            "tree_sha256": summary.tree_sha256,
            "source_id": summary.source_id,
            "source_revision": summary.source_revision,
            "review": summary.review,
            "controlled_data_ready": summary.controlled_data_ready,
        }
        if decision is not None:
            payload["decision"] = decision
        try:
            AuditLog(self.audit_path).append(
                session_id="project-skills",
                event_type=f"skill.{event}",
                occurred_at=occurred_at,
                payload=payload,
            )
        except (AuditIntegrityError, NativeLockUnavailableError) as error:
            raise SkillSettingsError(f"Unable to record Skill audit event: {error}") from error


def _summary_order(summary: SkillSummary) -> tuple[int, str, str]:
    rank = {"bundled": 0, "installed": 1, "catalog": 2, "local-candidate": 3}
    return rank[summary.source], summary.name, summary.source_id
