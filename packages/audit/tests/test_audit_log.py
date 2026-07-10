# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Tests for hash-chained audit logging."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from heartwood.audit import AuditIntegrityError, AuditLog


def test_audit_log_appends_hash_chained_events(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    first = log.append(
        session_id="session-1",
        event_type="command.received",
        occurred_at="2026-01-01T00:00:00Z",
        payload={"command_id": "command-1"},
    )
    second = log.append(
        session_id="session-1",
        event_type="detection.proposed",
        occurred_at="2026-01-01T00:00:01Z",
        payload={"platform": "generic"},
    )

    assert second.sequence == 1
    assert second.previous_event_hash == first.event_hash
    assert stat.S_IMODE(log.path.stat().st_mode) == 0o600
    log.verify()


def test_audit_log_detects_tampering(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    log.append(
        session_id="session-1",
        event_type="command.received",
        occurred_at="2026-01-01T00:00:00Z",
        payload={"command_id": "command-1"},
    )
    line = path.read_text(encoding="utf-8").splitlines()[0]
    payload = json.loads(line)
    payload["payload"]["command_id"] = "changed"
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    with pytest.raises(AuditIntegrityError):
        log.verify()


def test_audit_export_scrubs_sensitive_payload_fields(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    log.append(
        session_id="session-1",
        event_type="tool.execution.recorded",
        occurred_at="2026-01-01T00:00:00Z",
        payload={
            "prompt": "show records",
            "path": "/workspace/private/participant-output.csv",
            "row": {"person_id": "person-1"},
            "summary": "bounded preview",
            "apiKey": "inline-api-key",
            "nested": {
                "Authorization": "Bearer inline-token",
                "client_secret": "inline-client-secret",
            },
        },
    )

    persisted = path.read_text(encoding="utf-8")
    assert "show records" not in persisted
    assert "person-1" not in persisted
    assert "participant-output.csv" not in persisted
    assert "bounded preview" not in persisted
    assert "inline-api-key" not in persisted
    assert "inline-token" not in persisted
    assert "inline-client-secret" not in persisted

    exported = json.loads(log.export_jsonl().splitlines()[0])
    assert exported["payload"]["prompt"] == "[scrubbed]"
    assert exported["payload"]["path"] == "[scrubbed]"
    assert exported["payload"]["row"] == "[scrubbed]"
    assert exported["payload"]["summary"] == "[scrubbed]"
    assert exported["payload"]["apiKey"] == "[scrubbed]"
    assert exported["payload"]["nested"]["Authorization"] == "[scrubbed]"
    assert exported["payload"]["nested"]["client_secret"] == "[scrubbed]"
