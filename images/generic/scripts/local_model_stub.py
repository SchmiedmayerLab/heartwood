#!/usr/bin/env python3
# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Deterministic loopback stub used by the generic image smoke test."""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import ClassVar


class LocalModelHandler(BaseHTTPRequestHandler):
    """Handle one content-free chat-completion request for the stub profile."""

    request_log: ClassVar[Path]

    def do_POST(self) -> None:
        """Return a deterministic stub response."""
        if self.path != "/v1/chat/completions":
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        payload = json.loads(body.decode("utf-8")) if body else {}
        messages = payload.get("messages", []) if isinstance(payload, dict) else []
        self.request_log.parent.mkdir(parents=True, exist_ok=True)
        with self.request_log.open("a", encoding="utf-8") as log_file:
            log_file.write(
                json.dumps(
                    {
                        "path": self.path,
                        "model": payload.get("model") if isinstance(payload, dict) else None,
                        "messages_count": len(messages) if isinstance(messages, list) else 0,
                    },
                    sort_keys=True,
                )
                + "\n"
            )
        response = {
            "id": "chatcmpl-heartwood-local-runtime",
            "object": "chat.completion",
            "model": "heartwood-local-runtime",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": "Synthetic local model response.",
                    },
                }
            ],
            "usage": {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
            },
        }
        encoded = json.dumps(response, sort_keys=True).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, _fmt: str, *_args: object) -> None:
        """Suppress default request logging."""


def main() -> int:
    """Run the loopback stub."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--request-log", type=Path, required=True)
    args = parser.parse_args()
    LocalModelHandler.request_log = args.request_log
    server = HTTPServer((args.host, args.port), LocalModelHandler)
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
