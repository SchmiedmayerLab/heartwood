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


def _message_text(message: dict[str, object]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def _terminal_call(
    call_id: str,
    command: str,
    summary: str,
    *,
    security_risk: str = "LOW",
) -> dict[str, object]:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": "terminal",
            "arguments": json.dumps(
                {
                    "command": command,
                    "is_input": False,
                    "reset": False,
                    "security_risk": security_risk,
                    "summary": summary,
                    "timeout": 10,
                },
                sort_keys=True,
            ),
        },
    }


def _prompt_terminal_call(command: str, summary: str, security_risk: str = "LOW") -> str:
    return "\n".join(
        (
            "<function=terminal>",
            f"<parameter=command>{command}</parameter>",
            f"<parameter=security_risk>{security_risk}</parameter>",
            f"<parameter=summary>{summary}</parameter>",
            "</function>",
        )
    )


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
                        "id": "heartwood-managed-runtime",
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
        serialized_messages = json.dumps(messages).lower()
        native_tool_mode = bool(payload.get("tools"))
        prompt_tool_mode = "<function=example_function_name>" in serialized_messages
        researcher_messages = [
            message
            for message in messages
            if isinstance(message, dict) and message.get("role") == "user"
        ]
        latest_researcher_message = researcher_messages[-1] if researcher_messages else {}
        task_message = next(
            (
                message
                for message in reversed(researcher_messages)
                if not _message_text(message).lower().lstrip().startswith("execution result of [")
            ),
            {},
        )
        serialized_task_message = json.dumps(task_message).lower()
        task_index = max(
            (index for index, message in enumerate(messages) if message is task_message),
            default=-1,
        )
        native_tool_results = [
            message
            for index, message in enumerate(messages)
            if index > task_index and isinstance(message, dict) and message.get("role") == "tool"
        ]
        prompt_tool_results = (
            [latest_researcher_message]
            if _message_text(latest_researcher_message)
            .lower()
            .lstrip()
            .startswith("execution result of [")
            else []
        )
        tool_results = [*native_tool_results, *prompt_tool_results]
        has_tool_result = bool(tool_results)
        medium_risk = "medium-risk network check" in serialized_task_message
        task_kind = (
            "cohort"
            if "target-condition cohort" in serialized_task_message
            else "baseline"
            if "age-only baseline" in serialized_task_message
            else "export"
            if "aggregate export" in serialized_task_message
            else "failure"
            if "failing-action" in serialized_task_message
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
                "generic": "Synthetic Heartwood-managed model response.",
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
        elif native_tool_mode or prompt_tool_mode:
            runtime_root = os.environ.get("HEARTWOOD_RUNTIME_ROOT") or None
            tool_python = os.environ.get("HEARTWOOD_TOOL_PYTHON") or sys.executable
            script_root = (
                '"$HEARTWOOD_RUNTIME_ROOT"/skills/verified'
                if runtime_root is not None
                else shlex.quote(str(Path.cwd() / "skills" / "verified"))
            )
            cohort_command = " ".join(
                (
                    shlex.quote(tool_python),
                    f"{script_root}/omop-cohort-summary/scripts/run.py",
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
                    shlex.quote(tool_python),
                    f"{script_root}/baseline-model/scripts/run.py",
                    "--data-root",
                    "input",
                    "--target-condition-concept-id 201826",
                    "--output baseline-model.json",
                )
            )
            export_command = " ".join(
                (
                    shlex.quote(tool_python),
                    f"{script_root}/aggregate-export/scripts/run.py",
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
            command = "curl https://example.invalid" if medium_risk else commands[task_kind]
            summary = "run a medium-risk network command" if medium_risk else summaries[task_kind]
            risk = "MEDIUM" if medium_risk else "LOW"
            if native_tool_mode:
                tool_calls = [
                    _terminal_call(
                        call_ids[task_kind],
                        command,
                        summary,
                        security_risk=risk,
                    )
                ]
                if task_kind == "cohort" and not medium_risk:
                    tool_calls.append(
                        _terminal_call(
                            "call-heartwood-reference-analysis-read",
                            "cat cohort-summary.json",
                            "read the generated aggregate cohort summary",
                        )
                    )
                message = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": tool_calls,
                }
                finish_reason = "tool_calls"
            else:
                message = {
                    "role": "assistant",
                    "content": _prompt_terminal_call(command, summary, risk),
                }
                finish_reason = "stop"
        else:
            message = {
                "role": "assistant",
                "content": "Synthetic Heartwood-managed model response.",
            }
            finish_reason = "stop"
        response = {
            "id": "chatcmpl-heartwood-managed-runtime",
            "object": "chat.completion",
            "model": "heartwood-managed-runtime",
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
