# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""ASGI transport adapter for the session gateway."""

from __future__ import annotations

import asyncio
import json
import mimetypes
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs

from heartwood.gateway._gateway import SessionGateway
from heartwood.gateway._rest import RestGateway, RestRequest
from heartwood.session import JsonValue, SessionEvent

AsgiMessage = dict[str, object]
AsgiReceive = Callable[[], Awaitable[AsgiMessage]]
AsgiScope = Mapping[str, object]
AsgiSend = Callable[[AsgiMessage], Awaitable[None]]


class GatewayAsgiApp:
    """ASGI app exposing gateway commands over HTTP and events over WebSocket."""

    def __init__(
        self,
        gateway: SessionGateway,
        *,
        static_dir: Path | None = None,
        static_base_path: str = "/",
    ) -> None:
        self.gateway = gateway
        self.rest = RestGateway(gateway)
        self.static_dir = static_dir
        self.static_base_path = _normalize_base_path(static_base_path)

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
        raw_path = _scope_string(scope, "path")
        gateway_path = _gateway_path(raw_path, static_base_path=self.static_base_path)
        route = None if gateway_path is None else _session_events_stream_route(gateway_path)
        if route is not None and _scope_string(scope, "method") == "GET":
            try:
                after = _optional_int(_query_values(scope).get("after", [None])[0])
            except ValueError:
                await _send_json_response(send, status_code=400, body={"error": "invalid after"})
                return
            await self._handle_sse(
                session_id=route, after_sequence=after, receive=receive, send=send
            )
            return

        if gateway_path is not None:
            body = await _read_http_body(receive)
            response = self.rest.handle(
                RestRequest(
                    method=_scope_string(scope, "method"),
                    path=_path_with_query(scope, path=gateway_path),
                    body=body.decode("utf-8"),
                )
            )
            if response.status_code != 404 or gateway_path.startswith("/sessions/"):
                await _send_json_response(
                    send, status_code=response.status_code, body=response.body
                )
                return

        if self.static_dir is not None and _scope_string(scope, "method") == "GET":
            await _send_static_response(
                send,
                static_dir=self.static_dir,
                static_base_path=self.static_base_path,
                path=raw_path,
            )
            return
        await _send_json_response(send, status_code=404, body={"error": "unknown gateway route"})

    async def _handle_sse(
        self,
        *,
        session_id: str,
        after_sequence: int | None,
        receive: AsgiReceive,
        send: AsgiSend,
    ) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"text/event-stream"),
                    (b"cache-control", b"no-store"),
                    (b"x-accel-buffering", b"no"),
                ],
            }
        )
        stream = self.gateway.websocket(session_id=session_id, after_sequence=after_sequence)
        await _send_sse_events(send, stream.receive())

        while not stream.closed:
            next_events = cast(asyncio.Future[Any], asyncio.create_task(stream.receive_next()))
            next_message = cast(asyncio.Future[Any], asyncio.ensure_future(receive()))
            done, pending = await asyncio.wait(
                {next_events, next_message},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if next_message in done:
                message = cast(AsgiMessage, next_message.result())
                next_events.cancel()
                await _drain_cancelled(next_events)
                if _message_type(message) == "http.disconnect":
                    stream.close()
                    return
            if next_events in done:
                events = cast(tuple[SessionEvent, ...], next_events.result())
                next_message.cancel()
                await _drain_cancelled(next_message)
                await _send_sse_events(send, events)
            for task in pending:
                task.cancel()
                await _drain_cancelled(task)

    async def _handle_websocket(
        self, scope: AsgiScope, receive: AsgiReceive, send: AsgiSend
    ) -> None:
        gateway_path = _gateway_path(
            _scope_string(scope, "path"),
            static_base_path=self.static_base_path,
        )
        route = None if gateway_path is None else _session_events_route(gateway_path)
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


async def _send_sse_events(send: AsgiSend, events: tuple[SessionEvent, ...]) -> None:
    payload = {"events": [event.model_dump(mode="json") for event in events]}
    body = (
        f"event: heartwood-session-events\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"
    ).encode()
    await send({"type": "http.response.body", "body": body, "more_body": True})


async def _send_json_response(
    send: AsgiSend,
    *,
    status_code: int,
    body: Mapping[str, JsonValue],
) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status_code,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send(
        {
            "type": "http.response.body",
            "body": json.dumps(body, separators=(",", ":")).encode("utf-8"),
        }
    )


async def _send_static_response(
    send: AsgiSend,
    *,
    static_dir: Path,
    static_base_path: str,
    path: str,
) -> None:
    resolved = _static_file_path(static_dir, static_base_path=static_base_path, path=path)
    if resolved is None:
        await _send_json_response(send, status_code=404, body={"error": "static asset not found"})
        return
    content_type = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", content_type.encode("ascii")),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    body = await asyncio.to_thread(resolved.read_bytes)
    await send({"type": "http.response.body", "body": body})


async def _drain_cancelled(task: asyncio.Future[Any]) -> None:
    try:
        await task
    except asyncio.CancelledError:
        return


def _path_with_query(scope: AsgiScope, *, path: str | None = None) -> str:
    path = _scope_string(scope, "path") if path is None else path
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


def _session_events_stream_route(path: str) -> str | None:
    parts = tuple(part for part in path.split("/") if part)
    if len(parts) == 4 and parts[0] == "sessions" and parts[2] == "events" and parts[3] == "stream":
        return parts[1]
    return None


def _static_file_path(
    static_dir: Path,
    *,
    static_base_path: str,
    path: str,
) -> Path | None:
    root = static_dir.resolve()
    stripped = _strip_static_base(path, static_base_path=static_base_path)
    if stripped is None:
        return None
    relative = stripped.lstrip("/")
    if not relative:
        relative = "index.html"
    candidate = (root / relative).resolve()
    if root != candidate and root not in candidate.parents:
        return None
    if candidate.is_file():
        return candidate
    if Path(relative).suffix:
        return None
    fallback = (root / "index.html").resolve()
    if fallback.is_file():
        return fallback
    return None


def _strip_static_base(path: str, *, static_base_path: str) -> str | None:
    if static_base_path == "/":
        return path
    if path == static_base_path:
        return "/"
    prefix = f"{static_base_path}/"
    if path.startswith(prefix):
        return "/" + path[len(prefix) :]
    return None


def _gateway_path(path: str, *, static_base_path: str) -> str | None:
    if path.startswith("/sessions/"):
        return path
    return _strip_static_base(path, static_base_path=static_base_path)


def _normalize_base_path(value: str) -> str:
    if not value or value == "/":
        return "/"
    normalized = value if value.startswith("/") else f"/{value}"
    return normalized.rstrip("/")


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
