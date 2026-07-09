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

from heartwood.session import SessionEvent


class GatewayEventStream:
    """In-process WebSocket-style event stream with replay support."""

    def __init__(self, *, session_id: str, initial_events: Iterable[SessionEvent] = ()) -> None:
        self.session_id = session_id
        self._pending = list(initial_events)
        self._closed = False
        self._ready = asyncio.Event()
        if self._pending:
            self._ready.set()

    @property
    def closed(self) -> bool:
        """Return whether the stream is closed."""
        return self._closed

    def push(self, events: Iterable[SessionEvent]) -> None:
        """Push events to the stream."""
        event_tuple = tuple(events)
        if not self._closed and event_tuple:
            self._pending.extend(event_tuple)
            self._ready.set()

    def receive(self) -> tuple[SessionEvent, ...]:
        """Drain currently available events."""
        events = tuple(self._pending)
        self._pending.clear()
        if not self._pending:
            self._ready.clear()
        return events

    async def receive_next(self) -> tuple[SessionEvent, ...]:
        """Wait for and drain the next available event batch."""
        while not self._pending and not self._closed:
            await self._ready.wait()
        return self.receive()

    def close(self) -> None:
        """Close the stream."""
        self._closed = True
        self._pending.clear()
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
    ) -> GatewayEventStream:
        """Connect a stream and seed it with replay events."""
        stream = GatewayEventStream(session_id=session_id, initial_events=replay_events)
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
