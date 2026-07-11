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
import os
import re
import shlex
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import ClassVar


def _tool_result_failed(messages: list[object]) -> bool:
    serialized = json.dumps(messages).lower()
    exit_codes = {
        int(code)
        for pattern in (r"exit code\s+(-?\d+)", r'"exit_code"\s*:\s*(-?\d+)')
        for code in re.findall(pattern, serialized)
    }
    return '"is_error": true' in serialized or any(code != 0 for code in exit_codes)


class LocalModelHandler(BaseHTTPRequestHandler):
    """Handle one content-free chat-completion request for the stub profile."""

    request_log: ClassVar[Path]

    def do_GET(self) -> None:
        """Return the model catalog used by every Heartwood client."""
        if self.path != "/v1/models":
            self.send_response(404)
            self.end_headers()
            return
        self._send_json(
            {
                "object": "list",
                "data": [
                    {
                        "id": "heartwood-local-runtime",
                        "object": "model",
                        "created": 0,
                        "owned_by": "heartwood",
                    }
                ],
            }
        )

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
        researcher_messages = [
            message
            for message in messages
            if isinstance(message, dict) and message.get("role") == "user"
        ]
        latest_researcher_message = researcher_messages[-1] if researcher_messages else {}
        serialized_researcher_message = json.dumps(latest_researcher_message).lower()
        latest_researcher_index = max(
            (
                index
                for index, candidate in enumerate(messages)
                if isinstance(candidate, dict) and candidate.get("role") == "user"
            ),
            default=-1,
        )
        tool_results = [
            message
            for index, message in enumerate(messages)
            if index > latest_researcher_index
            and isinstance(message, dict)
            and message.get("role") == "tool"
        ]
        has_tool_result = bool(tool_results)
        medium_risk = "medium-risk network check" in serialized_researcher_message
        task_kind = (
            "cohort"
            if "target-condition cohort" in serialized_researcher_message
            else "baseline"
            if "age-only baseline" in serialized_researcher_message
            else "export"
            if "aggregate export" in serialized_researcher_message
            else "failure"
            if "failing-action" in serialized_researcher_message
            else "generic"
        )
        message: dict[str, object]
        finish_reason: str
        if has_tool_result:
            final_messages = {
                "cohort": "The synthetic target-condition cohort summary is ready for review.",
                "baseline": "The training-only age baseline is ready for review.",
                "export": "The count-floor-controlled aggregate export is ready for review.",
                "failure": "The synthetic failure check unexpectedly succeeded.",
                "generic": "Synthetic local model response.",
            }
            message = {
                "role": "assistant",
                "content": (
                    "The requested tool action failed; review the terminal outcome before retrying."
                    if _tool_result_failed(tool_results)
                    else final_messages[task_kind]
                ),
            }
            finish_reason = "stop"
        else:
            runtime_root = Path(os.environ.get("HEARTWOOD_RUNTIME_ROOT", Path.cwd())).resolve()
            cohort_command = " ".join(
                (
                    shlex.quote(sys.executable),
                    shlex.quote(
                        str(runtime_root / "skills/verified/omop-cohort-summary/scripts/run.py")
                    ),
                    "--data-root",
                    "input",
                    "--target-condition-concept-id 201826",
                    "--minimum-age 18",
                    "--aggregate-count-floor 20",
                    "--output cohort-summary.json",
                )
            )
            baseline_command = " ".join(
                (
                    shlex.quote(sys.executable),
                    shlex.quote(
                        str(runtime_root / "skills/verified/baseline-model/scripts/run.py")
                    ),
                    "--data-root",
                    "input",
                    "--target-condition-concept-id 201826",
                    "--output baseline-model.json",
                )
            )
            export_command = " ".join(
                (
                    shlex.quote(sys.executable),
                    shlex.quote(
                        str(runtime_root / "skills/verified/aggregate-export/scripts/run.py")
                    ),
                    "--summary cohort-summary.json",
                    "--aggregate-count-floor 20",
                    "--output aggregate-export.json",
                )
            )
            commands = {
                "cohort": cohort_command,
                "baseline": baseline_command,
                "export": export_command,
                "failure": "false",
                "generic": (
                    'test -z "${HEARTWOOD_UNUSED_MODEL_API_KEY:-}" '
                    "&& printf heartwood-openhands-action"
                ),
            }
            call_ids = {
                "cohort": "call-heartwood-reference-analysis",
                "baseline": "call-heartwood-baseline-analysis",
                "export": "call-heartwood-aggregate-export",
                "failure": "call-heartwood-failing-action",
                "generic": "call-heartwood-offline-smoke",
            }
            summaries = {
                "cohort": "build the aggregate synthetic target-condition cohort",
                "baseline": "fit the training-only synthetic age baseline",
                "export": "apply the aggregate count floor and prepare the export",
                "failure": "run the failing synthetic command",
                "generic": "run a bounded offline smoke command",
            }
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": call_ids[task_kind],
                        "type": "function",
                        "function": {
                            "name": "terminal",
                            "arguments": json.dumps(
                                {
                                    "command": (
                                        "curl https://example.invalid"
                                        if medium_risk
                                        else commands[task_kind]
                                    ),
                                    "is_input": False,
                                    "reset": False,
                                    "security_risk": "LOW",
                                    "summary": (
                                        "run a medium-risk network command"
                                        if medium_risk
                                        else summaries[task_kind]
                                    ),
                                    "timeout": 10,
                                },
                                sort_keys=True,
                            ),
                        },
                    }
                ],
            }
            finish_reason = "tool_calls"
        response = {
            "id": "chatcmpl-heartwood-local-runtime",
            "object": "chat.completion",
            "model": "heartwood-local-runtime",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": finish_reason,
                    "message": message,
                }
            ],
            "usage": {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
            },
        }
        self._send_json(response)

    def _send_json(self, value: object) -> None:
        """Write one deterministic JSON response."""
        encoded = json.dumps(value, sort_keys=True).encode("utf-8")
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
