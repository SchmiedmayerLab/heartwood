# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""ASGI transport adapter for the session gateway."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, cast
from urllib.parse import parse_qs

from heartwood.gateway._gateway import SessionGateway
from heartwood.gateway._rest import RestGateway, RestRequest
from heartwood.session import SessionEvent

AsgiMessage = dict[str, object]
AsgiReceive = Callable[[], Awaitable[AsgiMessage]]
AsgiScope = Mapping[str, object]
AsgiSend = Callable[[AsgiMessage], Awaitable[None]]


class GatewayAsgiApp:
    """ASGI app exposing gateway commands over HTTP and events over WebSocket."""

    def __init__(self, gateway: SessionGateway) -> None:
        self.gateway = gateway
        self.rest = RestGateway(gateway)

    async def __call__(self, scope: AsgiScope, receive: AsgiReceive, send: AsgiSend) -> None:
        """Handle one ASGI connection."""
        scope_type = _scope_string(scope, "type")
        if scope_type == "lifespan":
            await self._handle_lifespan(receive, send)
        elif scope_type == "http":
            await self._handle_http(scope, receive, send)
        elif scope_type == "websocket":
            await self._handle_websocket(scope, receive, send)
        else:
            msg = f"unsupported ASGI scope type: {scope_type}"
            raise ValueError(msg)

    async def _handle_lifespan(self, receive: AsgiReceive, send: AsgiSend) -> None:
        while True:
            message = await receive()
            message_type = _message_type(message)
            if message_type == "lifespan.startup":
                self.gateway.start()
                await send({"type": "lifespan.startup.complete"})
            elif message_type == "lifespan.shutdown":
                self.gateway.stop()
                await send({"type": "lifespan.shutdown.complete"})
                return

    async def _handle_http(self, scope: AsgiScope, receive: AsgiReceive, send: AsgiSend) -> None:
        body = await _read_http_body(receive)
        response = self.rest.handle(
            RestRequest(
                method=_scope_string(scope, "method"),
                path=_path_with_query(scope),
                body=body.decode("utf-8"),
            )
        )
        await send(
            {
                "type": "http.response.start",
                "status": response.status_code,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": json.dumps(response.body, separators=(",", ":")).encode("utf-8"),
            }
        )

    async def _handle_websocket(
        self, scope: AsgiScope, receive: AsgiReceive, send: AsgiSend
    ) -> None:
        route = _session_events_route(_scope_string(scope, "path"))
        if route is None:
            await send({"type": "websocket.close", "code": 1008})
            return
        try:
            after = _optional_int(_query_values(scope).get("after", [None])[0])
        except ValueError:
            await send({"type": "websocket.close", "code": 1008})
            return

        await send({"type": "websocket.accept"})
        stream = self.gateway.websocket(session_id=route, after_sequence=after)
        await _send_websocket_events(send, stream.receive())

        while not stream.closed:
            next_events = cast(
                asyncio.Future[Any],
                asyncio.create_task(stream.receive_next()),
            )
            next_message = cast(asyncio.Future[Any], asyncio.ensure_future(receive()))
            tasks = {next_events, next_message}
            done, pending = await asyncio.wait(
                tasks,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if next_message in done:
                message = cast(AsgiMessage, next_message.result())
                next_events.cancel()
                await _drain_cancelled(next_events)
                if _message_type(message) == "websocket.disconnect":
                    stream.close()
                    return
            if next_events in done:
                events = cast(tuple[SessionEvent, ...], next_events.result())
                next_message.cancel()
                await _drain_cancelled(next_message)
                await _send_websocket_events(send, events)
            for task in pending:
                task.cancel()
                await _drain_cancelled(task)


async def _read_http_body(receive: AsgiReceive) -> bytes:
    chunks: list[bytes] = []
    more_body = True
    while more_body:
        message = await receive()
        body = message.get("body", b"")
        if isinstance(body, bytes):
            chunks.append(body)
        more_body = bool(message.get("more_body", False))
    return b"".join(chunks)


async def _send_websocket_events(send: AsgiSend, events: tuple[SessionEvent, ...]) -> None:
    if not events:
        return
    payload = {"events": [event.model_dump(mode="json") for event in events]}
    await send(
        {
            "type": "websocket.send",
            "text": json.dumps(payload, separators=(",", ":")),
        }
    )


async def _drain_cancelled(task: asyncio.Future[Any]) -> None:
    try:
        await task
    except asyncio.CancelledError:
        return


def _path_with_query(scope: AsgiScope) -> str:
    path = _scope_string(scope, "path")
    query_string = scope.get("query_string", b"")
    if not isinstance(query_string, bytes) or not query_string:
        return path
    return f"{path}?{query_string.decode('utf-8')}"


def _query_values(scope: AsgiScope) -> dict[str, list[str | None]]:
    query_string = scope.get("query_string", b"")
    if not isinstance(query_string, bytes):
        return {}
    return cast(dict[str, list[str | None]], parse_qs(query_string.decode("utf-8")))


def _session_events_route(path: str) -> str | None:
    parts = tuple(part for part in path.split("/") if part)
    if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "events":
        return parts[1]
    return None


def _optional_int(value: str | None) -> int | None:
    if value is None:
        return None
    return int(value)


def _scope_string(scope: AsgiScope, key: str) -> str:
    value = scope.get(key)
    if isinstance(value, str):
        return value
    return ""


def _message_type(message: Mapping[str, object]) -> str:
    value = message.get("type")
    if isinstance(value, str):
        return value
    return ""
