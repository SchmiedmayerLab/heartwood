# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Replayable event streams for gateway clients."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Iterable
from threading import Lock

from heartwood.session import SessionEvent


class GatewayEventStream:
    """In-process WebSocket-style event stream with replay support."""

    def __init__(
        self,
        *,
        session_id: str,
        initial_events: Iterable[SessionEvent] = (),
        initial_changed: bool = True,
    ) -> None:
        self.session_id = session_id
        self._pending = list(initial_events)
        self._closed = False
        self._changed = initial_changed
        self._lock = Lock()
        self._ready = asyncio.Event()
        try:
            self._loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

    @property
    def closed(self) -> bool:
        """Return whether the stream is closed."""
        with self._lock:
            return self._closed

    def push(self, events: Iterable[SessionEvent]) -> None:
        """Push events to the stream."""
        event_tuple = tuple(events)
        with self._lock:
            if self._closed or not event_tuple:
                return
            self._pending.extend(event_tuple)
            self._changed = True
        self._signal()

    def notify(self) -> None:
        """Wake the stream because its transient projection changed."""
        with self._lock:
            if self._closed:
                return
            self._changed = True
        self._signal()

    def receive(self) -> tuple[SessionEvent, ...]:
        """Drain currently available events."""
        with self._lock:
            return self._receive_locked()

    async def receive_next(self) -> tuple[SessionEvent, ...]:
        """Wait for and drain the next available event batch."""
        while True:
            with self._lock:
                if self._pending or self._changed or self._closed:
                    return self._receive_locked()
                self._ready.clear()
            await self._ready.wait()

    def close(self) -> None:
        """Close the stream."""
        with self._lock:
            self._closed = True
            self._pending.clear()
        self._signal()

    def _receive_locked(self) -> tuple[SessionEvent, ...]:
        events = tuple(self._pending)
        self._pending.clear()
        self._changed = False
        self._ready.clear()
        return events

    def _signal(self) -> None:
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(self._ready.set)
        else:
            self._ready.set()


class EventStreamHub:
    """Track active event streams by session id."""

    def __init__(self) -> None:
        self._streams: dict[str, list[GatewayEventStream]] = defaultdict(list)

    def connect(
        self,
        *,
        session_id: str,
        replay_events: Iterable[SessionEvent] = (),
        initial_changed: bool = True,
    ) -> GatewayEventStream:
        """Connect a stream and seed it with replay events."""
        stream = GatewayEventStream(
            session_id=session_id,
            initial_events=replay_events,
            initial_changed=initial_changed,
        )
        self._streams[session_id].append(stream)
        return stream

    def publish(self, *, session_id: str, events: Iterable[SessionEvent]) -> None:
        """Publish events to active streams."""
        event_tuple = tuple(events)
        streams = self._streams.get(session_id, [])
        active_streams: list[GatewayEventStream] = []
        for stream in streams:
            if stream.closed:
                continue
            stream.push(event_tuple)
            active_streams.append(stream)
        self._streams[session_id] = active_streams

    def notify(self, *, session_id: str) -> None:
        """Wake active streams after a transient projection update."""
        streams = self._streams.get(session_id, [])
        active_streams: list[GatewayEventStream] = []
        for stream in streams:
            if stream.closed:
                continue
            stream.notify()
            active_streams.append(stream)
        self._streams[session_id] = active_streams
