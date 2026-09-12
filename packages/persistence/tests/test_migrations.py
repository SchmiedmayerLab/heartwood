# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Compatibility and determinism tests for persisted schema migrations."""

from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path

import pytest

from heartwood.persistence import (
    AUDIT_EVENT_KIND,
    AUDIT_EVENT_VERSION,
    EXPERIMENT_ENTRY_KIND,
    EXPERIMENT_ENTRY_VERSION,
    EXPERIMENT_EVENT_KIND,
    EXPERIMENT_EVENT_VERSION,
    OPENHANDS_STATE_KIND,
    OPENHANDS_STATE_VERSION,
    PERSISTENCE_MIGRATIONS,
    PROJECT_CONFIG_KIND,
    PROJECT_CONFIG_VERSION,
    PROJECT_STATE_KIND,
    PROJECT_STATE_VERSION,
    SESSION_COMMAND_RECEIPT_KIND,
    SESSION_COMMAND_RECEIPT_VERSION,
    SESSION_COMMIT_KIND,
    SESSION_COMMIT_VERSION,
    SESSION_EVENT_KIND,
    SESSION_EVENT_VERSION,
    SESSION_METADATA_KIND,
    SESSION_METADATA_VERSION,
    SESSION_WRITER_KIND,
    SESSION_WRITER_VERSION,
    MigrationError,
    MigrationRegistry,
)

_FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize(
    ("kind", "filename", "target"),
    [
        (PROJECT_STATE_KIND, "project-state-v1.json", PROJECT_STATE_VERSION),
        (PROJECT_STATE_KIND, "project-state-v2.json", PROJECT_STATE_VERSION),
        (PROJECT_STATE_KIND, "project-state-v3.json", PROJECT_STATE_VERSION),
        (PROJECT_CONFIG_KIND, "project-config-v1.json", PROJECT_CONFIG_VERSION),
        (SESSION_EVENT_KIND, "session-event-v1.json", SESSION_EVENT_VERSION),
        (
            SESSION_METADATA_KIND,
            "session-metadata-v1.json",
            SESSION_METADATA_VERSION,
        ),
        (
            SESSION_COMMAND_RECEIPT_KIND,
            "session-command-receipt-v1.json",
            SESSION_COMMAND_RECEIPT_VERSION,
        ),
        (SESSION_COMMIT_KIND, "session-commit-v1.json", SESSION_COMMIT_VERSION),
        (SESSION_WRITER_KIND, "session-writer-v1.json", SESSION_WRITER_VERSION),
        (AUDIT_EVENT_KIND, "audit-event-v1.json", AUDIT_EVENT_VERSION),
        (OPENHANDS_STATE_KIND, "openhands-state-unversioned.json", OPENHANDS_STATE_VERSION),
        (OPENHANDS_STATE_KIND, "openhands-state-v1.json", OPENHANDS_STATE_VERSION),
    ],
)
def test_checked_in_compatibility_fixtures_reach_current_schema(
    kind: str,
    filename: str,
    target: str,
) -> None:
    payload = _fixture(filename)
    original = deepcopy(payload)

    first = PERSISTENCE_MIGRATIONS.migrate(kind, payload)
    second = PERSISTENCE_MIGRATIONS.migrate(kind, payload)

    assert first == second
    assert first.target_version == target
    assert first.payload["schema_version"] == target
    assert payload == original


def test_project_state_migration_records_each_owned_persistence_format() -> None:
    result = PERSISTENCE_MIGRATIONS.migrate(
        PROJECT_STATE_KIND,
        _fixture("project-state-v1.json"),
    )

    assert result.source_version == "heartwood.project-state.v1"
    assert result.applied_versions == ("heartwood.project-state.v2", PROJECT_STATE_VERSION)
    assert result.payload == _fixture("project-state-v3.json")


@pytest.mark.parametrize(
    "change", ["unknown-format", "changed-format", "missing-format", "extra-field"]
)
def test_project_migration_never_relabels_unknown_format_declarations(change: str) -> None:
    payload = _fixture("project-state-v2.json")
    formats = payload["formats"]
    assert isinstance(formats, dict)
    if change == "extra-field":
        payload["private"] = "must-not-appear"
    elif change == "missing-format":
        del formats["session_event"]
    elif change == "changed-format":
        formats["session_event"] = "heartwood.session-event.v99"
    else:
        formats["unknown"] = "must-not-appear"
    before = deepcopy(payload)
    with pytest.raises(MigrationError, match="unsupported format declarations") as caught:
        PERSISTENCE_MIGRATIONS.migrate(PROJECT_STATE_KIND, payload)
    assert payload == before
    assert "must-not-appear" not in str(caught.value)


@pytest.mark.parametrize(
    ("kind", "version"),
    [
        (EXPERIMENT_EVENT_KIND, EXPERIMENT_EVENT_VERSION),
        (EXPERIMENT_ENTRY_KIND, EXPERIMENT_ENTRY_VERSION),
    ],
)
def test_experiment_formats_are_registered_without_rewriting_records(
    kind: str, version: str
) -> None:
    payload = {"schema_version": version, "content": "opaque-to-envelope-registry"}
    result = PERSISTENCE_MIGRATIONS.migrate(kind, payload)
    assert result.payload == payload
    assert not result.applied_versions
    with pytest.raises(MigrationError, match="unsupported persisted schema"):
        PERSISTENCE_MIGRATIONS.migrate(kind, {"schema_version": version.replace(".v1", ".v99")})


