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
import socket
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


def _loopback_port(env_name: str, *, excluded: frozenset[int] = frozenset()) -> int:
    configured = os.environ.get(env_name)
    if configured is not None:
        port = int(configured)
        if port in excluded:
            raise ValueError(f"{env_name} duplicates another Terra demo port")
        return port
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind((GATEWAY_HOST, 0))
            port = int(listener.getsockname()[1])
        if port not in excluded:
            return port


GATEWAY_PORT = _loopback_port("HEARTWOOD_TERRA_DEMO_GATEWAY_PORT")
PROXY_PORT = _loopback_port("HEARTWOOD_TERRA_DEMO_PROXY_PORT", excluded=frozenset({GATEWAY_PORT}))
SERVICE_PREFIX = os.environ.get("HEARTWOOD_TERRA_DEMO_SERVICE_PREFIX", "/user/synthetic/")
SESSION_ID = os.environ.get("HEARTWOOD_TERRA_DEMO_SESSION_ID", "terra-demo-smoke")
PROJECT_ROOT = Path(
    os.environ.get("HEARTWOOD_TERRA_DEMO_PROJECT_ROOT", "/tmp/heartwood-terra-demo")
)
WEB_ROOT = Path(os.environ.get("HEARTWOOD_WEB_ROOT", "/opt/heartwood/packages/webui/dist"))
RUNTIME_ROOT = Path(os.environ.get("HEARTWOOD_RUNTIME_ROOT", "/opt/heartwood"))
VERBOSE = os.environ.get("HEARTWOOD_TERRA_DEMO_VERBOSE") == "1"
REQUEST_TIMEOUT = float(os.environ.get("HEARTWOOD_TERRA_DEMO_REQUEST_TIMEOUT", "30"))
STARTUP_TIMEOUT = float(os.environ.get("HEARTWOOD_TERRA_DEMO_STARTUP_TIMEOUT", "60"))


def main() -> int:
    """Run the runtime Terra/Jupyter smoke."""
    if not (WEB_ROOT / "index.html").exists():
        raise SystemExit(f"web UI assets not found: {WEB_ROOT}")

    shutil.rmtree(PROJECT_ROOT, ignore_errors=True)
    input_root = PROJECT_ROOT / "input"
    input_root.mkdir(parents=True, exist_ok=True)
    fixture_root = RUNTIME_ROOT / "fixtures" / "synthetic" / "omop-like"
    for filename in ("person.csv", "condition_occurrence.csv"):
        source = fixture_root / filename
        shutil.copy2(source, input_root / source.name)
    os.chdir(PROJECT_ROOT)
    subprocess.run(
        (
            "heartwood",
            "setup",
            "--model-source",
            "local",
            "--model-id",
            "heartwood-local-runtime",
            "--non-interactive",
            "--yes",
        ),
        check=True,
        cwd=PROJECT_ROOT,
        stdout=subprocess.DEVNULL,
    )

    gateway = _start_gateway()
    proxy: ThreadingHTTPServer | None = None
    try:
        _trace("waiting for gateway")
        _wait_for_url(_gateway_url("/"), process=gateway)
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
        cwd=PROJECT_ROOT,
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

        def do_PUT(self) -> None:
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

            connection = HTTPConnection(GATEWAY_HOST, GATEWAY_PORT, timeout=REQUEST_TIMEOUT)
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
    _trace("reading project readiness")
    readiness = _request_json(urllib.parse.urljoin(external_base, "project/readiness"))
    if Path(str(readiness.get("project_root"))).resolve() != PROJECT_ROOT.resolve():
        raise AssertionError("gateway proxy did not preserve the current-directory project")

    _trace("reading action settings")
    actions = _request_json(urllib.parse.urljoin(external_base, "settings/actions"))
    if actions.get("confirmation_mode") != "always-confirm":
        raise AssertionError("gateway did not expose the default action confirmation mode")
    _trace("persisting action settings")
    selected_actions = _request_json(
        urllib.parse.urljoin(external_base, "settings/actions/confirmation"),
        data={"mode": "always-confirm"},
        method="PUT",
    )
    if selected_actions.get("confirmation_mode") != "always-confirm":
        raise AssertionError("gateway did not persist action confirmation through the proxy")

    _trace("submitting detection command")
    response = _request_json(
        urllib.parse.urljoin(external_base, f"sessions/{SESSION_ID}/commands"),
        data=_gateway_command("detect", "terra-demo-smoke-detect"),
    )
    event_kinds = {event["kind"] for event in response["events"]}
    if "detection.proposed" not in event_kinds:
        raise AssertionError("gateway command route did not return detection events")

    _trace("replaying session events")
    replay = _request_json(
        urllib.parse.urljoin(external_base, f"sessions/{SESSION_ID}/events?after=0")
    )
    if not any(event["sequence"] == 1 for event in replay["events"]):
        raise AssertionError("gateway replay route did not return persisted events")

    _trace("streaming session events")
    stream = _request_sse(
        urllib.parse.urljoin(external_base, f"sessions/{SESSION_ID}/events/stream?after=0")
    )
    if "event: heartwood-session-events" not in stream or "detection.proposed" not in stream:
        raise AssertionError("gateway SSE route did not stream persisted events")

    _trace("submitting OpenHands chat command")
    task = _request_json(
        urllib.parse.urljoin(external_base, f"sessions/{SESSION_ID}/commands"),
        data=_gateway_command(
            "chat",
            "terra-demo-smoke-chat",
            {
                "prompt": (
                    "Build the synthetic target-condition cohort for concept 201826 with the "
                    "repository-verified cohort Skill. Use the localized OMOP reference tables, "
                    "minimum age 18, aggregate count floor 20, and write cohort-summary.json."
                )
            },
        ),
    )
    task_kinds = {event["kind"] for event in task["events"]}
    if "confirmation.requested" not in task_kinds:
        raise AssertionError("gateway chat did not return an OpenHands confirmation")

    _trace("approving OpenHands tool call")
    allowed = _request_json(
        urllib.parse.urljoin(external_base, f"sessions/{SESSION_ID}/commands"),
        data=_gateway_command(
            "approve",
            "terra-demo-smoke-allow",
            {
                "target_id": "call-heartwood-reference-analysis",
                "target_type": "tool-call",
            },
        ),
    )
    allowed_kinds = {event["kind"] for event in allowed["events"]}
    if not {"confirmation.resolved", "tool.execution.recorded"}.issubset(allowed_kinds):
        raise AssertionError(
            f"gateway allow did not execute the pending OpenHands action: {sorted(allowed_kinds)}"
        )
    artifact = PROJECT_ROOT / "cohort-summary.json"
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    summary = payload["summary"]
    if (
        summary.get("source_condition_occurrence_count") != 39
        or summary.get("participant_count") != 20
        or summary.get("condition_occurrence_count") != 35
    ):
        raise AssertionError(f"gateway produced an unexpected reference cohort: {summary}")


