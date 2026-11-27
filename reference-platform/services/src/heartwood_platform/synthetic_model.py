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

A user message that contains ``synthetic:script`` followed by a JSON array of
``[tool, arguments]`` pairs replaces the fixed calls: the n-th answer with a tool call
after the last such message makes the n-th call of its array. A tool is named exactly, or by the
name of an MCP tool that the gateway offers as ``<server>__<name>_<digest>``, with
the hyphens of the server identifier written as underscores. When the
array is used up, or names a tool that the request does not offer, the model answers
with the fixed text.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
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
SCRIPT = "synthetic:script"
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


def _text(content: object) -> str:
    """Return the text of a message's content, a string or a list of parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            part["text"]
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    return ""


def _script(messages: list[Any]) -> tuple[list[Any], int] | None:
    """Return the script of the last user message that holds one, and its next step.

    The step counts the answers with a tool call after that message.
    """
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        text = _text(message.get("content"))
        _, found, rest = text.partition(SCRIPT)
        if not found:
            continue
        try:
            script, _ = json.JSONDecoder().raw_decode(rest.lstrip())
        except ValueError:
            return None
        if not isinstance(script, list):
            return None
        step = sum(
            1
            for later in messages[index + 1 :]
            if isinstance(later, dict)
            and later.get("role") == "assistant"
            and later.get("tool_calls")
        )
        return script, step
    return None


def _offered(name: str, tools: set[str]) -> str | None:
    """Return the offered tool of a script step: its exact name or the one MCP tool of it."""
    if name in tools:
        return name
    pattern = re.compile(rf"[a-z0-9_]+__{re.escape(name)}_[0-9a-f]{{8}}")
    found = [tool for tool in tools if pattern.fullmatch(tool)]
    return found[0] if len(found) == 1 else None


def _call(messages: list[Any], name: str, arguments: object) -> dict[str, Any]:
    # The identifier counts every earlier tool call of the conversation, so no two calls of
    # one conversation share it.
    earlier = sum(
        len(message.get("tool_calls") or [])
        for message in messages
        if isinstance(message, dict) and message.get("role") == "assistant"
    )
    call = {
        "id": f"synthetic-{earlier}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }
    return {"role": "assistant", "content": "", "tool_calls": [call]}


def _interruption(messages: list[Any]) -> str | None:
    """Identify a deliberate truncation without including message content in the error."""
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        text = _text(message.get("content"))
        if INTERRUPT in text:
            marker = re.search(r"synthetic:interrupt:([a-f0-9]{32})\b", text)
            if marker:
                return "interrupt-" + hashlib.sha256(marker[1].encode("ascii")).hexdigest()
            return ""
    return None


def answer(body: dict[str, Any]) -> dict[str, Any]:
    """Return the assistant message of the answer to a chat completions request."""
    messages = body.get("messages")
    if not isinstance(messages, list):
        messages = []
    tools = {
        name
        for tool in body.get("tools") or []
        if isinstance(tool, dict) and isinstance(name := tool.get("function", {}).get("name"), str)
    }
    scripted = _script(messages)
    if scripted is not None:
        script, step = scripted
        entry = script[step] if step < len(script) else None
        if isinstance(entry, list) and len(entry) == 2 and isinstance(entry[0], str):
            offered = _offered(entry[0], tools)
            if offered is not None:
                return _call(messages, offered, entry[1])
        return {"role": "assistant", "content": TEXT}
    if {"terminal", "finish"} <= tools:
        step = _step(messages)
        name, arguments = (
            ("terminal", {"command": COMMAND}) if step == 0 else ("finish", {"message": FINISH})
        )
        return _call(messages, name, arguments)
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
        interrupted = _interruption(body["messages"])
        if not body.get("stream"):
            document = json.dumps(
                {
                    **common,
                    "object": "chat.completion",
                    "choices": [{"index": 0, "message": message, "finish_reason": reason}],
                    "usage": USAGE,
                }
            )
            if interrupted is None:
                return Response(document, media_type="application/json")

            async def partial() -> AsyncIterator[str]:
                yield document[: len(document) // 2]
                raise _InterruptedAnswerError(interrupted)

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
            if interrupted is not None:
                raise _InterruptedAnswerError(interrupted)
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
