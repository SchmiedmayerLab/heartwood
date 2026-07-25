# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Tests for gateway-owned session lifecycle metadata."""

from __future__ import annotations

import errno
import json
import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import heartwood.core_adapter._state as session_state
from heartwood.audit import AuditLog
from heartwood.core_adapter import FileSessionStore, SessionService
from heartwood.gateway import (
    ProjectContext,
    RestGateway,
    RestRequest,
    SessionCatalog,
    SessionCatalogError,
    SessionGateway,
    SessionNotFoundError,
    SessionSummary,
)
from heartwood.session import (
    CommandKind,
    EventKind,
    JsonValue,
    SessionCommand,
    SessionEvent,
    compute_session_event_hash,
)


def _event(sequence: int, kind: EventKind, **payload: JsonValue) -> SessionEvent:
    return SessionEvent(
        event_id=f"event-{sequence}",
        session_id="legacy-session",
        sequence=sequence,
        kind=kind,
        occurred_at=f"2026-01-01T00:00:{sequence:02d}Z",
        payload=payload,
    )


def _append_event(store: FileSessionStore, event: SessionEvent) -> None:
    audit_log = AuditLog(store.audit_path)
    audit_events = audit_log.read()
    previous_hash = audit_events[-1].event_hash if audit_events else None
    paired_event = event.model_copy(update={"previous_event_hash": previous_hash})
    audit_event = audit_log.prepare(
        session_id=paired_event.session_id,
        event_type=str(paired_event.kind),
        occurred_at=paired_event.occurred_at,
        payload={"session_event_hash": compute_session_event_hash(paired_event)},
    )
    store.commit_event(paired_event, audit_event)


