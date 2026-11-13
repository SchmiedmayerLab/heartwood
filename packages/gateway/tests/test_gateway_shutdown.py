# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Shutdown closes gateway admission before draining accepted work."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from heartwood.gateway import (
    GatewayClosedError,
    ProjectContext,
    RestGateway,
    RestRequest,
    SessionGateway,
)
from heartwood.gateway._session_catalog import SessionSummary
from heartwood.session import CommandKind, SessionCommand


def test_stopped_gateway_cannot_create_a_session(tmp_path: Path) -> None:
    gateway = SessionGateway(project=ProjectContext(tmp_path), env={}, backend_id="deterministic")
    gateway.stop()
    with pytest.raises(RuntimeError, match="shutting down or stopped"):
        gateway.create_session("After shutdown")
    assert not gateway.project.state_root.exists()


def test_stopped_gateway_returns_unavailable_to_rest_clients(tmp_path: Path) -> None:
    gateway = SessionGateway(project=ProjectContext(tmp_path), env={}, backend_id="deterministic")
    gateway.stop()
    response = RestGateway(gateway).handle(RestRequest(method="POST", path="/sessions/default"))
    assert response.status_code == 503
    assert not gateway.project.state_root.exists()


def test_shutdown_drains_accepted_calls_and_rejects_new_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = SessionGateway(project=ProjectContext(tmp_path), env={}, backend_id="deterministic")
    entered, release, stopping = Event(), Event(), Event()
    request_stop = gateway.local_model_manager.request_stop

    def stop_downloads() -> None:
        request_stop()
        stopping.set()

    def create(title: str | None = None) -> SessionSummary:
        del title
        entered.set()
        assert release.wait(timeout=5)
        gateway.platform_capabilities()
        return gateway.session_catalog.default()

    monkeypatch.setattr(gateway.session_catalog, "create", create)
    monkeypatch.setattr(gateway.local_model_manager, "request_stop", stop_downloads)
    with ThreadPoolExecutor(max_workers=2) as executor:
        operation = executor.submit(gateway.create_session)
        assert entered.wait(timeout=5)
        shutdown = executor.submit(gateway.stop)
        try:
            assert stopping.wait(timeout=5)
            assert not shutdown.done()
            with pytest.raises(RuntimeError, match="shutting down or stopped"):
                gateway.sessions()
        finally:
            release.set()
        assert operation.result(timeout=5)["session_id"] == "session-main"
        shutdown.result(timeout=5)
    assert gateway.project.state_exists()


def test_every_public_gateway_entry_rejects_calls_after_shutdown(tmp_path: Path) -> None:
    gateway = SessionGateway(project=ProjectContext(tmp_path), env={}, backend_id="deterministic")
    gateway.stop()
    for name, method in vars(SessionGateway).items():
        if name.startswith("_") or name == "stop" or not callable(method):
            continue
        with pytest.raises(GatewayClosedError, match="shutting down or stopped"):
            # Admission must reject even a malformed call before it reaches an entry point.
            getattr(gateway, name)()
    gateway.stop()
    assert not gateway.project.state_root.exists()


def test_failed_call_releases_admission_and_shutdown_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = SessionGateway(project=ProjectContext(tmp_path), env={}, backend_id="deterministic")

    def fail(title: str | None = None) -> SessionSummary:
        del title
        raise ValueError("synthetic request failure")

    monkeypatch.setattr(gateway.session_catalog, "create", fail)
    with pytest.raises(ValueError, match="synthetic request failure"):
        gateway.create_session()
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(gateway.stop)
        second = executor.submit(gateway.stop)
        first.result(timeout=5)
        second.result(timeout=5)
    with pytest.raises(GatewayClosedError):
        gateway.create_session()


def test_stop_inside_a_call_fails_without_deadlock_or_closing_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = SessionGateway(project=ProjectContext(tmp_path), env={}, backend_id="deterministic")

    def stop_inside(title: str | None = None) -> SessionSummary:
        del title
        gateway.stop()
        pytest.fail("stopping inside an active call must fail")

    monkeypatch.setattr(gateway.session_catalog, "create", stop_inside)
    with pytest.raises(RuntimeError, match="inside an active gateway call"):
        gateway.create_session()
    assert gateway.default_session()["session_id"] == "session-main"
    gateway.stop()


def test_shutdown_timeout_retains_session_ownership_and_credentials_until_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = SessionGateway(project=ProjectContext(tmp_path), env={}, backend_id="deterministic")
    gateway.handle(
        SessionCommand(
            command_id="synthetic-pause",
            session_id="session-existing",
            kind=CommandKind.PAUSE,
            actor_id="human",
            created_at="2026-01-01T00:00:00Z",
        )
    )
    service = gateway._services["session-existing"]
    gateway.credential_store.save("SYNTHETIC_TOKEN", "synthetic-value")
    entered, release = Event(), Event()

    def create(title: str | None = None) -> SessionSummary:
        del title
        entered.set()
        assert release.wait(timeout=10)
        gateway.platform_capabilities()
        return gateway.session_catalog.default()

    monkeypatch.setattr(gateway.session_catalog, "create", create)
    with ThreadPoolExecutor(max_workers=1) as executor:
        operation = executor.submit(gateway.create_session)
        assert entered.wait(timeout=5)
        try:
            with pytest.raises(TimeoutError, match="calls are still active"):
                gateway.stop()
            assert bool(service.store.owns_writer)
            assert gateway.credential_store.resolve("SYNTHETIC_TOKEN") == "synthetic-value"
            with pytest.raises(GatewayClosedError):
                gateway.default_session()
        finally:
            release.set()
        operation.result(timeout=5)
    gateway.stop()
    assert not service.store.owns_writer
    assert gateway.credential_store.resolve("SYNTHETIC_TOKEN") is None


def test_shutdown_closes_all_owned_event_streams(tmp_path: Path) -> None:
    gateway = SessionGateway(project=ProjectContext(tmp_path), env={}, backend_id="deterministic")
    first = gateway.websocket(session_id="session-one")
    second, _snapshot = gateway.open_event_stream(session_id="session-two")
    gateway.stop()
    assert first.closed
    assert second.closed
    assert gateway._streams.active_stream_count(session_id="session-one") == 0
    assert gateway._streams.active_stream_count(session_id="session-two") == 0
