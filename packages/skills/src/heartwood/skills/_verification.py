# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Verification gate for local ``SKILL.md`` directories."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from json import JSONDecodeError
from pathlib import Path
from typing import Final, Literal, cast

from pydantic import ValidationError

from heartwood.schemas import ApprovalRecord, JsonValue, SkillMetadata

_DEFAULT_ALLOWED_TOOLS: Final[tuple[str, ...]] = (
    "read-local-csv",
    "write-aggregate-json",
    "train-synthetic-baseline",
    "emit-replay-record",
)


class SkillVerificationError(ValueError):
    """Raised when a local skill directory fails verification."""


@dataclass(frozen=True, slots=True)
class SkillManifest:
    """Verified local skill manifest derived from ``SKILL.md`` frontmatter."""

    skill_id: str
    name: str
    description: str
    root: Path
    metadata: SkillMetadata
    declared_tools: tuple[str, ...]
    approval_summary: str
    entrypoint: Path


@dataclass(frozen=True, slots=True)
class SkillVerification:
    """Result of verifying a local skill directory."""

    verified: bool
    reason: str
    manifest: SkillManifest | None = None


class LocalSkillVerifier:
    """Verify local ``SKILL.md`` directories before they can be activated."""

    def __init__(
        self,
        root: Path,
        *,
        allowed_tools: tuple[str, ...] = _DEFAULT_ALLOWED_TOOLS,
        require_verified_tier: bool = True,
        allow_network: bool = False,
    ) -> None:
        """Initialize a root-confined verifier."""
        self.root = root.resolve()
        self.allowed_tools = allowed_tools
        self.require_verified_tier = require_verified_tier
        self.allow_network = allow_network

    def verify(self, path: Path) -> SkillVerification:
        """Return a verification result for a local skill path."""
        try:
            manifest = self.load_manifest(path)
        except SkillVerificationError as error:
            return SkillVerification(verified=False, reason=str(error))
        return SkillVerification(
            verified=True,
            reason="local skill metadata, approval copy, and entrypoint verified",
            manifest=manifest,
        )

    def load_manifest(self, path: Path) -> SkillManifest:
        """Load and verify a local skill manifest."""
        skill_root = path.resolve()
        if not skill_root.is_relative_to(self.root):
            msg = f"skill path escapes verification root: {path}"
            raise SkillVerificationError(msg)
        manifest = load_skill_manifest(skill_root)
        if self.require_verified_tier and manifest.metadata.trust_tier != "verified":
            msg = "only verified skills can pass this local gate"
            raise SkillVerificationError(msg)
        if manifest.metadata.requires_network and not self.allow_network:
            msg = "skills requiring network access are not allowed in the local gate"
            raise SkillVerificationError(msg)
        if manifest.metadata.trust_tier == "verified" and (
            not manifest.metadata.signature
            or not manifest.metadata.signature.startswith("sigstore:")
        ):
            msg = "verified skills must declare a sigstore provenance placeholder"
            raise SkillVerificationError(msg)
        unsupported_tools = sorted(set(manifest.declared_tools) - set(self.allowed_tools))
        if unsupported_tools:
            msg = f"skill declares unsupported tools: {', '.join(unsupported_tools)}"
            raise SkillVerificationError(msg)
        if not manifest.entrypoint.is_relative_to(skill_root):
            msg = "skill entrypoint escapes the skill root"
            raise SkillVerificationError(msg)
        if not manifest.entrypoint.is_file():
            msg = f"skill entrypoint does not exist: {manifest.entrypoint.relative_to(skill_root)}"
            raise SkillVerificationError(msg)
        return manifest


