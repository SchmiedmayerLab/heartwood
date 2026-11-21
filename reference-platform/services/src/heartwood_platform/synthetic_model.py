# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""A deterministic model behind LiteLLM, for tests on a local cluster.

python -m heartwood_platform.synthetic_model CONFIGURATION

It serves the OpenAI chat completions and model list routes over HTTPS and accepts only
its API key. Its answers depend only on the request: when the request offers the tools
``terminal`` and ``finish``, the first answer after the last user message calls
``terminal`` with a fixed command, and the next one calls ``finish``; otherwise it
answers with a fixed text. A user message that contains ``synthetic:interrupt`` makes it
close the connection in the middle of its answer. Every answer reports the same usage,
so LiteLLM records the same cost for every request.
"""

from __future__ import annotations

import hmac
import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from heartwood_platform.common.serving import TlsSettings, serve

COMMAND = "printf synthetic-route > /workspace/synthetic-route.txt"
FINISH = "The synthetic model route completed its task."
TEXT = "This is the synthetic model."
INTERRUPT = "synthetic:interrupt"
USAGE = {"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500}
_MAX_BODY = 4 * 1024 * 1024


class Settings(BaseModel):
    """The configuration file of the synthetic model."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    models: tuple[str, ...] = Field(min_length=1)
    api_key_file: Path
    tls: TlsSettings


class _InterruptedAnswerError(Exception):
    """The answer is cut off on purpose."""


def _step(messages: list[Any]) -> int:
    """Return how many tool calls the model made since the last user message."""
    count = 0
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "user":
            count = 0
        elif message.get("role") == "assistant" and message.get("tool_calls"):
            count += 1
    return count


def _interrupted(messages: list[Any]) -> bool:
    return any(
        isinstance(message, dict)
        and message.get("role") == "user"
        and INTERRUPT in json.dumps(message.get("content"))
        for message in messages
    )


def answer(body: dict[str, Any]) -> dict[str, Any]:
    """Return the assistant message of the answer to a chat completions request."""
    messages = body.get("messages")
    if not isinstance(messages, list):
        messages = []
    tools = {
        tool.get("function", {}).get("name")
        for tool in body.get("tools") or []
        if isinstance(tool, dict)
    }
    if {"terminal", "finish"} <= tools:
        step = _step(messages)
        name, arguments = (
            ("terminal", {"command": COMMAND}) if step == 0 else ("finish", {"message": FINISH})
        )
        call = {
            "id": f"synthetic-{step}",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }
        return {"role": "assistant", "content": "", "tool_calls": [call]}
    return {"role": "assistant", "content": TEXT}


def create_app(settings: Settings) -> Starlette:
    """Build the application of the synthetic model."""
    key = settings.api_key_file.read_text(encoding="utf-8").strip()

    def authorized(request: Request) -> bool:
        scheme, _, given = request.headers.get("Authorization", "").partition(" ")
        return scheme.lower() == "bearer" and hmac.compare_digest(given.encode(), key.encode())

    def refused() -> JSONResponse:
        return JSONResponse(
            {"error": {"message": "Invalid API key", "type": "authentication_error"}},
            status_code=401,
        )

    def models(request: Request) -> Response:
        if not authorized(request):
            return refused()
        return JSONResponse(
            {
                "object": "list",
                "data": [
                    {"id": name, "object": "model", "created": 0, "owned_by": "synthetic"}
                    for name in settings.models
                ],
            }
        )

    async def completions(request: Request) -> Response:
        if not authorized(request):
            return refused()
        raw = await request.body()
        if len(raw) > _MAX_BODY:
            return JSONResponse({"error": {"message": "request too large"}}, status_code=413)
        try:
            body = json.loads(raw)
        except ValueError:
            body = None
        if not isinstance(body, dict) or body.get("model") not in settings.models:
            return JSONResponse({"error": {"message": "unknown model"}}, status_code=404)
        message = answer(body)
        common = {"id": "synthetic", "created": 0, "model": body["model"]}
        reason = "tool_calls" if message.get("tool_calls") else "stop"
        interrupted = _interrupted(body["messages"])
        if not body.get("stream"):
            document = json.dumps(
                {
                    **common,
                    "object": "chat.completion",
                    "choices": [{"index": 0, "message": message, "finish_reason": reason}],
                    "usage": USAGE,
                }
            )
            if not interrupted:
                return Response(document, media_type="application/json")

            async def partial() -> AsyncIterator[str]:
                yield document[: len(document) // 2]
                raise _InterruptedAnswerError

            return StreamingResponse(partial(), media_type="application/json")

        async def chunks() -> AsyncIterator[str]:
            delta = dict(message)
            if "tool_calls" in delta:
                delta["tool_calls"] = [{"index": 0, **delta["tool_calls"][0]}]
            first = {
                **common,
                "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
            }
            yield f"data: {json.dumps(first)}\n\n"
            if interrupted:
                raise _InterruptedAnswerError
            last = {
                **common,
                "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": {}, "finish_reason": reason}],
                "usage": USAGE,
            }
            yield f"data: {json.dumps(last)}\n\ndata: [DONE]\n\n"

        return StreamingResponse(chunks(), media_type="text/event-stream")

    def health(_: Request) -> Response:
        return JSONResponse({"status": "ok"})

    return Starlette(
        routes=[
            Route("/healthz", health),
            Route("/v1/models", models),
            Route("/v1/chat/completions", completions, methods=["POST"]),
        ]
    )


def main() -> None:
    """Serve the synthetic model with the configuration file named on the command line."""
    settings = Settings.model_validate_json(Path(sys.argv[1]).read_text(encoding="utf-8"))
    serve(create_app(settings), settings.tls)


if __name__ == "__main__":
    main()
