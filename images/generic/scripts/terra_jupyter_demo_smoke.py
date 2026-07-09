#!/usr/bin/env python
#
# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Smoke test the packaged web UI through a Terra-style Jupyter proxy."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http import HTTPStatus
from http.client import HTTPConnection, IncompleteRead
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

from heartwood.notebook import NotebookSession, jupyter_proxy_url

GATEWAY_HOST = "127.0.0.1"
GATEWAY_PORT = int(os.environ.get("HEARTWOOD_TERRA_DEMO_GATEWAY_PORT", "8767"))
PROXY_PORT = int(os.environ.get("HEARTWOOD_TERRA_DEMO_PROXY_PORT", "8768"))
SERVICE_PREFIX = os.environ.get("HEARTWOOD_TERRA_DEMO_SERVICE_PREFIX", "/user/synthetic/")
SESSION_ID = os.environ.get("HEARTWOOD_TERRA_DEMO_SESSION_ID", "terra-demo-smoke")
WORKSPACE = Path(os.environ.get("HEARTWOOD_TERRA_DEMO_WORKSPACE", "/tmp/heartwood-terra-demo"))
WEB_ROOT = Path(os.environ.get("HEARTWOOD_WEB_ROOT", "/opt/heartwood/packages/webui/dist"))
VERBOSE = os.environ.get("HEARTWOOD_TERRA_DEMO_VERBOSE") == "1"


def main() -> int:
    """Run the runtime Terra/Jupyter smoke."""
    if not (WEB_ROOT / "index.html").exists():
        raise SystemExit(f"web UI assets not found: {WEB_ROOT}")

    shutil.rmtree(WORKSPACE, ignore_errors=True)
    WORKSPACE.mkdir(parents=True, exist_ok=True)

    gateway = _start_gateway()
    proxy: ThreadingHTTPServer | None = None
    try:
        _trace("waiting for gateway")
        _wait_for_url(_gateway_url("/"))
        _trace("starting proxy")
        proxy = _start_proxy()
        external_base = _external_base_url()
        _trace("waiting for external proxy route")
        _wait_for_url(external_base)
        _trace("verifying web UI")
        _verify_web_ui(external_base)
        _trace("verifying gateway session routes")
        _verify_gateway_session(external_base)
        _trace("verifying notebook API")
        _verify_notebook_api()
    finally:
        _trace("cleaning up")
        if proxy is not None:
            proxy.shutdown()
            proxy.server_close()
        gateway.terminate()
        try:
            gateway.wait(timeout=5)
        except subprocess.TimeoutExpired:
            gateway.kill()
            gateway.wait(timeout=5)
    print("Terra-style Jupyter demo smoke: ok")
    return 0