def load_skill_manifest(skill_root: Path) -> SkillManifest:
    """Load a local skill manifest from a ``SKILL.md`` directory."""
    skill_root = skill_root.resolve()
    skill_file = skill_root / "SKILL.md"
    metadata_file = skill_root / "metadata.json"
    if not skill_file.is_file():
        msg = "skill is missing SKILL.md"
        raise SkillVerificationError(msg)
    if not metadata_file.is_file():
        msg = "skill is missing metadata.json"
        raise SkillVerificationError(msg)

    frontmatter = _read_skill_frontmatter(skill_file)
    metadata_payload = _mapping(frontmatter.get("metadata"), "metadata")
    try:
        skill_metadata = SkillMetadata.model_validate(metadata_payload)
    except ValidationError as error:
        msg = "SKILL.md metadata is invalid"
        raise SkillVerificationError(msg) from error
    try:
        metadata_json = json.loads(metadata_file.read_text(encoding="utf-8"))
    except JSONDecodeError as error:
        msg = "metadata.json is invalid JSON"
        raise SkillVerificationError(msg) from error
    try:
        file_metadata = SkillMetadata.model_validate(metadata_json)
    except ValidationError as error:
        msg = "metadata.json is invalid"
        raise SkillVerificationError(msg) from error
    if skill_metadata.model_dump(mode="json", by_alias=True) != file_metadata.model_dump(
        mode="json", by_alias=True
    ):
        msg = "SKILL.md metadata does not match metadata.json"
        raise SkillVerificationError(msg)

    skill_id = _required_string(frontmatter, "id")
    name = _required_string(frontmatter, "name")
    description = _required_string(frontmatter, "description")
    approval_summary = _required_string(frontmatter, "approval-summary")
    declared_tools = _split_csv(_required_string(frontmatter, "tools"))
    if not declared_tools:
        msg = "skill must declare at least one tool"
        raise SkillVerificationError(msg)
    entrypoint = (skill_root / _required_string(frontmatter, "entrypoint")).resolve()
    return SkillManifest(
        skill_id=skill_id,
        name=name,
        description=description,
        root=skill_root,
        metadata=skill_metadata,
        declared_tools=declared_tools,
        approval_summary=approval_summary,
        entrypoint=entrypoint,
    )


def build_skill_approval_record(
    manifest: SkillManifest,
    *,
    session_id: str,
    actor_id: str,
    occurred_at: str,
    decision: str = "approved",
) -> ApprovalRecord:
    """Build the approval record that authorizes loading a verified skill."""
    if decision not in {"approved", "denied"}:
        msg = f"unsupported skill approval decision: {decision}"
        raise SkillVerificationError(msg)
    checked_decision = cast(Literal["approved", "denied"], decision)
    return ApprovalRecord(
        approval_id=f"{session_id}-{manifest.skill_id.rsplit('.', maxsplit=1)[-1]}-approval",
        session_id=session_id,
        target_type="skill",
        target_id=manifest.skill_id,
        decision=checked_decision,
        actor_id=actor_id,
        occurred_at=occurred_at,
        reason=manifest.approval_summary,
    )


def _read_skill_frontmatter(path: Path) -> dict[str, JsonValue]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    start = 0
    if not lines or lines[start].strip() != "---":
        msg = "SKILL.md must start with YAML frontmatter"
        raise SkillVerificationError(msg)
    try:
        end = next(
            index
            for index, line in enumerate(lines[start + 1 :], start=start + 1)
            if line.strip() == "---"
        )
    except StopIteration as error:
        msg = "SKILL.md frontmatter is not closed"
        raise SkillVerificationError(msg) from error

    result: dict[str, JsonValue] = {}
    current_section: str | None = None
    for raw_line in lines[start + 1 : end]:
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        if raw_line.startswith("  ") and current_section:
            section = _mapping(result[current_section], current_section)
            key, value = _split_key_value(raw_line.strip())
            section[key] = _parse_scalar(value)
            continue
        key, value = _split_key_value(raw_line)
        if value.strip():
            result[key] = _parse_scalar(value)
            current_section = None
        else:
            result[key] = {}
            current_section = key
    return result


def _split_key_value(line: str) -> tuple[str, str]:
    if ":" not in line:
        msg = f"unsupported SKILL.md frontmatter line: {line}"
        raise SkillVerificationError(msg)
    key, value = line.split(":", maxsplit=1)
    return key.strip(), value.strip()


def _parse_scalar(value: str) -> JsonValue:
    normalized = value.strip()
    if len(normalized) >= 2 and normalized[0] == normalized[-1] == '"':
        return normalized[1:-1]
    if normalized.lower() == "true":
        return True
    if normalized.lower() == "false":
        return False
    return normalized


def _required_string(mapping: Mapping[str, JsonValue], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        msg = f"skill frontmatter requires string field: {key}"
        raise SkillVerificationError(msg)
    return value


def _mapping(value: JsonValue | None, name: str) -> dict[str, JsonValue]:
    if isinstance(value, dict):
        return value
    msg = f"skill frontmatter requires object field: {name}"
    raise SkillVerificationError(msg)


def _split_csv(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())
