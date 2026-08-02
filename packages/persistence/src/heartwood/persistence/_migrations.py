# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Deterministic migrations for persisted Heartwood envelopes."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass

type JsonObject = dict[str, object]
type Migration = Callable[[Mapping[str, object]], JsonObject]

PROJECT_STATE_KIND = "project-state"
PROJECT_CONFIG_KIND = "project-config"
SESSION_EVENT_KIND = "session-event"
SESSION_METADATA_KIND = "session-metadata"
SESSION_COMMAND_RECEIPT_KIND = "session-command-receipt"
SESSION_COMMIT_KIND = "session-commit"
SESSION_WRITER_KIND = "session-writer"
AUDIT_EVENT_KIND = "audit-event"
SKILL_METADATA_KIND = "skill-metadata"
OPENHANDS_STATE_KIND = "openhands-state"

PROJECT_STATE_VERSION = "heartwood.project-state.v2"
PROJECT_CONFIG_VERSION = "heartwood.project-config.v1"
SESSION_EVENT_VERSION = "heartwood.session-event.v1"
SESSION_METADATA_VERSION = "heartwood.session-metadata.v1"
SESSION_COMMAND_RECEIPT_VERSION = "heartwood.session-command-receipt.v1"
SESSION_COMMIT_VERSION = "heartwood.session-commit.v1"
SESSION_WRITER_VERSION = "heartwood.session-writer.v1"
AUDIT_EVENT_VERSION = "heartwood.audit-event.v1"
SKILL_METADATA_VERSION = "heartwood.skill-metadata.v1"
OPENHANDS_STATE_VERSION = "heartwood.openhands-state.v1"

PROJECT_STATE_FORMATS: dict[str, str] = {
    "audit_event": AUDIT_EVENT_VERSION,
    "project_config": PROJECT_CONFIG_VERSION,
    "openhands_state": OPENHANDS_STATE_VERSION,
    "session_command_receipt": SESSION_COMMAND_RECEIPT_VERSION,
    "session_commit": SESSION_COMMIT_VERSION,
    "session_event": SESSION_EVENT_VERSION,
    "session_metadata": SESSION_METADATA_VERSION,
    "session_writer": SESSION_WRITER_VERSION,
    "skill_metadata": SKILL_METADATA_VERSION,
}


class MigrationError(ValueError):
    """Raised when persisted state cannot migrate to its current schema."""


@dataclass(frozen=True, slots=True)
class MigrationResult:
    """One normalized payload and the versions traversed to produce it."""

    payload: JsonObject
    source_version: str
    target_version: str
    applied_versions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _MigrationStep:
    target_version: str
    migrate: Migration


class MigrationRegistry:
    """Own one deterministic, acyclic migration path for each persisted kind."""

    def __init__(self) -> None:
        self._current: dict[str, str] = {}
        self._steps: dict[tuple[str, str], _MigrationStep] = {}

    def register_kind(self, kind: str, *, current_version: str) -> None:
        """Register the one schema version newly written for a persisted kind."""
        _require_identifier(kind, "persisted kind")
        _require_identifier(current_version, "current schema version")
        existing = self._current.get(kind)
        if existing is not None and existing != current_version:
            raise MigrationError(f"current schema is already registered for {kind}")
        self._current[kind] = current_version

    def register(
        self,
        kind: str,
        *,
        source_version: str,
        target_version: str,
        migrate: Migration,
    ) -> None:
        """Register one forward-only migration step."""
        if kind not in self._current:
            raise MigrationError(f"persisted kind is not registered: {kind}")
        _require_identifier(source_version, "source schema version")
        _require_identifier(target_version, "target schema version")
        if source_version == target_version:
            raise MigrationError("migration source and target schemas must differ")
        key = (kind, source_version)
        if key in self._steps:
            raise MigrationError(f"migration source is already registered for {kind}")
        self._steps[key] = _MigrationStep(target_version=target_version, migrate=migrate)

    def current_version(self, kind: str) -> str:
        """Return the current persisted schema for a kind."""
        try:
            return self._current[kind]
        except KeyError as error:
            raise MigrationError(f"persisted kind is not registered: {kind}") from error

    def migrate(self, kind: str, payload: Mapping[str, object]) -> MigrationResult:
        """Normalize a payload without mutating it or exposing its content in errors."""
        target = self.current_version(kind)
        working = deepcopy(dict(payload))
        source = _schema_version(working, kind)
        version = source
        applied: list[str] = []
        visited: set[str] = set()
        while version != target:
            if version in visited:
                raise MigrationError(f"migration cycle detected for {kind}")
            visited.add(version)
            step = self._steps.get((kind, version))
            if step is None:
                raise MigrationError(f"unsupported persisted schema for {kind}: {version}")
            before = deepcopy(working)
            first_input = deepcopy(working)
            second_input = deepcopy(working)
            first = step.migrate(first_input)
            second = step.migrate(second_input)
            if first_input != before or second_input != before:
                raise MigrationError(f"migration mutated its input for {kind}: {version}")
            if first != second:
                raise MigrationError(f"migration is not deterministic for {kind}: {version}")
            working = _json_object(first, kind)
            next_version = _schema_version(working, kind)
            if next_version != step.target_version:
                raise MigrationError(f"migration produced an invalid target for {kind}: {version}")
            applied.append(next_version)
            version = next_version
        return MigrationResult(
            payload=working,
            source_version=source,
            target_version=target,
            applied_versions=tuple(applied),
        )

    def supported_versions(self, kind: str) -> tuple[str, ...]:
        """Return versions with a complete path to the current schema."""
        target = self.current_version(kind)
        candidates = {target, *(source for step_kind, source in self._steps if step_kind == kind)}
        supported: list[str] = []
        for candidate in candidates:
            version = candidate
            visited: set[str] = set()
            while version != target and version not in visited:
                visited.add(version)
                step = self._steps.get((kind, version))
                if step is None:
                    break
                version = step.target_version
            if version == target:
                supported.append(candidate)
        return tuple(sorted(supported))


