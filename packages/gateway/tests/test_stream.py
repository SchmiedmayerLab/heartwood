# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Tests for durable and transient gateway stream wakeups."""

from __future__ import annotations

import asyncio
from pathlib import Path
from threading import Timer

from heartwood.core_adapter import BackendLifecycle, BackendLifecycleEvent
from heartwood.gateway import GatewayEventStream, ProjectContext, SessionGateway
from heartwood.gateway._stream import EventStreamHub
from heartwood.session import EventKind, SessionEvent


def test_cross_thread_projection_notification_wakes_exactly_once() -> None:
    async def scenario() -> None:
        stream = GatewayEventStream(session_id="session-1")
        assert stream.receive() == ()

        notification = Timer(0.02, stream.notify)
        notification.start()
        try:
            assert await asyncio.wait_for(stream.receive_next(), timeout=1) == ()
        finally:
            notification.cancel()

        next_receive = asyncio.create_task(stream.receive_next())
        await asyncio.sleep(0.02)
        assert not next_receive.done()

        stream.close()
        assert await asyncio.wait_for(next_receive, timeout=1) == ()

    asyncio.run(scenario())


def test_closed_streams_ignore_updates_and_are_removed_from_the_hub() -> None:
    hub = EventStreamHub()
    closed = hub.connect(session_id="session-1")
    active = hub.connect(session_id="session-1")
    assert closed.receive() == ()
    assert active.receive() == ()
    closed.close()

    event = SessionEvent(
        event_id="event-1",
        session_id="session-1",
        sequence=0,
        kind=EventKind.AGENT_LIFECYCLE_UPDATED,
        occurred_at="2026-01-01T00:00:00Z",
        payload={"status": "running"},
    )
    closed.push((event,))
    closed.notify()
    hub.publish(session_id="session-1", events=(event,))
    assert closed.receive() == ()
    assert active.receive() == (event,)

    hub.notify(session_id="session-1")
    assert active.receive() == ()
    active.close()
    hub.notify(session_id="session-1")


def test_token_deltas_are_transient_and_clear_at_a_stable_boundary(tmp_path: Path) -> None:
    gateway = SessionGateway(
        project=ProjectContext(tmp_path),
        env={},
        backend_id="deterministic",
    )
    gateway.project.initialize()
    service = gateway._service("session-1")
    service._accept_backend_events(
        (
            BackendLifecycleEvent(
                lifecycle=BackendLifecycle.RUNNING,
                source_event_id="synthetic-running",
            ),
        )
    )
    gateway._publish_token_delta(session_id="session-1", delta="Working")

    streaming = gateway.session_projection(session_id="session-1")
    assert streaming.streaming_text == "Working"
    assert streaming.stream_revision == 1

    service._accept_backend_events(
        (
            BackendLifecycleEvent(
                lifecycle=BackendLifecycle.FINISHED,
                source_event_id="synthetic-finished",
            ),
        )
    )

    finished = gateway.session_projection(session_id="session-1")
    assert finished.streaming_text == ""
    assert finished.stream_revision == 2
    gateway._publish_token_delta(session_id="session-1", delta="late")
    assert gateway.session_projection(session_id="session-1") == finished
    gateway.stop()
    restarted = SessionGateway(
        project=ProjectContext(tmp_path),
        env={},
        backend_id="deterministic",
    )
    assert restarted.session_projection(session_id="session-1").streaming_text == ""
