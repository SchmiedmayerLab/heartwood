# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from uuid import uuid4

import pytest
from openhands.sdk import Agent, RemoteConversation
from openhands.sdk.testing import TestLLM

from heartwood.gateway._remote_workspace import AdmittedRemoteWorkspace, WorkerResponseError


def test_native_attach_bounds_repeating_history_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = Agent(llm=TestLLM(model="test-model"), tools=[])
    identity = uuid4()
    requests = []
    metadata = {
        "id": str(identity),
        "agent": agent.model_dump(mode="json"),
        "max_iterations": 10,
        "execution_status": "idle",
        "tool_module_qualnames": {},
    }
    monkeypatch.setenv("OPENHANDS_REMOTE_WS_READY_TIMEOUT", "0.05")
    monkeypatch.setenv("OPENHANDS_REMOTE_WS_READY_REQUIRED", "false")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append(self.path)
            body = metadata
            if "/events/search" in self.path:
                body = {"items": [], "next_page_id": "repeated" if len(requests) < 1200 else None}
            encoded = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, _format: str, *_args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    workspace = AdmittedRemoteWorkspace(
        host=f"http://127.0.0.1:{server.server_port}", working_dir="/workspace"
    )
    conversation = None
    try:
        with pytest.raises(WorkerResponseError, match="budget exhausted"):
            conversation = RemoteConversation.attach(workspace, identity, visualizer=None)
        assert len(requests) == 1024
        assert any("page_id=repeated" in path for path in requests)
        workspace.reset_client()
        with pytest.raises(WorkerResponseError, match="closed"):
            RemoteConversation.attach(workspace, identity, visualizer=None)
        assert len(requests) == 1024
    finally:
        if conversation is not None:
            conversation.close()
        workspace.reset_client()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()
