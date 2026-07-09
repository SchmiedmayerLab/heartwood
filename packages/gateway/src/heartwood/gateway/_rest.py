# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""REST-style request handling for the session gateway."""

from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from pydantic import ValidationError

from heartwood.gateway._gateway import SessionGateway
from heartwood.schemas import JsonValue
from heartwood.session import SessionCommand


@dataclass(frozen=True, slots=True)
class RestRequest:
    """Minimal REST request envelope for gateway tests and adapters."""

    method: str
    path: str
    body: str = ""


@dataclass(frozen=True, slots=True)
class RestResponse:
    """Minimal REST response envelope."""

    status_code: int
    body: dict[str, JsonValue]


class RestGateway:
    """Handle REST-style session command and replay requests."""

    def __init__(self, gateway: SessionGateway) -> None:
        self.gateway = gateway

    def handle(self, request: RestRequest) -> RestResponse:
        """Handle a REST-style request."""
        parsed = urlsplit(request.path)
        parts = tuple(part for part in parsed.path.split("/") if part)
        if len(parts) != 3 or parts[0] != "sessions":
            return _error(404, "unknown gateway route")
        session_id = parts[1]
        resource = parts[2]
        if request.method == "POST" and resource == "commands":
            return self._handle_command(session_id=session_id, body=request.body)
        if request.method == "GET" and resource == "events":
            query = parse_qs(parsed.query)
            try:
                after = _optional_int(query.get("after", [None])[0])
            except ValueError:
                return _error(400, "after query parameter must be an integer")
            events = self.gateway.replay_events(session_id=session_id, after_sequence=after)
            return RestResponse(
                status_code=200,
                body={"events": [event.model_dump(mode="json") for event in events]},
            )
        return _error(405, "method is not allowed for gateway route")

    def _handle_command(self, *, session_id: str, body: str) -> RestResponse:
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return _error(400, "request body must be valid JSON")
        try:
            command = SessionCommand.model_validate(payload)
        except ValidationError as error:
            return _error(422, error.errors()[0]["msg"])
        if command.session_id != session_id:
            return _error(409, "command session does not match route session")
        result = self.gateway.handle(command)
        return RestResponse(
            status_code=200,
            body={"events": [event.model_dump(mode="json") for event in result.events]},
        )


def _optional_int(value: str | None) -> int | None:
    if value is None:
        return None
    return int(value)


def _error(status_code: int, reason: object) -> RestResponse:
    return RestResponse(status_code=status_code, body={"error": str(reason)})
