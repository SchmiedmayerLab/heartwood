# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Tests for hash-chained audit logging."""

from __future__ import annotations

import json
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from filelock import FileLock

from heartwood.audit import (
    AuditIntegrityError,
    AuditLog,
    canonical_audit_jsonl,
    prepare_audit_event,
    verify_audit_events,
    verify_audit_jsonl,
)
from heartwood.persistence import NativeLockUnavailableError


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
        event_type="session.paused",
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


def test_audit_log_serializes_concurrent_sequence_derivation(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"

    def append(index: int) -> None:
        AuditLog(path).append(
            session_id="session-1",
            event_type="command.received",
            occurred_at=f"2026-01-01T00:00:{index:02d}Z",
            payload={"command_id": f"command-{index}"},
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        tuple(executor.map(append, range(20)))

    events = AuditLog(path).read()
    assert [event.sequence for event in events] == list(range(20))
    assert AuditLog(path).verify(events).event_count == 20


def test_audit_append_rejects_session_change_and_corrupt_tail(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    log.append(
        session_id="session-1",
        event_type="command.received",
        occurred_at="2026-01-01T00:00:00Z",
    )

    with pytest.raises(AuditIntegrityError, match="session does not match"):
        log.append(
            session_id="session-2",
            event_type="command.received",
            occurred_at="2026-01-01T00:00:01Z",
        )

    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["event_hash"] = f"sha256:{'0' * 64}"
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    with pytest.raises(AuditIntegrityError, match="tail is invalid"):
        log.append(
            session_id="session-1",
            event_type="session.paused",
            occurred_at="2026-01-01T00:00:01Z",
        )


def test_audit_append_rejects_an_earlier_session_change(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    first = prepare_audit_event(
        session_id="other-session",
        sequence=0,
        previous_event_hash=None,
        event_type="command.received",
        occurred_at="2026-01-01T00:00:00Z",
    )
    second = prepare_audit_event(
        session_id="session-1",
        sequence=1,
        previous_event_hash=first.event_hash,
        event_type="session.paused",
        occurred_at="2026-01-01T00:00:01Z",
    )
    third = prepare_audit_event(
        session_id="session-1",
        sequence=2,
        previous_event_hash=second.event_hash,
        event_type="session.resumed",
        occurred_at="2026-01-01T00:00:02Z",
    )
    path.write_text(canonical_audit_jsonl((first, second, third)), encoding="utf-8")

    with pytest.raises(AuditIntegrityError, match="session does not match"):
        AuditLog(path).append(
            session_id="session-1",
            event_type="command.received",
            occurred_at="2026-01-01T00:00:03Z",
        )


def test_full_audit_verification_rejects_identity_sequence_and_link_changes(
    tmp_path: Path,
) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    log.append(
        session_id="session-1",
        event_type="command.received",
        occurred_at="2026-01-01T00:00:00Z",
    )
    log.append(
        session_id="session-1",
        event_type="session.paused",
        occurred_at="2026-01-01T00:00:01Z",
    )
    first, second = log.read()

    with pytest.raises(AuditIntegrityError, match="session mismatch"):
        verify_audit_events((first, second.model_copy(update={"session_id": "session-2"})))
    with pytest.raises(AuditIntegrityError, match="sequence gap"):
        verify_audit_events((first, second.model_copy(update={"sequence": 3})))
    with pytest.raises(AuditIntegrityError, match="previous hash mismatch"):
        verify_audit_events((first, second.model_copy(update={"previous_event_hash": None})))


def test_malformed_audit_exports_and_lock_failures_remain_distinct(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(AuditIntegrityError, match="malformed"):
        verify_audit_jsonl("{\n")

    path = tmp_path / "malformed.jsonl"
    path.write_text("[]\n", encoding="utf-8")
    with pytest.raises(AuditIntegrityError, match="audit log is malformed"):
        AuditLog(path).read()
    with pytest.raises(AuditIntegrityError, match="audit log is malformed"):
        AuditLog(path).append(
            session_id="session-1",
            event_type="command.received",
            occurred_at="2026-01-01T00:00:00Z",
        )

    def unavailable(*_args: object, **_kwargs: object) -> None:
        raise OSError("unsupported filesystem")

    monkeypatch.setattr(FileLock, "acquire", unavailable)
    with pytest.raises(NativeLockUnavailableError, match="required native lock"):
        AuditLog(tmp_path / "audit.jsonl").read()


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
            "arguments": {"command": "python run.py /workspace/private/participant-output.csv"},
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
    assert "python run.py" not in persisted
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
