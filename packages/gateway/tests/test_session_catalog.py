# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Tests for gateway-owned session lifecycle metadata."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from heartwood.core_adapter import FileSessionStore
from heartwood.gateway import (
    ProjectContext,
    RestGateway,
    RestRequest,
    SessionCatalog,
    SessionCatalogError,
    SessionGateway,
    SessionNotFoundError,
)
from heartwood.session import EventKind, JsonValue, SessionEvent


def _event(sequence: int, kind: EventKind, **payload: JsonValue) -> SessionEvent:
    return SessionEvent(
        event_id=f"event-{sequence}",
        session_id="legacy-session",
        sequence=sequence,
        kind=kind,
        occurred_at=f"2026-01-01T00:00:{sequence:02d}Z",
        payload=payload,
    )


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


def test_catalog_discovers_legacy_event_stores_and_derives_waiting_state(tmp_path: Path) -> None:
    store = FileSessionStore(tmp_path, "legacy-session")
    store.append_event(_event(0, EventKind.COMMAND_RECEIVED, command_id="command-0"))
    store.append_event(
        _event(
            1,
            EventKind.CONFIRMATION_REQUESTED,
            request={"tool_call_id": "tool-1"},
        )
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
    catalog = SessionCatalog(tmp_path)
    store.append_event(_event(0, EventKind.SESSION_PAUSED))
    assert catalog.get("legacy-session").status == "paused"

    store.append_event(_event(1, EventKind.SESSION_RESUMED))
    assert catalog.get("legacy-session").status == "idle"

    store.append_event(_event(2, EventKind.ERROR_RECORDED, reason="synthetic"))
    assert catalog.get("legacy-session").status == "error"

    store.append_event(_event(3, EventKind.COMMAND_RECEIVED, command_id="retry"))
    assert catalog.get("legacy-session").status == "idle"


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

    assert len(closed_descriptors) == 1
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
