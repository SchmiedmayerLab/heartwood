# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""ASGI transport adapter for the session gateway."""

from __future__ import annotations

import asyncio
import html
import json
import mimetypes
from collections.abc import Awaitable, Callable, Mapping
from functools import partial
from pathlib import Path
from typing import Literal, cast
from urllib.parse import parse_qs

from heartwood.gateway._diagnostics import diagnostic_for
from heartwood.gateway._gateway import SessionGateway
from heartwood.gateway._ingress import IngressPolicy, IngressRequestError
from heartwood.gateway._rest import RestGateway, RestRequest
from heartwood.session import JsonValue, SessionEvent, validate_session_id

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
        ingress: IngressPolicy | None = None,
    ) -> None:
        self.gateway = gateway
        self.rest = RestGateway(gateway)
        self.static_dir = static_dir
        self.ingress = IngressPolicy.create() if ingress is None else ingress

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
        try:
            request = self.ingress.validate_scope(scope)
        except IngressRequestError as error:
            diagnostic = diagnostic_for("gateway-request")
            await _send_json_response(
                send,
                status_code=error.status_code,
                body={
                    "code": diagnostic.code,
                    "error": str(error),
                },
            )
            return
        route = _session_events_stream_route(request.path)
        if route is not None and _scope_string(scope, "method") == "GET":
            try:
                route = validate_session_id(route)
            except ValueError as error:
                await _send_json_response(send, status_code=422, body={"error": str(error)})
                return
            try:
                after = _optional_int(_query_values(request.query_string).get("after", [None])[0])
            except ValueError:
                await _send_json_response(send, status_code=400, body={"error": "invalid after"})
                return
            await self._handle_sse(
                session_id=route, after_sequence=after, receive=receive, send=send
            )
            return

        body = await _read_http_body(receive)
        response = await asyncio.to_thread(
            self.rest.handle,
            RestRequest(
                method=_scope_string(scope, "method"),
                path=_path_with_query(
                    path=request.path,
                    query_string=request.query_string,
                ),
                body=body.decode("utf-8"),
            ),
        )
        if response.status_code != 404 or _is_gateway_api_path(request.path):
            await _send_json_response(send, status_code=response.status_code, body=response.body)
            return

        if self.static_dir is not None and _scope_string(scope, "method") == "GET":
            await _send_static_response(
                send,
                static_dir=self.static_dir,
                path=request.path,
                browser_base_path=self.ingress.browser_base_path,
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
        stream, snapshot = await asyncio.to_thread(
            partial(
                self.gateway.open_event_stream,
                session_id=session_id,
                after_sequence=after_sequence,
            )
        )
        try:
            last_sequence = snapshot.projection.revision
            await _send_sse_events(
                send,
                snapshot.events,
                projection=snapshot.projection.safe_dict(),
            )

            while not stream.closed:
                signal = await _wait_for_stream_signal(
                    stream.receive_next,
                    receive,
                    disconnect_type="http.disconnect",
                )
                if signal == "disconnect":
                    return
                if signal == "update":
                    snapshot = await asyncio.to_thread(
                        partial(
                            self.gateway.session_snapshot,
                            session_id=session_id,
                            after_sequence=last_sequence,
                        )
                    )
                    last_sequence = snapshot.projection.revision
                    await _send_sse_events(
                        send,
                        snapshot.events,
                        projection=snapshot.projection.safe_dict(),
                    )
        finally:
            stream.close()

    async def _handle_websocket(
        self, scope: AsgiScope, receive: AsgiReceive, send: AsgiSend
    ) -> None:
        try:
            request = self.ingress.validate_scope(scope, websocket=True)
        except IngressRequestError:
            await send({"type": "websocket.close", "code": 1008})
            return
        route = _session_events_route(request.path)
        if route is None:
            await send({"type": "websocket.close", "code": 1008})
            return
        try:
            route = validate_session_id(route)
            after = _optional_int(_query_values(request.query_string).get("after", [None])[0])
        except ValueError:
            await send({"type": "websocket.close", "code": 1008})
            return

        await send({"type": "websocket.accept"})
        stream, snapshot = await asyncio.to_thread(
            partial(
                self.gateway.open_event_stream,
                session_id=route,
                after_sequence=after,
            )
        )
        try:
            last_sequence = snapshot.projection.revision
            await _send_websocket_events(
                send,
                snapshot.events,
                projection=snapshot.projection.safe_dict(),
            )

            while not stream.closed:
                signal = await _wait_for_stream_signal(
                    stream.receive_next,
                    receive,
                    disconnect_type="websocket.disconnect",
                )
                if signal == "disconnect":
                    return
                if signal == "update":
                    snapshot = await asyncio.to_thread(
                        partial(
                            self.gateway.session_snapshot,
                            session_id=route,
                            after_sequence=last_sequence,
                        )
                    )
                    last_sequence = snapshot.projection.revision
                    await _send_websocket_events(
                        send,
                        snapshot.events,
                        projection=snapshot.projection.safe_dict(),
                    )
        finally:
            stream.close()


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


async def _send_websocket_events(
    send: AsgiSend,
    events: tuple[SessionEvent, ...],
    *,
    projection: Mapping[str, object],
) -> None:
    payload = {
        "events": [event.model_dump(mode="json") for event in events],
        "projection": projection,
    }
    await send(
        {
            "type": "websocket.send",
            "text": json.dumps(payload, separators=(",", ":")),
        }
    )


async def _send_sse_events(
    send: AsgiSend,
    events: tuple[SessionEvent, ...],
    *,
    projection: Mapping[str, object],
) -> None:
    payload = {
        "events": [event.model_dump(mode="json") for event in events],
        "projection": projection,
    }
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
            "headers": [
                (b"content-type", b"application/json"),
                (b"cache-control", b"no-store"),
                (b"x-content-type-options", b"nosniff"),
            ],
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
    path: str,
    browser_base_path: str,
) -> None:
    resolved = _static_file_path(static_dir, path=path)
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
    if resolved.name == "index.html":
        body = _inject_browser_base_path(body, browser_base_path=browser_base_path)
    await send({"type": "http.response.body", "body": body})