def _project_state_v1_to_v2(payload: Mapping[str, object]) -> JsonObject:
    if set(payload) != {"schema_version"}:
        raise MigrationError("legacy project state contains unsupported fields")
    return {
        "schema_version": PROJECT_STATE_VERSION,
        "formats": dict(PROJECT_STATE_FORMATS),
    }


def _openhands_unversioned_to_v1(payload: Mapping[str, object]) -> JsonObject:
    allowed = {"schema_version", "openhands_sdk_version", "content_policy"}
    if set(payload) != allowed:
        raise MigrationError("unversioned OpenHands state contains unsupported fields")
    sdk_version = payload.get("openhands_sdk_version")
    content_policy = payload.get("content_policy")
    if not isinstance(sdk_version, str) or not sdk_version:
        raise MigrationError("unversioned OpenHands state has no SDK version")
    if not isinstance(content_policy, str) or not content_policy:
        raise MigrationError("unversioned OpenHands state has no content policy")
    return {
        "schema_version": OPENHANDS_STATE_VERSION,
        "openhands_sdk_version": sdk_version,
        "content_policy": content_policy,
        "adopted_from": "unversioned",
    }


def _build_registry() -> MigrationRegistry:
    registry = MigrationRegistry()
    for kind, version in (
        (PROJECT_STATE_KIND, PROJECT_STATE_VERSION),
        (PROJECT_CONFIG_KIND, PROJECT_CONFIG_VERSION),
        (SESSION_EVENT_KIND, SESSION_EVENT_VERSION),
        (SESSION_METADATA_KIND, SESSION_METADATA_VERSION),
        (SESSION_COMMAND_RECEIPT_KIND, SESSION_COMMAND_RECEIPT_VERSION),
        (SESSION_COMMIT_KIND, SESSION_COMMIT_VERSION),
        (SESSION_WRITER_KIND, SESSION_WRITER_VERSION),
        (AUDIT_EVENT_KIND, AUDIT_EVENT_VERSION),
        (SKILL_METADATA_KIND, SKILL_METADATA_VERSION),
        (OPENHANDS_STATE_KIND, OPENHANDS_STATE_VERSION),
    ):
        registry.register_kind(kind, current_version=version)
    registry.register(
        PROJECT_STATE_KIND,
        source_version="heartwood.project-state.v1",
        target_version=PROJECT_STATE_VERSION,
        migrate=_project_state_v1_to_v2,
    )
    registry.register(
        OPENHANDS_STATE_KIND,
        source_version="heartwood.openhands-state.unversioned",
        target_version=OPENHANDS_STATE_VERSION,
        migrate=_openhands_unversioned_to_v1,
    )
    return registry


def _schema_version(payload: Mapping[str, object], kind: str) -> str:
    value = payload.get("schema_version")
    if not isinstance(value, str) or not value:
        raise MigrationError(f"persisted state has no schema version for {kind}")
    return value


def _json_object(value: object, kind: str) -> JsonObject:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise MigrationError(f"migration did not produce an object for {kind}")
    return deepcopy(value)


def _require_identifier(value: str, label: str) -> None:
    if not value.strip():
        raise MigrationError(f"{label} must not be empty")


PERSISTENCE_MIGRATIONS = _build_registry()
