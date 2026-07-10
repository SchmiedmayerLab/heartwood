# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""REST-style request handling for the session gateway."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, urlsplit

from pydantic import ValidationError

from heartwood.gateway._action_settings import ActionSettingsError
from heartwood.gateway._gateway import SessionGateway
from heartwood.gateway._model_artifacts import ModelArtifactError
from heartwood.gateway._model_settings import (
    ModelSettingsError,
    model_profile_from_mapping,
)
from heartwood.gateway._skill_settings import SkillSettingsError
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
        if parts == ("settings", "actions") and request.method == "GET":
            try:
                settings = self.gateway.action_settings()
            except ActionSettingsError as error:
                return _error(422, error)
            return RestResponse(status_code=200, body=_json_object(settings))
        if parts == ("settings", "actions", "confirmation") and request.method == "PUT":
            return self._handle_action_confirmation(body=request.body)
        if parts == ("settings", "models") and request.method == "GET":
            try:
                settings = self.gateway.model_settings()
            except ModelSettingsError as error:
                return _error(422, error)
            return RestResponse(status_code=200, body=_json_object(settings))
        if parts == ("settings", "skills") and request.method == "GET":
            try:
                settings = self.gateway.skill_settings()
            except SkillSettingsError as error:
                return _error(422, error)
            return RestResponse(status_code=200, body=_json_object(settings))
        if parts == ("settings", "skills", "inspect") and request.method == "POST":
            return self._handle_skill_inspection(body=request.body)
        if parts == ("settings", "skills", "install") and request.method == "POST":
            return self._handle_skill_install(body=request.body)
        if len(parts) == 3 and parts[:2] == ("settings", "skills") and request.method == "DELETE":
            try:
                settings = self.gateway.remove_skill(parts[2])
            except SkillSettingsError as error:
                return _error(422, error)
            return RestResponse(status_code=200, body=_json_object(settings))
        if parts == ("settings", "models", "artifacts") and request.method == "GET":
            return RestResponse(status_code=200, body=_json_object(self.gateway.model_artifacts()))
        if parts == ("settings", "models", "downloads") and request.method == "POST":
            try:
                payload = json.loads(request.body)
            except json.JSONDecodeError:
                return _error(400, "request body must be valid JSON")
            if not isinstance(payload, dict) or not isinstance(payload.get("artifact_id"), str):
                return _error(422, "artifact_id must be a string")
            try:
                download = self.gateway.download_model_artifact(payload["artifact_id"])
            except ModelArtifactError as error:
                return _error(422, error)
            return RestResponse(status_code=202, body=_json_object(download))
        if parts == ("settings", "models", "validation") and request.method == "GET":
            profile_id = parse_qs(parsed.query).get("profile_id", [None])[0]
            try:
                validation = self.gateway.validate_model_profile(profile_id)
            except ModelSettingsError as error:
                return _error(422, error)
            return RestResponse(status_code=200, body=_json_object(validation))
        if parts == ("settings", "models", "profiles") and request.method == "POST":
            return self._handle_model_profile(body=request.body)
        if parts == ("settings", "models", "active") and request.method == "PUT":
            return self._handle_model_selection(body=request.body)
        if (
            len(parts) == 4
            and parts[:3] == ("settings", "models", "profiles")
            and request.method == "DELETE"
        ):
            try:
                settings = self.gateway.remove_model_profile(parts[3])
            except ModelSettingsError as error:
                return _error(422, error)
            return RestResponse(status_code=200, body=_json_object(settings))
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

    def _handle_action_confirmation(self, *, body: str) -> RestResponse:
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return _error(400, "request body must be valid JSON")
        if not isinstance(payload, dict) or not isinstance(payload.get("mode"), str):
            return _error(422, "mode must be a string")
        try:
            settings = self.gateway.select_action_confirmation_mode(payload["mode"])
        except ActionSettingsError as error:
            return _error(422, error)
        return RestResponse(status_code=200, body=_json_object(settings))

    def _handle_model_profile(self, *, body: str) -> RestResponse:
        try:
            payload = json.loads(body)
            profile = model_profile_from_mapping(payload)
            settings = self.gateway.save_model_profile(profile)
        except json.JSONDecodeError:
            return _error(400, "request body must be valid JSON")
        except ModelSettingsError as error:
            return _error(422, error)
        return RestResponse(status_code=200, body=_json_object(settings))

    def _handle_model_selection(self, *, body: str) -> RestResponse:
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return _error(400, "request body must be valid JSON")
        if not isinstance(payload, dict) or not isinstance(payload.get("profile_id"), str):
            return _error(422, "profile_id must be a string")
        try:
            settings = self.gateway.select_model_profile(payload["profile_id"])
        except ModelSettingsError as error:
            return _error(422, error)
        return RestResponse(status_code=200, body=_json_object(settings))

    def _handle_skill_inspection(self, *, body: str) -> RestResponse:
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return _error(400, "request body must be valid JSON")
        if not isinstance(payload, dict) or not isinstance(payload.get("source"), str):
            return _error(422, "source must be a string")
        try:
            summary = self.gateway.inspect_skill(Path(payload["source"]))
        except SkillSettingsError as error:
            return _error(422, error)
        return RestResponse(status_code=200, body=_json_object(summary))

    def _handle_skill_install(self, *, body: str) -> RestResponse:
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return _error(400, "request body must be valid JSON")
        if not isinstance(payload, dict) or not isinstance(payload.get("source"), str):
            return _error(422, "source must be a string")
        if not isinstance(payload.get("approved"), bool):
            return _error(422, "approved must be a boolean")
        try:
            settings = self.gateway.install_skill(
                Path(payload["source"]),
                approved=payload["approved"],
            )
        except SkillSettingsError as error:
            return _error(422, error)
        return RestResponse(status_code=200, body=_json_object(settings))


def _optional_int(value: str | None) -> int | None:
    if value is None:
        return None
    return int(value)


def _error(status_code: int, reason: object) -> RestResponse:
    return RestResponse(status_code=status_code, body={"error": str(reason)})


def _json_object(value: object) -> dict[str, JsonValue]:
    decoded = json.loads(json.dumps(value))
    if not isinstance(decoded, dict):  # pragma: no cover - callers pass mappings
        msg = "expected a JSON object"
        raise TypeError(msg)
    return cast(dict[str, JsonValue], decoded)