def _verify_notebook_api() -> None:
    env = {"JUPYTERHUB_SERVICE_PREFIX": SERVICE_PREFIX}
    expected_proxy_url = f"{_normalize_prefix(SERVICE_PREFIX).rstrip('/')}/proxy/{GATEWAY_PORT}/"
    if jupyter_proxy_url(port=GATEWAY_PORT, env=env) != expected_proxy_url:
        raise AssertionError("notebook proxy URL did not match Terra-style service prefix")

    session = NotebookSession(session_id=f"{SESSION_ID}-notebook")
    if session.project.root != PROJECT_ROOT.resolve():
        raise AssertionError("notebook API did not preserve the current-directory project")
    view_model = session.detect()
    if not any(item.kind == "detection.proposed" for item in view_model.activity):
        raise AssertionError("notebook API did not project detection events")


def _wait_for_url(url: str, *, process: subprocess.Popen[str] | None = None) -> None:
    deadline = time.time() + STARTUP_TIMEOUT
    last_error: BaseException | None = None
    while time.time() < deadline:
        if process is not None and process.poll() is not None:
            raise RuntimeError(f"gateway exited before becoming ready: {process.returncode}")
        try:
            _request_text(url, timeout=1)
            return
        except (OSError, urllib.error.URLError) as error:
            last_error = error
            time.sleep(0.1)
    raise RuntimeError(f"server did not become ready: {last_error}")


def _request_text(url: str, *, timeout: float = REQUEST_TIMEOUT) -> str:
    request = urllib.request.Request(url, headers={"Connection": "close"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8")


def _request_json(
    url: str,
    *,
    data: dict[str, object] | None = None,
    method: str | None = None,
) -> dict[str, object]:
    encoded = None if data is None else json.dumps(data).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=encoded,
        headers={"Connection": "close", "Content-Type": "application/json"},
        method=method or ("POST" if encoded is not None else "GET"),
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def _request_sse(url: str) -> str:
    request = urllib.request.Request(url, headers={"Connection": "close"})
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        buffer = ""
        while "\n\n" not in buffer and "\r\n\r\n" not in buffer:
            chunk = response.read(1)
            if not chunk:
                break
            buffer += chunk.decode("utf-8")
        return buffer


def _gateway_command(
    kind: str,
    command_id: str,
    payload: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "actor_id": "synthetic-user",
        "command_id": command_id,
        "created_at": "2026-01-01T00:00:00Z",
        "kind": kind,
        "payload": {} if payload is None else payload,
        "schema_version": "heartwood.session-command.v1",
        "session_id": SESSION_ID,
    }


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
