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

from heartwood.core_adapter import (
    CommandConflictError,
    SessionOwnershipError,
    SessionRecoveryError,
)
from heartwood.gateway._action_settings import ActionSettingsError
from heartwood.gateway._credentials import CredentialStoreError
from heartwood.gateway._gateway import SessionGateway
from heartwood.gateway._local_model_contract import DEFAULT_LOCAL_CONTEXT_WINDOW
from heartwood.gateway._local_models import ModelRepositoryError
from heartwood.gateway._model_artifacts import ModelArtifactError
from heartwood.gateway._model_catalog import ModelCatalogError
from heartwood.gateway._model_settings import (
    ModelSettingsError,
    model_profile_from_mapping,
)
from heartwood.gateway._project_config import ProjectConfigError
from heartwood.gateway._session_catalog import SessionCatalogError, SessionNotFoundError
from heartwood.gateway._skill_settings import SkillSettingsError
from heartwood.gateway._startup import InterfaceKind
from heartwood.schemas import (
    ActionConfirmationRequest,
    ApiRequest,
    CustomLocalModelDownloadRequest,
    JsonValue,
    LocalModelImportRequest,
    ModelCatalogRequest,
    ModelConnectRequest,
    ModelDownloadRequest,
    ModelProfileRequest,
    ModelRepositoryRequest,
    ModelSelectionRequest,
    ModelSourceRequest,
    SessionCreateRequest,
    SessionRenameRequest,
    SkillInspectRequest,
    SkillInstallRequest,
    SubscriptionDeviceLoginRequest,
    SubscriptionDevicePollRequest,
)
from heartwood.session import SessionCommand, validate_session_id


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
        if parts == ("sessions",) and request.method == "GET":
            return RestResponse(status_code=200, body=_json_object(self.gateway.sessions()))
        if parts == ("sessions",) and request.method == "POST":
            return self._handle_session_creation(body=request.body)
        if parts == ("sessions", "default") and request.method == "POST":
            return RestResponse(status_code=200, body=_json_object(self.gateway.default_session()))
        if parts == ("project", "readiness") and request.method == "GET":
            try:
                readiness = self.gateway.project_readiness()
            except ProjectConfigError as error:
                return _error(422, error)
            return RestResponse(status_code=200, body=_json_object(readiness))
        if parts == ("project", "capabilities") and request.method == "GET":
            return RestResponse(
                status_code=200,
                body=_json_object(self.gateway.platform_capabilities()),
            )
        if parts == ("project", "startup") and request.method == "GET":
            query = parse_qs(parsed.query)
            interface = query.get("interface", ["web"])[0]
            if interface not in {"terminal", "web", "notebook"}:
                return _error(422, "interface must be terminal, web, or notebook")
            try:
                port = _optional_int(query.get("port", [None])[0]) or 8767
                startup = self.gateway.startup_plan(
                    interface=cast(InterfaceKind, interface),
                    port=port,
                )
            except (ProjectConfigError, ValueError) as error:
                return _error(422, error)
            return RestResponse(status_code=200, body=_json_object(startup))
        if parts == ("project", "initialize") and request.method == "POST":
            try:
                startup = self.gateway.initialize_project()
            except (ProjectConfigError, OSError) as error:
                return _error(422, error)
            return RestResponse(status_code=200, body=_json_object(startup))
        if len(parts) == 2 and parts[0] == "sessions" and request.method == "GET":
            try:
                session_id = validate_session_id(parts[1])
                session = self.gateway.session(session_id)
            except SessionNotFoundError as error:
                return _error(404, error)
            except SessionCatalogError as error:
                return _error(422, error)
            except ValueError as error:
                return _error(422, error)
            return RestResponse(status_code=200, body=_json_object(session))
        if len(parts) == 2 and parts[0] == "sessions" and request.method == "PATCH":
            try:
                session_id = validate_session_id(parts[1])
            except ValueError as error:
                return _error(422, error)
            return self._handle_session_rename(session_id=session_id, body=request.body)
        if parts == ("settings", "actions") and request.method == "GET":
            try:
                action_settings = self.gateway.action_settings()
            except ActionSettingsError as error:
                return _error(422, error)
            return RestResponse(status_code=200, body=_json_object(action_settings))
        if parts == ("settings", "actions", "confirmation") and request.method == "PUT":
            return self._handle_action_confirmation(body=request.body)
        if parts == ("settings", "models") and request.method == "GET":
            try:
                model_settings = self.gateway.model_settings()
            except (CredentialStoreError, ModelSettingsError) as error:
                return _error(422, error)
            return RestResponse(status_code=200, body=_json_object(model_settings))
        if parts == ("settings", "credentials") and request.method == "GET":
            try:
                credential_settings = self.gateway.credential_settings()
            except CredentialStoreError as error:
                return _error(422, error)
            return RestResponse(status_code=200, body=_json_object(credential_settings))
        if (
            len(parts) == 3
            and parts[:2] == ("settings", "credentials")
            and request.method == "DELETE"
        ):
            try:
                credential_settings = self.gateway.forget_credential(parts[2])
            except (CredentialStoreError, ModelCatalogError) as error:
                return _error(422, error)
            return RestResponse(status_code=200, body=_json_object(credential_settings))
        if parts == ("settings", "skills") and request.method == "GET":
            try:
                skill_settings = self.gateway.skill_settings()
            except SkillSettingsError as error:
                return _error(422, error)
            return RestResponse(status_code=200, body=_json_object(skill_settings))
        if parts == ("settings", "skills", "inspect") and request.method == "POST":
            return self._handle_skill_inspection(body=request.body)
        if parts == ("settings", "skills", "install") and request.method == "POST":
            return self._handle_skill_install(body=request.body)
        if len(parts) == 3 and parts[:2] == ("settings", "skills") and request.method == "DELETE":
            try:
                skill_settings = self.gateway.remove_skill(parts[2])
            except SkillSettingsError as error:
                return _error(422, error)
            return RestResponse(status_code=200, body=_json_object(skill_settings))
        if parts == ("settings", "models", "artifacts") and request.method == "GET":
            return RestResponse(status_code=200, body=_json_object(self.gateway.model_artifacts()))
        if parts == ("settings", "models", "catalog") and request.method == "POST":
            return self._handle_model_catalog(body=request.body)
        if parts == ("settings", "models", "subscription", "device") and request.method == "POST":
            return self._handle_subscription_device_login(body=request.body)
        if (
            parts == ("settings", "models", "subscription", "device", "poll")
            and request.method == "POST"
        ):
            return self._handle_subscription_device_poll(body=request.body)
        if parts == ("settings", "models", "repository") and request.method == "POST":
            return self._handle_model_repository(body=request.body)
        if parts == ("settings", "models", "source") and request.method == "PUT":
            return self._handle_model_source(body=request.body)
        if parts == ("settings", "models", "downloads") and request.method == "POST":
            payload = _request_body(ModelDownloadRequest, request.body)
            if isinstance(payload, RestResponse):
                return payload
            try:
                download = self.gateway.download_local_model(payload.model_id)
            except (ModelArtifactError, ModelRepositoryError) as error:
                return _error(422, error)
            return RestResponse(status_code=202, body=_json_object(download))
        if parts == ("settings", "models", "downloads", "custom") and request.method == "POST":
            return self._handle_custom_model_download(body=request.body)
        if parts == ("settings", "models", "imports") and request.method == "POST":
            return self._handle_local_model_import(body=request.body)
        if parts == ("settings", "models", "validation") and request.method == "GET":
            profile_id = parse_qs(parsed.query).get("profile_id", [None])[0]
            try:
                validation = self.gateway.validate_model_profile(profile_id)
            except (CredentialStoreError, ModelSettingsError) as error:
                return _error(422, error)
            return RestResponse(status_code=200, body=_json_object(validation))
        if parts == ("settings", "models", "profiles") and request.method == "POST":
            return self._handle_model_profile(body=request.body)
        if parts == ("settings", "models", "connect") and request.method == "POST":
            return self._handle_model_connection(body=request.body)
        if parts == ("settings", "models", "active") and request.method == "PUT":
            return self._handle_model_selection(body=request.body)
        if (
            len(parts) == 4
            and parts[:3] == ("settings", "models", "profiles")
            and request.method == "DELETE"
        ):
            try:
                model_settings = self.gateway.remove_model_profile(parts[3])
            except ModelSettingsError as error:
                return _error(422, error)
            return RestResponse(status_code=200, body=_json_object(model_settings))
        if len(parts) != 3 or parts[0] != "sessions":
            return _error(404, "unknown gateway route")
        try:
            session_id = validate_session_id(parts[1])
        except ValueError as error:
            return _error(422, error)
        resource = parts[2]
        if request.method == "GET" and resource == "audit-export":
            try:
                export = self.gateway.audit_export(session_id)
            except SessionCatalogError as error:
                return _error(404, error)
            return RestResponse(status_code=200, body=_json_object(export))
        if request.method == "POST" and resource == "commands":
            return self._handle_command(session_id=session_id, body=request.body)
        if request.method == "GET" and resource == "projection":
            try:
                projection = self.gateway.session_projection(session_id=session_id)
            except (SessionOwnershipError, SessionRecoveryError) as error:
                return _error(409, error)
            return RestResponse(
                status_code=200,
                body=_json_object(projection.safe_dict()),
            )
        if request.method == "GET" and resource == "events":
            query = parse_qs(parsed.query)
            try:
                after = _optional_int(query.get("after", [None])[0])
            except ValueError:
                return _error(400, "after query parameter must be an integer")
            try:
                snapshot = self.gateway.session_snapshot(
                    session_id=session_id,
                    after_sequence=after,
                )
            except (SessionOwnershipError, SessionRecoveryError) as error:
                return _error(409, error)
            return RestResponse(
                status_code=200,
                body={
                    "events": [event.model_dump(mode="json") for event in snapshot.events],
                    "projection": _json_object(snapshot.projection.safe_dict()),
                },
            )
        return _error(405, "method is not allowed for gateway route")

    def _handle_session_creation(self, *, body: str) -> RestResponse:
        payload = _request_body(SessionCreateRequest, body or "{}")
        if isinstance(payload, RestResponse):
            return payload
        try:
            session = self.gateway.create_session(payload.title)
        except SessionCatalogError as error:
            return _error(422, error)
        return RestResponse(status_code=201, body=_json_object(session))

    def _handle_session_rename(self, *, session_id: str, body: str) -> RestResponse:
        payload = _request_body(SessionRenameRequest, body)
        if isinstance(payload, RestResponse):
            return payload
        try:
            session = self.gateway.rename_session(session_id, payload.title)
        except SessionNotFoundError as error:
            return _error(404, error)
        except SessionCatalogError as error:
            return _error(422, error)
        return RestResponse(status_code=200, body=_json_object(session))

    def _handle_command(self, *, session_id: str, body: str) -> RestResponse:
        try:
            command = SessionCommand.model_validate_json(body)
        except ValidationError as error:
            if error.errors()[0]["type"] == "json_invalid":
                return _error(400, "request body must be valid JSON")
            return _error(422, _validation_reason(error))
        except ValueError:
            return _error(400, "request body must be valid JSON")
        if command.session_id != session_id:
            return _error(409, "command session does not match route session")
        try:
            result = self.gateway.handle(command)
        except (CommandConflictError, SessionOwnershipError, SessionRecoveryError) as error:
            return _error(409, error)
        snapshot = self.gateway.session_snapshot(
            session_id=session_id,
            after_sequence=(result.events[0].sequence - 1 if result.events else None),
        )
        return RestResponse(
            status_code=200,
            body={
                "events": [event.model_dump(mode="json") for event in snapshot.events],
                "projection": _json_object(snapshot.projection.safe_dict()),
            },
        )

    def _handle_action_confirmation(self, *, body: str) -> RestResponse:
        payload = _request_body(ActionConfirmationRequest, body)
        if isinstance(payload, RestResponse):
            return payload
        try:
            action_settings = self.gateway.select_action_confirmation_mode(payload.mode)
        except ActionSettingsError as error:
            return _error(422, error)
        return RestResponse(status_code=200, body=_json_object(action_settings))

    def _handle_model_profile(self, *, body: str) -> RestResponse:
        payload = _request_body(ModelProfileRequest, body)
        if isinstance(payload, RestResponse):
            return payload
        try:
            profile = model_profile_from_mapping(payload.model_dump(mode="python"))
            model_settings = self.gateway.save_model_profile(profile)
        except ModelSettingsError as error:
            return _error(422, error)
        return RestResponse(status_code=200, body=_json_object(model_settings))

    def _handle_model_repository(self, *, body: str) -> RestResponse:
        payload = _request_body(ModelRepositoryRequest, body)
        if isinstance(payload, RestResponse):
            return payload
        try:
            inspection = self.gateway.inspect_model_repository(
                payload.repository,
                revision=payload.revision,
            )
        except ModelRepositoryError as error:
            return _error(422, error)
        return RestResponse(status_code=200, body=_json_object(inspection))

    def _handle_custom_model_download(self, *, body: str) -> RestResponse:
        payload = _request_body(CustomLocalModelDownloadRequest, body)
        if isinstance(payload, RestResponse):
            return payload
        try:
            download = self.gateway.download_custom_local_model(
                payload.repository,
                revision=payload.revision,
            )
        except ModelRepositoryError as error:
            return _error(422, error)
        return RestResponse(status_code=202, body=_json_object(download))

    def _handle_local_model_import(self, *, body: str) -> RestResponse:
        payload = _request_body(LocalModelImportRequest, body)
        if isinstance(payload, RestResponse):
            return payload
        try:
            imported = self.gateway.import_local_model(
                Path(payload.path),
                source_repository=payload.repository,
                source_revision=payload.revision,
                license_posture=payload.license,
                context_window=payload.context_window or DEFAULT_LOCAL_CONTEXT_WINDOW,
            )
        except (ModelRepositoryError, ProjectConfigError, OSError) as error:
            return _error(422, error)
        return RestResponse(status_code=201, body=_json_object(imported))

    def _handle_model_source(self, *, body: str) -> RestResponse:
        payload = _request_body(ModelSourceRequest, body)
        if isinstance(payload, RestResponse):
            return payload
        try:
            model_settings = self.gateway.configure_model_source(payload.source_id)
        except (ModelCatalogError, ProjectConfigError) as error:
            return _error(422, error)
        return RestResponse(status_code=200, body=_json_object(model_settings))

    def _handle_model_connection(self, *, body: str) -> RestResponse:
        payload = _request_body(ModelConnectRequest, body)
        if isinstance(payload, RestResponse):
            return payload
        try:
            model_settings = self.gateway.connect_model(
                payload.connection_id,
                payload.model_id,
                token=payload.token,
                base_url=payload.base_url,
                manual=payload.manual,
                remember=payload.remember,
            )
        except (CredentialStoreError, ModelCatalogError, ModelSettingsError) as error:
            return _error(422, error)
        return RestResponse(status_code=200, body=_json_object(model_settings))

    def _handle_model_catalog(self, *, body: str) -> RestResponse:
        payload = _request_body(ModelCatalogRequest, body)
        if isinstance(payload, RestResponse):
            return payload
        try:
            catalog = self.gateway.discover_models(
                payload.connection_id,
                token=payload.token,
                base_url=payload.base_url,
                refresh=payload.refresh,
                remember=payload.remember,
            )
        except (CredentialStoreError, ModelCatalogError) as error:
            return _error(422, error)
        return RestResponse(status_code=200, body=_json_object(catalog))

    def _handle_subscription_device_login(self, *, body: str) -> RestResponse:
        payload = _request_body(SubscriptionDeviceLoginRequest, body)
        if isinstance(payload, RestResponse):
            return payload
        try:
            login = self.gateway.start_subscription_device_login(payload.connection_id)
        except ModelCatalogError as error:
            return _error(422, error)
        return RestResponse(status_code=201, body=_json_object(login))

    def _handle_subscription_device_poll(self, *, body: str) -> RestResponse:
        payload = _request_body(SubscriptionDevicePollRequest, body)
        if isinstance(payload, RestResponse):
            return payload
        try:
            login = self.gateway.poll_subscription_device_login(
                payload.connection_id,
                payload.login_id,
            )
        except ModelCatalogError as error:
            return _error(422, error)
        return RestResponse(status_code=200, body=_json_object(login))

    def _handle_model_selection(self, *, body: str) -> RestResponse:
        payload = _request_body(ModelSelectionRequest, body)
        if isinstance(payload, RestResponse):
            return payload
        try:
            model_settings = self.gateway.select_model_profile(payload.profile_id)
        except ModelSettingsError as error:
            return _error(422, error)
        return RestResponse(status_code=200, body=_json_object(model_settings))

    def _handle_skill_inspection(self, *, body: str) -> RestResponse:
        payload = _request_body(SkillInspectRequest, body)
        if isinstance(payload, RestResponse):
            return payload
        try:
            summary = self.gateway.inspect_skill(Path(payload.source))
        except SkillSettingsError as error:
            return _error(422, error)
        return RestResponse(status_code=200, body=_json_object(summary))

    def _handle_skill_install(self, *, body: str) -> RestResponse:
        payload = _request_body(SkillInstallRequest, body)
        if isinstance(payload, RestResponse):
            return payload
        try:
            skill_settings = self.gateway.install_skill(
                Path(payload.source),
                approved=payload.approved,
            )
        except SkillSettingsError as error:
            return _error(422, error)
        return RestResponse(status_code=200, body=_json_object(skill_settings))


def _request_body[RequestT: ApiRequest](
    request_type: type[RequestT],
    body: str,
) -> RequestT | RestResponse:
    try:
        return request_type.model_validate_json(body)
    except ValidationError as error:
        if error.errors()[0]["type"] == "json_invalid":
            return _error(400, "request body must be valid JSON")
        return _error(422, _validation_reason(error))


def _validation_reason(error: ValidationError) -> str:
    issue = error.errors(include_url=False)[0]
    issue_type = issue["type"]
    location = issue["loc"]
    field = ".".join(str(part) for part in location)
    if issue_type == "model_type":
        return "request body must be an object"
    if issue_type == "extra_forbidden":
        return f"request contains unsupported fields: {field}"
    if issue_type == "missing":
        return f"{field} is required"
    if field == "terms_accepted":
        return "ChatGPT terms must be accepted before sign-in"
    if issue_type == "string_type":
        return f"{field} must be a string"
    if issue_type == "bool_type":
        return f"{field} must be a boolean"
    if issue_type == "int_type":
        return f"{field} must be an integer"
    message = issue["msg"]
    return f"{field}: {message}" if field else message


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