def test_catalog_creates_lists_and_renames_private_session_metadata(tmp_path: Path) -> None:
    catalog = SessionCatalog(tmp_path / "sessions")

    created = catalog.create("  Cohort   review  ")
    renamed = catalog.rename(created.session_id, "Aggregate analysis")
    sessions = SessionCatalog(tmp_path / "sessions").list()

    assert created.title == "Cohort review"
    assert created.status == "empty"
    assert renamed.title == "Aggregate analysis"
    assert sessions == (renamed,)
    session_dir = tmp_path / "sessions" / created.session_id
    assert stat.S_IMODE(session_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((session_dir / "metadata.json").stat().st_mode) == 0o600


def test_catalog_discovers_paired_event_stores_and_derives_waiting_state(tmp_path: Path) -> None:
    store = FileSessionStore(tmp_path, "legacy-session")
    store.acquire_writer()
    _append_event(store, _event(0, EventKind.COMMAND_RECEIVED, command_id="command-0"))
    _append_event(
        store,
        _event(
            1,
            EventKind.CONFIRMATION_REQUESTED,
            request={"tool_call_id": "tool-1"},
        ),
    )

    summary = SessionCatalog(tmp_path).list()[0]

    assert summary.session_id == "legacy-session"
    assert summary.title == "legacy-session"
    assert summary.status == "waiting"
    assert summary.created_at == "2026-01-01T00:00:00Z"
    assert summary.updated_at == "2026-01-01T00:00:01Z"
    assert summary.event_count == 2


def test_catalog_derives_pause_error_and_recovery_states(tmp_path: Path) -> None:
    store = FileSessionStore(tmp_path, "legacy-session")
    store.acquire_writer()
    catalog = SessionCatalog(tmp_path)
    _append_event(store, _event(0, EventKind.SESSION_PAUSED))
    assert catalog.get("legacy-session").status == "paused"

    _append_event(store, _event(1, EventKind.SESSION_RESUMED))
    assert catalog.get("legacy-session").status == "idle"

    _append_event(store, _event(2, EventKind.ERROR_RECORDED, reason="synthetic"))
    assert catalog.get("legacy-session").status == "error"

    _append_event(store, _event(3, EventKind.COMMAND_RECEIVED, command_id="retry"))
    assert catalog.get("legacy-session").status == "idle"


def test_catalog_recovers_an_interrupted_paired_append_after_process_exit(
    tmp_path: Path,
) -> None:
    writer_script = """
import os
import sys
from pathlib import Path
import heartwood.core_adapter._state as state
from heartwood.core_adapter import SessionService
from heartwood.session import CommandKind, SessionCommand

root = Path(sys.argv[1])
original_append = state._append_private_json_line

def exit_after_audit(path, content):
    original_append(path, content)
    if path.name == "audit.jsonl":
        os._exit(17)

state._append_private_json_line = exit_after_audit
service = SessionService.synthetic_default(root)
service.handle(
    SessionCommand(
        command_id="catalog-crash",
        session_id="session-synthetic-001",
        kind=CommandKind.PAUSE,
        actor_id="writer",
        created_at="2026-01-01T00:00:00Z",
    )
)
"""
    crashed = subprocess.run(
        [sys.executable, "-c", writer_script, str(tmp_path)],
        check=False,
        capture_output=True,
        text=True,
    )

    summary = SessionCatalog(tmp_path).list()[0]

    assert crashed.returncode == 17, crashed.stderr
    assert summary.session_id == "session-synthetic-001"
    assert summary.status == "idle"
    assert summary.event_count == 1
    assert not (tmp_path / summary.session_id / ".pending-commit.json").exists()


def test_catalog_listing_waits_for_a_complete_concurrent_append(tmp_path: Path) -> None:
    writer_script = """
import sys
import time
from pathlib import Path
import heartwood.core_adapter._state as state
from heartwood.core_adapter import SessionService
from heartwood.session import CommandKind, SessionCommand

root = Path(sys.argv[1])
ready = root / "catalog-audit-appended"
release = root / "release-catalog-commit"
original_append = state._append_private_json_line

def pause_after_audit(path, content):
    original_append(path, content)
    if path.name == "audit.jsonl":
        ready.write_text("ready", encoding="utf-8")
        while not release.exists():
            time.sleep(0.01)

state._append_private_json_line = pause_after_audit
service = SessionService.synthetic_default(root)
service.handle(
    SessionCommand(
        command_id="catalog-concurrent",
        session_id="session-synthetic-001",
        kind=CommandKind.PAUSE,
        actor_id="writer",
        created_at="2026-01-01T00:00:00Z",
    )
)
service.close()
"""
    writer = subprocess.Popen(
        [sys.executable, "-c", writer_script, str(tmp_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    result: list[tuple[SessionSummary, ...]] = []
    reader: threading.Thread | None = None
    try:
        _wait_for_marker(writer, tmp_path / "catalog-audit-appended")
        reader = threading.Thread(target=lambda: result.append(SessionCatalog(tmp_path).list()))
        reader.start()
        time.sleep(0.1)
        assert reader.is_alive()

        (tmp_path / "release-catalog-commit").write_text("continue", encoding="utf-8")
        writer_stdout, writer_stderr = writer.communicate(timeout=5)
        reader.join(timeout=5)
    finally:
        if writer.poll() is None:
            writer.kill()
            writer.communicate(timeout=5)

    assert writer.returncode == 0, f"{writer_stdout}\n{writer_stderr}"
    assert reader is not None
    assert not reader.is_alive()
    assert len(result) == 1
    assert len(result[0]) == 1
    summary = result[0][0]
    assert summary.status == "paused"
    assert summary.event_count == 2


def test_catalog_marks_an_active_interrupted_append_as_recovery_required(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_append = session_state._append_private_json_line

    def fail_after_audit(path: Path, content: str) -> None:
        original_append(path, content)
        if path.name == "audit.jsonl":
            raise OSError("synthetic interrupted append")

    monkeypatch.setattr(session_state, "_append_private_json_line", fail_after_audit)
    service = SessionService.synthetic_default(tmp_path)
    command = SessionCommand(
        command_id="catalog-active-recovery",
        session_id="session-synthetic-001",
        kind=CommandKind.PAUSE,
        actor_id="writer",
        created_at="2026-01-01T00:00:00Z",
    )
    with pytest.raises(OSError, match="synthetic interrupted append"):
        service.handle(command)

    summary = SessionCatalog(tmp_path).list()[0]
    service.close()

    assert summary.status == "recovery-required"
    assert summary.event_count == 0


def test_catalog_keeps_unsupported_session_storage_visible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def native_lock_unavailable(_descriptor: int) -> bool:
        raise OSError(errno.ENOSYS, "synthetic unsupported filesystem")

    (tmp_path / "session-unsupported").mkdir()
    monkeypatch.setattr("filelock._unix._lock_fd_nonblocking", native_lock_unavailable)

    summary = SessionCatalog(tmp_path).list()[0]

    assert summary.session_id == "session-unsupported"
    assert summary.status == "recovery-required"
    assert summary.event_count == 0


def test_catalog_skips_corrupt_metadata_without_hiding_other_sessions(tmp_path: Path) -> None:
    catalog = SessionCatalog(tmp_path)
    valid = catalog.create("Valid session")
    corrupt_dir = tmp_path / "corrupt-session"
    corrupt_dir.mkdir(mode=0o700)
    (corrupt_dir / "metadata.json").write_text("{", encoding="utf-8")

    assert [session.session_id for session in catalog.list()] == [valid.session_id]
    with pytest.raises(SessionCatalogError, match="unable to load"):
        catalog.get("corrupt-session")


def test_catalog_closes_temporary_descriptor_when_metadata_open_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed_descriptors: list[int] = []
    real_close = os.close

    def fail_fdopen(*_args: object, **_kwargs: object) -> None:
        raise OSError("synthetic open failure")

    def record_close(descriptor: int) -> None:
        closed_descriptors.append(descriptor)
        real_close(descriptor)

    monkeypatch.setattr("heartwood.gateway._session_catalog.os.fdopen", fail_fdopen)
    monkeypatch.setattr("heartwood.gateway._session_catalog.os.close", record_close)

    with pytest.raises(OSError, match="synthetic open failure"):
        SessionCatalog(tmp_path).create()

    assert closed_descriptors
    session_dir = next(tmp_path.iterdir())
    assert list(session_dir.glob(".metadata-*")) == []


def test_catalog_rejects_invalid_titles_and_unknown_renames(tmp_path: Path) -> None:
    catalog = SessionCatalog(tmp_path)

    with pytest.raises(SessionCatalogError, match="must not be empty"):
        catalog.create("   ")
    with pytest.raises(SessionNotFoundError, match="unknown session"):
        catalog.get("missing")
    with pytest.raises(SessionNotFoundError, match="unknown session"):
        catalog.rename("missing", "New title")
    with pytest.raises(SessionCatalogError, match="at most"):
        catalog.create("x" * 121)
    with pytest.raises(SessionCatalogError, match="session id must start"):
        catalog.get("invalid/session")


def test_rest_exposes_session_creation_listing_and_rename(tmp_path: Path) -> None:
    rest = RestGateway(
        SessionGateway(
            project=ProjectContext(tmp_path),
            env={},
            backend_id="deterministic",
        )
    )

    created = rest.handle(
        RestRequest(
            method="POST",
            path="/sessions",
            body=json.dumps({"title": "New analysis"}),
        )
    )
    session_id = created.body["session_id"]
    assert isinstance(session_id, str)
    listed = rest.handle(RestRequest(method="GET", path="/sessions"))
    renamed = rest.handle(
        RestRequest(
            method="PATCH",
            path=f"/sessions/{session_id}",
            body=json.dumps({"title": "Renamed analysis"}),
        )
    )

    assert created.status_code == 201
    assert listed.status_code == 200
    assert listed.body["sessions"] == [created.body]
    assert renamed.status_code == 200
    assert renamed.body["title"] == "Renamed analysis"


@pytest.mark.parametrize(
    ("method", "path", "body", "status_code"),
    [
        ("POST", "/sessions", "{", 400),
        ("POST", "/sessions", "[]", 422),
        ("POST", "/sessions", '{"title": 2}', 422),
        ("GET", "/sessions/missing", "", 404),
        ("PATCH", "/sessions/missing", "{}", 422),
        ("PATCH", "/sessions/missing", '{"title": "Valid"}', 404),
        ("GET", "/sessions/invalid!session", "", 422),
        ("PATCH", "/sessions/invalid!session", '{"title": "Valid"}', 422),
    ],
)
def test_rest_validates_session_metadata_requests(
    tmp_path: Path,
    method: str,
    path: str,
    body: str,
    status_code: int,
) -> None:
    rest = RestGateway(SessionGateway(project=ProjectContext(tmp_path), env={}))

    response = rest.handle(RestRequest(method=method, path=path, body=body))

    assert response.status_code == status_code


def _wait_for_marker(process: subprocess.Popen[str], marker: Path) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if marker.exists():
            return
        if process.poll() is not None:
            _, stderr = process.communicate(timeout=1)
            pytest.fail(f"writer exited before creating {marker.name}: {stderr}")
        time.sleep(0.01)
    pytest.fail(f"writer did not create {marker.name}")