def _start_gateway() -> subprocess.Popen[str]:
    return subprocess.Popen(
        [
            "heartwood",
            "--workspace",
            str(WORKSPACE),
            "serve",
            "--host",
            GATEWAY_HOST,
            "--port",
            str(GATEWAY_PORT),
            "--web-root",
            str(WEB_ROOT),
            "--base-path",
            "/",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _start_proxy() -> ThreadingHTTPServer:
    external_base_path = _external_base_path()

    class TerraProxyHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "HeartwoodTerraProxySmoke/1"
        hop_by_hop_headers: ClassVar[set[str]] = {
            "connection",
            "content-encoding",
            "content-length",
            "keep-alive",
            "proxy-authenticate",
            "proxy-authorization",
            "te",
            "trailer",
            "transfer-encoding",
            "upgrade",
        }

        def log_message(self, _format: str, *_args: object) -> None:
            return

        def do_GET(self) -> None:
            self._proxy()

        def do_POST(self) -> None:
            self._proxy()

        def _proxy(self) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            if not parsed.path.startswith(external_base_path):
                self.send_error(HTTPStatus.NOT_FOUND)
                return

            target_path = parsed.path[len(external_base_path) - 1 :] or "/"
            if parsed.query:
                target_path = f"{target_path}?{parsed.query}"

            body = None
            if self.command in {"POST", "PUT", "PATCH"}:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)

            connection = HTTPConnection(GATEWAY_HOST, GATEWAY_PORT, timeout=10)
            try:
                headers = {
                    key: value
                    for key, value in self.headers.items()
                    if key.lower() not in self.hop_by_hop_headers
                }
                headers["Host"] = f"{GATEWAY_HOST}:{GATEWAY_PORT}"
                connection.request(self.command, target_path, body=body, headers=headers)
                upstream = connection.getresponse()
                self.send_response(upstream.status, upstream.reason)
                self.send_header(
                    "Content-Type",
                    _safe_content_type(upstream.getheader("Content-Type")),
                )
                self.send_header("Connection", "close")
                self.end_headers()
                content_type = upstream.getheader("Content-Type", "")
                chunk_size = 1 if content_type.startswith("text/event-stream") else 4096
                while True:
                    try:
                        chunk = upstream.read(chunk_size)
                    except IncompleteRead:
                        break
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        break
            finally:
                connection.close()

    server = ThreadingHTTPServer((GATEWAY_HOST, PROXY_PORT), TerraProxyHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _verify_web_ui(external_base: str) -> None:
    html = _request_text(external_base)
    if '<div id="root"></div>' not in html:
        raise AssertionError("web UI root element missing")
    asset_path = _first_asset_path(html)
    asset = _request_text(urllib.parse.urljoin(external_base, asset_path))
    if not asset:
        raise AssertionError("web UI asset was empty")


def _verify_gateway_session(external_base: str) -> None:
    command = {
        "actor_id": "synthetic-user",
        "command_id": "terra-demo-smoke-detect",
        "created_at": "2026-01-01T00:00:00Z",
        "kind": "detect",
        "payload": {},
        "schema_version": "heartwood.session-command.v1",
        "session_id": SESSION_ID,
    }
    response = _request_json(
        urllib.parse.urljoin(external_base, f"sessions/{SESSION_ID}/commands"),
        data=command,
    )
    event_kinds = {event["kind"] for event in response["events"]}
    if "detection.proposed" not in event_kinds:
        raise AssertionError("gateway command route did not return detection events")

    replay = _request_json(
        urllib.parse.urljoin(external_base, f"sessions/{SESSION_ID}/events?after=0")
    )
    if not any(event["sequence"] == 1 for event in replay["events"]):
        raise AssertionError("gateway replay route did not return persisted events")

    stream = _request_sse(
        urllib.parse.urljoin(external_base, f"sessions/{SESSION_ID}/events/stream?after=0")
    )
    if "event: heartwood-session-events" not in stream or "detection.proposed" not in stream:
        raise AssertionError("gateway SSE route did not stream persisted events")


def _verify_notebook_api() -> None:
    env = {"JUPYTERHUB_SERVICE_PREFIX": SERVICE_PREFIX}
    expected_proxy_url = f"{_normalize_prefix(SERVICE_PREFIX).rstrip('/')}/proxy/{GATEWAY_PORT}/"
    if jupyter_proxy_url(port=GATEWAY_PORT, env=env) != expected_proxy_url:
        raise AssertionError("notebook proxy URL did not match Terra-style service prefix")

    session = NotebookSession(workspace=WORKSPACE, session_id=f"{SESSION_ID}-notebook")
    view_model = session.detect()
    if not any(item.kind == "detection.proposed" for item in view_model.activity):
        raise AssertionError("notebook API did not project detection events")


def _wait_for_url(url: str) -> None:
    deadline = time.time() + 20
    last_error: BaseException | None = None
    while time.time() < deadline:
        try:
            _request_text(url, timeout=1)
            return
        except (OSError, urllib.error.URLError) as error:
            last_error = error
            time.sleep(0.1)
    raise RuntimeError(f"server did not become ready: {last_error}")


def _request_text(url: str, *, timeout: float = 5) -> str:
    request = urllib.request.Request(url, headers={"Connection": "close"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8")


def _request_json(url: str, *, data: dict[str, object] | None = None) -> dict[str, object]:
    encoded = None if data is None else json.dumps(data).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=encoded,
        headers={"Connection": "close", "Content-Type": "application/json"},
        method="POST" if encoded is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def _request_sse(url: str) -> str:
    request = urllib.request.Request(url, headers={"Connection": "close"})
    with urllib.request.urlopen(request, timeout=5) as response:
        buffer = ""
        while "\n\n" not in buffer and "\r\n\r\n" not in buffer:
            chunk = response.read(1)
            if not chunk:
                break
            buffer += chunk.decode("utf-8")
        return buffer


def _first_asset_path(html: str) -> str:
    for marker in ('src="./', 'href="./'):
        start = html.find(marker)
        if start == -1:
            continue
        start += len(marker)
        end = html.find('"', start)
        if end != -1:
            return html[start:end]
    raise AssertionError("web UI index did not reference a built asset")


def _external_base_url() -> str:
    return f"http://{GATEWAY_HOST}:{PROXY_PORT}{_external_base_path()}"


def _gateway_url(path: str) -> str:
    return f"http://{GATEWAY_HOST}:{GATEWAY_PORT}{path}"


def _external_base_path() -> str:
    return f"{_normalize_prefix(SERVICE_PREFIX)}proxy/{GATEWAY_PORT}/"


def _normalize_prefix(prefix: str) -> str:
    normalized = prefix if prefix.startswith("/") else f"/{prefix}"
    return normalized if normalized.endswith("/") else f"{normalized}/"


def _safe_content_type(content_type: str | None) -> str:
    if content_type is None:
        return "application/octet-stream"
    content_type_lower = content_type.lower()
    if content_type_lower.startswith("text/event-stream"):
        return "text/event-stream"
    if content_type_lower.startswith("text/html"):
        return "text/html; charset=utf-8"
    if content_type_lower.startswith("application/json"):
        return "application/json"
    if content_type_lower.startswith("application/javascript") or content_type_lower.startswith(
        "text/javascript"
    ):
        return "application/javascript"
    if content_type_lower.startswith("text/css"):
        return "text/css"
    return "application/octet-stream"


def _trace(message: str) -> None:
    if VERBOSE:
        print(message)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AssertionError, RuntimeError, TimeoutError) as error:
        print(f"Terra-style Jupyter demo smoke failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
