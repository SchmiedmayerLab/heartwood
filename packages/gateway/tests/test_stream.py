# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Tests for durable and transient gateway stream wakeups."""

from __future__ import annotations

import asyncio
from pathlib import Path
from threading import Event, Thread

from heartwood.core_adapter import BackendLifecycle, BackendLifecycleEvent
from heartwood.gateway import GatewayEventStream, ProjectContext, SessionGateway
from heartwood.gateway._stream import EventStreamHub
from heartwood.session import EventKind, SessionEvent


def test_cross_thread_projection_notification_wakes_exactly_once() -> None:
    stream = GatewayEventStream(session_id="session-1")

    async def scenario() -> None:
        assert stream.receive() == ()

        next_notification = asyncio.create_task(stream.receive_next())
        await asyncio.sleep(0)
        assert stream._loop is asyncio.get_running_loop()
        notification = Thread(target=stream.notify)
        notification.start()
        assert await asyncio.wait_for(next_notification, timeout=1) == ()
        notification.join()

        next_receive = asyncio.create_task(stream.receive_next())
        await asyncio.sleep(0)
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
    assert hub.active_stream_count(session_id="session-1") == 2
    closed.close()
    assert hub.active_stream_count(session_id="session-1") == 1

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
    assert hub.active_stream_count(session_id="session-1") == 0
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
    assert gateway.persisted_session_projection(session_id="session-1").streaming_text == "Working"

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
    assert gateway.persisted_session_projection(session_id="session-1").streaming_text == ""
    gateway._publish_token_delta(session_id="session-1", delta="late")
    assert gateway.session_projection(session_id="session-1") == finished
    gateway.stop()
    restarted = SessionGateway(
        project=ProjectContext(tmp_path),
        env={},
        backend_id="deterministic",
    )
    assert restarted.session_projection(session_id="session-1").streaming_text == ""


def test_transient_streams_are_isolated_and_discarded_after_worker_restart(
    tmp_path: Path,
) -> None:
    gateway = SessionGateway(
        project=ProjectContext(tmp_path),
        env={},
        backend_id="deterministic",
    )
    gateway.project.initialize()
    first = gateway._service("session-1")
    second = gateway._service("session-2")
    for index, service in enumerate((first, second), start=1):
        service._accept_backend_events(
            (
                BackendLifecycleEvent(
                    lifecycle=BackendLifecycle.RUNNING,
                    source_event_id=f"synthetic-running-{index}",
                ),
            )
        )
    gateway._publish_token_delta(session_id="session-1", delta="first partial")
    gateway._publish_token_delta(session_id="session-2", delta="second partial")

    first_projection = gateway.session_projection(session_id="session-1")
    second_projection = gateway.session_projection(session_id="session-2")

    assert first_projection.streaming_text == "first partial"
    assert second_projection.streaming_text == "second partial"
    assert first_projection.stream_epoch == second_projection.stream_epoch
    previous_epoch = first_projection.stream_epoch
    gateway.stop()

    restarted = SessionGateway(
        project=ProjectContext(tmp_path),
        env={},
        backend_id="deterministic",
    )
    restarted_first = restarted.session_projection(session_id="session-1")
    restarted_second = restarted.session_projection(session_id="session-2")

    assert restarted_first.streaming_text == ""
    assert restarted_second.streaming_text == ""
    assert restarted_first.lifecycle.status == "running"
    assert restarted_second.lifecycle.status == "running"
    assert restarted_first.stream_epoch == restarted_second.stream_epoch
    assert restarted_first.stream_epoch != previous_epoch
    restarted.stop()


def test_delayed_completion_cannot_clear_a_newer_run_stream(
    tmp_path: Path,
) -> None:
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
                source_event_id="synthetic-running-old",
            ),
        )
    )
    gateway._publish_token_delta(session_id="session-1", delta="old partial")
    original_sink = service._event_sink
    delayed_sink_entered = Event()
    release_delayed_sink = Event()
    delay_first_publish = True

    def delayed_sink(events: tuple[SessionEvent, ...]) -> None:
        nonlocal delay_first_publish
        if delay_first_publish:
            delay_first_publish = False
            delayed_sink_entered.set()
            assert release_delayed_sink.wait(timeout=2)
        original_sink(events)

    service._event_sink = delayed_sink
    completion = Thread(
        target=service._accept_backend_events,
        args=(
            (
                BackendLifecycleEvent(
                    lifecycle=BackendLifecycle.FINISHED,
                    source_event_id="synthetic-finished-old",
                ),
            ),
        ),
        daemon=True,
    )
    completion.start()
    assert delayed_sink_entered.wait(timeout=2)

    completed = gateway.session_projection(session_id="session-1")
    assert completed.lifecycle.status == "finished"
    assert completed.streaming_text == ""
    service._accept_backend_events(
        (
            BackendLifecycleEvent(
                lifecycle=BackendLifecycle.RUNNING,
                source_event_id="synthetic-running-new",
            ),
        )
    )
    gateway._publish_token_delta(session_id="session-1", delta="new partial")

    release_delayed_sink.set()
    completion.join(timeout=2)
    assert not completion.is_alive()
    current = gateway.session_projection(session_id="session-1")
    assert current.lifecycle.status == "running"
    assert current.streaming_text == "new partial"
    gateway.stop()


def test_out_of_order_sink_callbacks_publish_one_contiguous_sequence(
    tmp_path: Path,
) -> None:
    gateway = SessionGateway(
        project=ProjectContext(tmp_path),
        env={},
        backend_id="deterministic",
    )
    stream = gateway.websocket(session_id="session-1")
    assert stream.receive() == ()
    running = SessionEvent(
        event_id="running",
        session_id="session-1",
        sequence=0,
        kind=EventKind.AGENT_LIFECYCLE_UPDATED,
        occurred_at="2026-01-01T00:00:00Z",
        payload={"status": "running"},
    )
    finished = SessionEvent(
        event_id="finished",
        session_id="session-1",
        sequence=1,
        kind=EventKind.AGENT_LIFECYCLE_UPDATED,
        occurred_at="2026-01-01T00:00:01Z",
        payload={"status": "finished"},
    )

    assert (
        gateway._publish_committed_events(
            session_id="session-1",
            events=(finished,),
        )
        == ()
    )
    assert stream.receive() == ()

    assert gateway._publish_committed_events(
        session_id="session-1",
        events=(running,),
    ) == (running, finished)
    assert stream.receive() == (running, finished)

    assert (
        gateway._publish_committed_events(
            session_id="session-1",
            events=(finished,),
        )
        == ()
    )
    assert stream.receive() == ()
    gateway.stop()