async def _wait_for_stream_signal(
    receive_update: Callable[[], Awaitable[object]],
    receive_message: AsgiReceive,
    *,
    disconnect_type: str,
) -> Literal["disconnect", "message", "update"]:
    update_task: asyncio.Future[object] = asyncio.ensure_future(receive_update())
    message_task: asyncio.Future[AsgiMessage] = asyncio.ensure_future(receive_message())
    tasks = (update_task, message_task)
    try:
        done, _pending = await asyncio.wait(
            tasks,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if message_task in done:
            message = message_task.result()
            if _message_type(message) == disconnect_type:
                return "disconnect"
        if update_task in done:
            update_task.result()
            return "update"
        return "message"
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _path_with_query(*, path: str, query_string: str) -> str:
    if not query_string:
        return path
    return f"{path}?{query_string}"


def _query_values(query_string: str) -> dict[str, list[str | None]]:
    return cast(dict[str, list[str | None]], parse_qs(query_string))


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
    path: str,
) -> Path | None:
    root = static_dir.resolve()
    relative = path.lstrip("/")
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


def _is_gateway_api_path(path: str) -> bool:
    return path.startswith(("/sessions/", "/settings/")) or path in {
        "/sessions",
        "/settings",
    }


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


def _inject_browser_base_path(body: bytes, *, browser_base_path: str) -> bytes:
    """Inject the one gateway-owned browser route into the built index."""
    try:
        document = body.decode("utf-8")
    except UnicodeDecodeError:
        return body
    marker = '<meta name="heartwood-gateway-base"'
    if marker in document:
        return body
    escaped = html.escape(browser_base_path, quote=True)
    metadata = f'<meta name="heartwood-gateway-base" content="{escaped}" />'
    if "<head>" not in document:
        return body
    return document.replace("<head>", f"<head>\n    {metadata}", 1).encode("utf-8")