def test_registry_rejects_unsupported_version_without_exposing_payload() -> None:
    with pytest.raises(MigrationError, match="unsupported persisted schema") as captured:
        PERSISTENCE_MIGRATIONS.migrate(
            SESSION_EVENT_KIND,
            {
                "schema_version": "heartwood.session-event.v0",
                "prompt": "participant content",
            },
        )

    assert "participant content" not in str(captured.value)


def test_registry_rejects_mutating_and_nondeterministic_migrations() -> None:
    mutating = MigrationRegistry()
    mutating.register_kind("record", current_version="record.v2")

    def mutate(payload: Mapping[str, object]) -> dict[str, object]:
        assert isinstance(payload, dict)
        payload["schema_version"] = "record.v2"
        return payload

    mutating.register(
        "record",
        source_version="record.v1",
        target_version="record.v2",
        migrate=mutate,
    )
    with pytest.raises(MigrationError, match="mutated its input"):
        mutating.migrate("record", {"schema_version": "record.v1"})

    calls = 0
    nondeterministic = MigrationRegistry()
    nondeterministic.register_kind("record", current_version="record.v2")

    def vary(payload: Mapping[str, object]) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {**payload, "schema_version": "record.v2", "attempt": calls}

    nondeterministic.register(
        "record",
        source_version="record.v1",
        target_version="record.v2",
        migrate=vary,
    )
    with pytest.raises(MigrationError, match="not deterministic"):
        nondeterministic.migrate("record", {"schema_version": "record.v1"})


def test_registry_detects_cycles_and_reports_supported_versions() -> None:
    registry = MigrationRegistry()
    registry.register_kind("record", current_version="record.v3")
    registry.register(
        "record",
        source_version="record.v1",
        target_version="record.v2",
        migrate=lambda payload: {**payload, "schema_version": "record.v2"},
    )
    registry.register(
        "record",
        source_version="record.v2",
        target_version="record.v1",
        migrate=lambda payload: {**payload, "schema_version": "record.v1"},
    )

    assert registry.supported_versions("record") == ("record.v3",)
    with pytest.raises(MigrationError, match="cycle"):
        registry.migrate("record", {"schema_version": "record.v1"})


def test_registry_rejects_invalid_registration_and_migration_contracts() -> None:
    registry = MigrationRegistry()
    with pytest.raises(MigrationError, match="must not be empty"):
        registry.register_kind("", current_version="record.v1")
    with pytest.raises(MigrationError, match="not registered"):
        registry.current_version("missing")
    with pytest.raises(MigrationError, match="not registered"):
        registry.register(
            "missing",
            source_version="record.v1",
            target_version="record.v2",
            migrate=dict,
        )

    registry.register_kind("record", current_version="record.v2")
    with pytest.raises(MigrationError, match="must differ"):
        registry.register(
            "record",
            source_version="record.v1",
            target_version="record.v1",
            migrate=dict,
        )
    registry.register(
        "record",
        source_version="record.v1",
        target_version="record.v2",
        migrate=lambda payload: {**payload, "schema_version": "record.v3"},
    )
    with pytest.raises(MigrationError, match="invalid target"):
        registry.migrate("record", {"schema_version": "record.v1"})


def test_shared_registry_is_frozen_after_construction() -> None:
    with pytest.raises(MigrationError, match="registry is frozen"):
        PERSISTENCE_MIGRATIONS.register_kind("new-record", current_version="record.v1")

    registry = MigrationRegistry()
    registry.register_kind("record", current_version="record.v1")
    registry.freeze()
    with pytest.raises(MigrationError, match="registry is frozen"):
        registry.register(
            "record",
            source_version="record.v0",
            target_version="record.v1",
            migrate=dict,
        )


def test_registry_rejects_missing_schema_and_invalid_builtin_legacy_state() -> None:
    with pytest.raises(MigrationError, match="no schema version"):
        PERSISTENCE_MIGRATIONS.migrate(PROJECT_STATE_KIND, {})
    with pytest.raises(MigrationError, match="unsupported fields") as captured:
        PERSISTENCE_MIGRATIONS.migrate(
            PROJECT_STATE_KIND,
            {
                "schema_version": "heartwood.project-state.v1",
                "private_content": "must-not-appear-in-errors",
            },
        )
    assert "must-not-appear-in-errors" not in str(captured.value)
    with pytest.raises(MigrationError, match="no SDK version"):
        PERSISTENCE_MIGRATIONS.migrate(
            OPENHANDS_STATE_KIND,
            {
                "schema_version": "heartwood.openhands-state.unversioned",
                "openhands_sdk_version": "",
                "content_policy": "heartwood.openhands-content-minimized.v1",
            },
        )


def _fixture(name: str) -> dict[str, object]:
    value = json.loads((_FIXTURES / name).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value
