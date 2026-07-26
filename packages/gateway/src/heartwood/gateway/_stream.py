# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Replayable event streams for gateway clients."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Callable, Iterable
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
        on_close: Callable[[GatewayEventStream], None] | None = None,
    ) -> None:
        self.session_id = session_id
        self._pending = list(initial_events)
        self._closed = False
        self._changed = initial_changed
        self._on_close = on_close
        self._lock = Lock()
        self._ready: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

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
        loop = asyncio.get_running_loop()
        while True:
            with self._lock:
                if self._loop is None:
                    self._loop = loop
                    self._ready = asyncio.Event()
                elif self._loop is not loop:
                    raise RuntimeError("gateway event stream cannot move between event loops")
                if self._pending or self._changed or self._closed:
                    return self._receive_locked()
                ready = self._ready
                if ready is None:  # pragma: no cover - initialized with the loop above
                    raise RuntimeError("gateway event stream signal is unavailable")
                ready.clear()
            await ready.wait()

    def close(self) -> None:
        """Close the stream."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._pending.clear()
        self._signal()
        if self._on_close is not None:
            self._on_close(self)

    def _receive_locked(self) -> tuple[SessionEvent, ...]:
        events = tuple(self._pending)
        self._pending.clear()
        self._changed = False
        return events

    def _signal(self) -> None:
        with self._lock:
            loop = self._loop
            ready = self._ready
        if loop is not None and ready is not None and loop.is_running():
            try:
                loop.call_soon_threadsafe(ready.set)
            except RuntimeError:
                return


class EventStreamHub:
    """Track active event streams by session id."""

    def __init__(self) -> None:
        self._streams: dict[str, list[GatewayEventStream]] = defaultdict(list)
        self._lock = Lock()

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
            on_close=self._remove,
        )
        with self._lock:
            self._streams[session_id].append(stream)
        return stream

    def publish(self, *, session_id: str, events: Iterable[SessionEvent]) -> None:
        """Publish events to active streams."""
        event_tuple = tuple(events)
        with self._lock:
            streams = tuple(self._streams.get(session_id, ()))
        for stream in streams:
            stream.push(event_tuple)

    def notify(self, *, session_id: str) -> None:
        """Wake active streams after a transient projection update."""
        with self._lock:
            streams = tuple(self._streams.get(session_id, ()))
        for stream in streams:
            stream.notify()

    def active_stream_count(self, *, session_id: str) -> int:
        """Return the number of streams retained for one session."""
        with self._lock:
            return len(self._streams.get(session_id, ()))

    def _remove(self, stream: GatewayEventStream) -> None:
        with self._lock:
            remaining = [
                candidate
                for candidate in self._streams.get(stream.session_id, ())
                if candidate is not stream
            ]
            if remaining:
                self._streams[stream.session_id] = remaining
            else:
                self._streams.pop(stream.session_id, None)
