# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Model connections and upstream-backed catalog discovery."""

from __future__ import annotations

import os
import re
import ssl
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, fields
from importlib import import_module
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from heartwood.core_adapter import BackendErrorCode, backend_error_message
from heartwood.gateway._managed_inference import managed_inference_client, require_managed_base_url
from heartwood.gateway._model_settings import CredentialKind, ModelProfile
from heartwood.gateway._openhands_models import (
    OpenHandsModelError,
    prepare_openhands_import,
    request_endpoint_for_model,
)
from heartwood.gateway._subscriptions import (
    OPENAI_SUBSCRIPTION_CONNECTION_ID,
    OPENAI_SUBSCRIPTION_CREDENTIAL_REFERENCE,
    OPENAI_SUBSCRIPTION_ENDPOINT,
    OpenHandsOpenAISubscription,
)
from heartwood.model_policy import ModelPolicyEngine, PolicyInputError, normalize_endpoint
from heartwood.schemas import (
    ModelCallDecision,
    ModelConnectionManifest,
    ModelConnectionsManifest,
    ModelDataHandling,
    PlatformModelMetadata,
    PolicyProfile,
)

type ConnectionProtocol = Literal[
    "anthropic", "openai", "openai-compatible", "static", "subscription"
]
type ConnectionSource = Literal["built-in", "platform", "user"]
type ConnectionGroup = Literal[
    "compatible-service", "heartwood-managed", "hosted-provider", "research-environment"
]
type ModelAvailability = Literal["available", "experimental", "unsupported"]
type ApiMode = Literal["chat", "responses"]

_CONNECTION_PROTOCOLS = {
    "anthropic",
    "openai",
    "openai-compatible",
    "static",
    "subscription",
}
_CONNECTION_SOURCES = {"built-in", "platform", "user"}
_CONNECTION_GROUP_LABELS: dict[ConnectionGroup, str] = {
    "heartwood-managed": "Run with Heartwood",
    "research-environment": "Institution-managed providers",
    "hosted-provider": "Hosted providers",
    "compatible-service": "Other compatible services",
}
_CONNECTION_GROUP_ORDER: dict[ConnectionGroup, int] = {
    "heartwood-managed": 0,
    "research-environment": 1,
    "hosted-provider": 2,
    "compatible-service": 3,
}
_CREDENTIAL_KINDS = {"environment", "file", "managed-identity", "none"}
_CATALOG_TIMEOUT_SECONDS = 30.0
_CATALOG_MAX_RETRIES = 1
_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_SECRET_FIELD_MARKERS = ("api_key", "apikey", "password", "secret", "token")
# Declared protocols name the OpenHands API modes; other identifiers are unsupported.
_API_MODES: frozenset[str] = frozenset({"chat", "responses"})


class ModelCatalogError(ValueError):
    """Raised when model connection metadata or discovery is invalid.

    A failure that matches a platform failure state carries its stable diagnostic code,
    which the message starts with and the gateway's HTTP routes return separately.
    """

    def __init__(self, detail: str, *, code: BackendErrorCode | None = None) -> None:
        self.detail = detail
        self.code = None if code is None else code.value
        super().__init__(detail if code is None else f"{code.value}: {detail}")


@dataclass(frozen=True, slots=True)
class ModelConnection:
    """One non-secret model source that may expose several models."""

    connection_id: str
    label: str
    protocol: ConnectionProtocol
    model_prefix: str
    source: ConnectionSource
    credential_kind: CredentialKind
    policy_endpoint: str | None
    catalog_endpoint: str | None
    base_url: str | None = None
    api_key_env: str | None = None
    api_key_file: str | None = None
    api_version: str | None = None
    aws_region_name: str | None = None
    aws_profile_name: str | None = None
    description: str = ""
    static_models: tuple[str, ...] = ()
    # Certificate authorities that replace the default bundle for model discovery.
    ca_file: str | None = None
    subscription_vendor: str | None = None
    data_handling: ModelDataHandling | None = None
    # Deployment declarations for models the OpenHands model table may not recognize.
    model_metadata: tuple[PlatformModelMetadata, ...] = ()

    def validate(self, *, configurable: bool = False) -> None:
        """Validate connection identity, routes, and credential references."""
        if _SAFE_ID.fullmatch(self.connection_id) is None:
            raise ModelCatalogError(
                "connection_id must contain only letters, numbers, hyphens, or underscores"
            )
        if not self.label.strip():
            raise ModelCatalogError("connection label must be a non-empty string")
        if self.protocol not in _CONNECTION_PROTOCOLS:
            raise ModelCatalogError(f"unsupported model connection protocol: {self.protocol}")
        if self.source not in _CONNECTION_SOURCES:
            raise ModelCatalogError(f"unsupported model connection source: {self.source}")
        if not self.model_prefix or not self.model_prefix.endswith("/"):
            raise ModelCatalogError("model_prefix must be a provider prefix ending in a slash")
        for name, value in (
            ("api_version", self.api_version),
            ("aws_region_name", self.aws_region_name),
            ("aws_profile_name", self.aws_profile_name),
        ):
            if value is not None and not value.strip():
                raise ModelCatalogError(f"{name} must be non-empty when provided")
        if self.credential_kind not in _CREDENTIAL_KINDS:
            raise ModelCatalogError(f"unsupported credential kind: {self.credential_kind}")
        if configurable:
            if any((self.base_url, self.catalog_endpoint, self.policy_endpoint)):
                raise ModelCatalogError("configurable connections cannot declare fixed endpoints")
        else:
            if self.policy_endpoint is None:
                raise ModelCatalogError("model connections require a policy_endpoint")
            _validate_endpoint(self.policy_endpoint, "policy_endpoint")
            if self.protocol not in {"static", "subscription"} and self.catalog_endpoint is None:
                raise ModelCatalogError("discoverable model connections require a catalog_endpoint")
        if self.catalog_endpoint is not None:
            _validate_endpoint(self.catalog_endpoint, "catalog_endpoint")
        if self.base_url is not None:
            _validate_base_url(self.base_url)
            for endpoint in (self.catalog_endpoint, self.policy_endpoint):
                if endpoint is not None and _origin(endpoint) != _origin(self.base_url):
                    raise ModelCatalogError(
                        "base_url, catalog_endpoint, and policy_endpoint must use the same origin"
                    )
        self._validate_credentials(configurable=configurable)
        if self.ca_file is not None and (
            not Path(self.ca_file).is_absolute()
            or self.protocol not in {"openai", "openai-compatible"}
            or self.catalog_endpoint is None
            or not self.catalog_endpoint.startswith("https://")
        ):
            raise ModelCatalogError(
                "ca_file is an absolute path for discovery over https by an OpenAI protocol"
            )
        if self.protocol == "subscription":
            if self.subscription_vendor != "openai":
                raise ModelCatalogError(
                    "subscription connections require a supported subscription_vendor"
                )
            if self.credential_kind != "managed-identity":
                raise ModelCatalogError(
                    "subscription connections require dependency-managed credentials"
                )
        elif self.subscription_vendor is not None:
            raise ModelCatalogError(
                "subscription_vendor is allowed only for subscription connections"
            )
        if self.protocol == "static" and not self.static_models:
            raise ModelCatalogError("static model connections require at least one model")
        if self.protocol != "static" and self.static_models:
            raise ModelCatalogError("static_models are only allowed for static connections")
        if any(
            not model.strip() or any(char.isspace() for char in model)
            for model in self.static_models
        ):
            raise ModelCatalogError("static model identifiers must be non-empty without whitespace")
        if len(self.static_models) != len(set(self.static_models)):
            raise ModelCatalogError("static model identifiers must be unique")
        if any(item.connection_id != self.connection_id for item in self.model_metadata):
            raise ModelCatalogError("model metadata must name its own connection")

    def _validate_credentials(self, *, configurable: bool) -> None:
        if self.credential_kind == "environment":
            if self.api_key_env is None or _ENVIRONMENT_NAME.fullmatch(self.api_key_env) is None:
                raise ModelCatalogError("environment credentials require a valid api_key_env name")
            if self.api_key_file is not None:
                raise ModelCatalogError("api_key_file is only allowed for file credentials")
            return
        if self.credential_kind == "file":
            if self.api_key_file is None or not Path(self.api_key_file).is_absolute():
                raise ModelCatalogError("file credentials require an absolute api_key_file path")
            if self.api_key_env is not None:
                raise ModelCatalogError("api_key_env is only allowed for environment credentials")
            return
        if self.api_key_env is not None or self.api_key_file is not None:
            raise ModelCatalogError(
                f"{self.credential_kind} credentials cannot declare API key references"
            )
        if self.credential_kind == "none" and not configurable and self.policy_endpoint is not None:
            parsed = urlsplit(self.policy_endpoint)
            if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
                raise ModelCatalogError(
                    "credential kind none is allowed only for loopback model endpoints"
                )

    @property
    def credential_reference(self) -> str | None:
        """Return the non-secret policy reference for this connection."""
        if self.protocol == "subscription":
            return OPENAI_SUBSCRIPTION_CREDENTIAL_REFERENCE
        if self.credential_kind == "environment":
            return self.api_key_env
        if self.credential_kind == "file":
            return self.api_key_file
        if self.credential_kind == "managed-identity":
            return "managed-identity"
        return None

    def provider_model_id(self, execution_model: str) -> str:
        """Return the provider-facing identifier from a normalized execution model."""
        return execution_model.removeprefix(self.model_prefix)

    def declared_model(self, execution_model: str) -> PlatformModelMetadata | None:
        """Return the deployment's declaration for one model, if it made one."""
        model_id = self.provider_model_id(execution_model)
        return next(
            (item for item in self.model_metadata if item.model_id in {model_id, execution_model}),
            None,
        )

    def request_endpoint(self, execution_model: str) -> str:
        """Return the endpoint OpenHands will use for this model."""
        if self.policy_endpoint is None:
            raise ModelCatalogError("model connection does not define a request endpoint")
        if self.protocol not in {"openai", "openai-compatible"}:
            return self.policy_endpoint
        declared = self.declared_model(execution_model)
        try:
            return request_endpoint_for_model(
                execution_model,
                self.policy_endpoint,
                api_mode=None if declared is None else declared_api_mode(declared),
            )
        except OpenHandsModelError as error:
            raise ModelCatalogError(str(error)) from error

    def matches_profile(self, profile: ModelProfile) -> bool:
        """Return whether a profile preserves this connection's complete route contract."""
        profile_route = (
            profile.profile_id,
            profile.credential_kind,
            profile.auth_type,
            profile.subscription_vendor,
            profile.base_url,
            profile.api_key_env,
            profile.api_key_file,
            profile.api_version,
            profile.aws_region_name,
            profile.aws_profile_name,
        )
        connection_route = (
            self.connection_id,
            self.credential_kind,
            "subscription" if self.protocol == "subscription" else "api_key",
            self.subscription_vendor,
            self.base_url,
            self.api_key_env,
            self.api_key_file,
            self.api_version,
            self.aws_region_name,
            self.aws_profile_name,
        )
        if not profile.model.startswith(self.model_prefix) or profile_route != connection_route:
            return False
        try:
            return profile.policy_endpoint == self.request_endpoint(profile.model)
        except ModelCatalogError:
            return False

    @property
    def group(self) -> ConnectionGroup:
        """Return the shared researcher-facing category for this connection."""
        if self.connection_id == "heartwood":
            return "heartwood-managed"
        if self.source == "platform":
            return "research-environment"
        if self.source == "user":
            return "compatible-service"
        return "hosted-provider"

    @property
    def presentation_order(self) -> int:
        """Return the shared connection ordering key used by every interface."""
        return _CONNECTION_GROUP_ORDER[self.group]

    def credential_status(self, env: Mapping[str, str]) -> str:
        """Return whether the referenced credential is available."""
        if self.credential_kind in {"managed-identity", "none"}:
            return "configured"
        if self.credential_kind == "environment":
            return "available" if env.get(cast(str, self.api_key_env)) else "missing"
        path = Path(cast(str, self.api_key_file))
        try:
            return "available" if path.read_text(encoding="utf-8").strip() else "missing"
        except OSError:
            return "missing"

    def resolve_api_key(self, env: Mapping[str, str]) -> str | None:
        """Resolve the credential without persisting it in connection metadata."""
        if self.credential_kind in {"managed-identity", "none"}:
            return None
        if self.credential_kind == "environment":
            value = env.get(cast(str, self.api_key_env))
            if not value:
                raise ModelCatalogError("model connection credential is unavailable")
            return value
        path = Path(cast(str, self.api_key_file))
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError as error:
            raise ModelCatalogError("model connection credential file is unavailable") from error
        if not value:
            raise ModelCatalogError("model connection credential file is empty")
        return value

    def manifest_dict(self) -> dict[str, object]:
        """Return the non-secret manifest representation of this connection."""
        return {
            **{
                field.name: getattr(self, field.name)
                for field in fields(self)
                if field.name != "model_metadata"
            },
            "data_handling": (
                None if self.data_handling is None else self.data_handling.model_dump(mode="json")
            ),
        }

    def safe_dict(self, env: Mapping[str, str]) -> dict[str, object]:
        """Return API-safe connection metadata, without the operator's file paths."""
        manifest = self.manifest_dict()
        manifest.pop("ca_file")
        return {
            **manifest,
            "data_handling_summary": data_handling_summary(self.data_handling),
            "group": self.group,
            "group_label": _CONNECTION_GROUP_LABELS[self.group],
            "accepts_token": self.credential_kind == "environment",
            "supports_login": self.protocol == "subscription",
            "auth_type": "subscription" if self.protocol == "subscription" else "api_key",
            "credential_status": self.credential_status(env),
        }


def matching_model_connection(
    profile: ModelProfile,
    connections: Iterable[ModelConnection],
) -> ModelConnection | None:
    """Return the connection whose complete route contract matches a profile."""
    return next(
        (connection for connection in connections if connection.matches_profile(profile)),
        None,
    )


def route_data_handling(
    profile: ModelProfile,
    connections: Iterable[ModelConnection],
) -> ModelDataHandling | None:
    """Return the declared handling of the connection that owns a profile's route."""
    connection = matching_model_connection(profile, connections)
    return None if connection is None else connection.data_handling


def route_policy_decision(
    profile: ModelProfile,
    policy: PolicyProfile,
    connections: Iterable[ModelConnection],
) -> ModelCallDecision:
    """Return the policy decision for a profile's route, as each model call receives it.

    Selection, validation and readiness share it, so none of them can admit a route
    that the policy refuses before a model call.
    """
    return ModelPolicyEngine(policy).evaluate(
        endpoint=profile.policy_endpoint,
        capability_tier=profile.capability_tier,
        credential_reference=profile.credential_reference,
        data_handling=route_data_handling(profile, connections),
        decision_id=f"model-profile-{profile.profile_id}",
        purpose=(
            "selected Heartwood-managed model"
            if profile.profile_id == "heartwood"
            else f"model profile {profile.profile_id}"
        ),
    )


_DESTINATION_TEXT = {
    "same-compute": "Content stays on the compute running this project",
    "institution-managed": "Content goes to an institution-managed service",
    "provider": "Content goes to the model provider",
    "operator-defined": "Content goes to a service defined by its operator",
}
_RETENTION_TEXT = {
    "deployment": "the deployment",
    "institution": "the institution's agreements",
    "provider": "the provider's terms",
    "service-operator": "the service operator",
}


def data_handling_summary(data_handling: ModelDataHandling | None) -> str:
    """Return the shared researcher-facing statement of a route's declared handling."""
    if data_handling is None:
        return "Data handling is not declared for this route."
    return (
        f"{_DESTINATION_TEXT[data_handling.destination]} ({data_handling.route}); "
        f"retention is governed by {_RETENTION_TEXT[data_handling.retention_authority]}. "
        "This is a declaration, not verified provider behavior."
    )


def active_model_connections(
    base_connections: Iterable[ModelConnection],
    configured_connections: Iterable[ModelConnection],
    *,
    allowed_connection_ids: Iterable[str],
) -> tuple[ModelConnection, ...]:
    """Return the one validated connection set used by readiness and execution."""
    allowed = frozenset(allowed_connection_ids)
    loaded = tuple(
        connection
        for connection in (*base_connections, *configured_connections)
        if connection.connection_id in allowed or connection.source == "platform"
    )
    for connection in loaded:
        connection.validate(configurable=connection.connection_id == "custom-api")
    connection_ids = [connection.connection_id for connection in loaded]
    if len(connection_ids) != len(set(connection_ids)):
        raise ModelCatalogError("model connection ids must be unique")
    return loaded


@dataclass(frozen=True, slots=True)
class ProviderModel:
    """Minimal model identity returned by an upstream catalog."""

    model_id: str
    display_name: str | None = None


@dataclass(frozen=True, slots=True)
class ModelCatalogEntry:
    """One normalized model choice presented to every Heartwood client."""

    model_id: str
    display_name: str
    execution_model: str
    availability: ModelAvailability
    reason: str
    context_window: int | None = None
    supports_tools: bool | None = None

    def safe_dict(self) -> dict[str, object]:
        """Return serializable model metadata."""
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ModelCatalog:
    """Normalized catalog for one model connection."""

    connection: ModelConnection
    models: tuple[ModelCatalogEntry, ...]
    refreshed_at: int

    def safe_dict(self, env: Mapping[str, str]) -> dict[str, object]:
        """Return the shared web and CLI catalog representation."""
        return {
            "schema_version": "heartwood.model-catalog.v1",
            "connection": self.connection.safe_dict(env),
            "models": [model.safe_dict() for model in self.models],
            "refreshed_at": self.refreshed_at,
        }


class ModelLister(Protocol):
    """Provider SDK model-list operation used by the catalog service."""

    def __call__(
        self,
        connection: ModelConnection,
        api_key: str | None,
        /,
    ) -> Sequence[ProviderModel]: ...


class ModelCatalogService:
    """Discover models with maintained SDKs and normalize OpenHands compatibility."""

    def __init__(
        self,
        *,
        openai_lister: ModelLister | None = None,
        anthropic_lister: ModelLister | None = None,
        subscription_lister: ModelLister | None = None,
        compatibility: Callable[
            [ModelConnection, str],
            tuple[ModelAvailability, str, int | None, bool | None],
        ]
        | None = None,
        cache_ttl_seconds: int = 300,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._env = dict(os.environ if env is None else env)
        self._openai_lister = openai_lister or (
            lambda connection, api_key: _list_openai_models(connection, api_key, env=self._env)
        )
        self._anthropic_lister = anthropic_lister or _list_anthropic_models
        self._subscription_lister = subscription_lister or _list_subscription_models
        self._compatibility = compatibility or _model_compatibility
        self._cache_ttl_seconds = cache_ttl_seconds
        self._cache: dict[str, tuple[float, ModelCatalog]] = {}

    def discover(
        self,
        connection: ModelConnection,
        *,
        api_key: str | None,
        refresh: bool = False,
    ) -> ModelCatalog:
        """Return a cached or freshly discovered catalog."""
        cached = self._cache.get(connection.connection_id)
        now = time.monotonic()
        if not refresh and cached is not None and now - cached[0] < self._cache_ttl_seconds:
            return cached[1]
        try:
            if connection.protocol == "static":
                discovered = tuple(
                    ProviderModel(model_id=model) for model in connection.static_models
                )
            elif connection.protocol == "anthropic":
                discovered = tuple(self._anthropic_lister(connection, api_key))
            elif connection.protocol == "subscription":
                discovered = tuple(self._subscription_lister(connection, api_key))
            else:
                discovered = tuple(self._openai_lister(connection, api_key))
        except ModelCatalogError:
            raise
        except Exception as error:
            code = _discovery_error_code(error)
            raise ModelCatalogError(backend_error_message(code), code=code) from error
        models = self._normalize(connection, discovered)
        catalog = ModelCatalog(
            connection=connection,
            models=models,
            refreshed_at=int(time.time()),
        )
        self._cache[connection.connection_id] = (now, catalog)
        return catalog

    def cached(self, connection_id: str) -> ModelCatalog | None:
        """Return the current cached catalog without triggering network access."""
        cached = self._cache.get(connection_id)
        if cached is None:
            return None
        if time.monotonic() - cached[0] >= self._cache_ttl_seconds:
            self._cache.pop(connection_id, None)
            return None
        return cached[1]

    def manual(self, connection: ModelConnection, model_id: str) -> ModelCatalog:
        """Create a one-model fallback when a custom API cannot list models."""
        if connection.connection_id != "custom-api":
            raise ModelCatalogError("manual model identifiers are allowed only for Custom API")
        models = self._normalize(connection, (ProviderModel(model_id=model_id),))
        if not models:
            raise ModelCatalogError("model_id must be a non-empty identifier without whitespace")
        catalog = ModelCatalog(
            connection=connection,
            models=models,
            refreshed_at=int(time.time()),
        )
        self._cache[connection.connection_id] = (time.monotonic(), catalog)
        return catalog

    def invalidate(self, connection_id: str) -> None:
        """Discard cached metadata for a connection."""
        self._cache.pop(connection_id, None)

    def _normalize(
        self,
        connection: ModelConnection,
        discovered: Sequence[ProviderModel],
    ) -> tuple[ModelCatalogEntry, ...]:
        unique: dict[str, ProviderModel] = {}
        for item in discovered:
            model_id = item.model_id.strip()
            if not model_id or any(character.isspace() for character in model_id):
                continue
            unique.setdefault(model_id, item)
        entries: list[ModelCatalogEntry] = []
        for model_id, item in unique.items():
            display_name = (item.display_name or model_id).strip() or model_id
            execution_model = (
                model_id
                if model_id.startswith(connection.model_prefix)
                else f"{connection.model_prefix}{model_id}"
            )
            declared = connection.declared_model(execution_model)
            availability, reason, context_window, supports_tools = (
                self._compatibility(connection, execution_model)
                if declared is None
                else _declared_compatibility(declared)
            )
            entries.append(
                ModelCatalogEntry(
                    model_id=model_id,
                    display_name=display_name,
                    execution_model=execution_model,
                    availability=availability,
                    reason=reason,
                    context_window=context_window,
                    supports_tools=supports_tools,
                )
            )
        order = {"available": 0, "experimental": 1, "unsupported": 2}
        entries.sort(
            key=lambda item: (
                order[item.availability],
                item.display_name.casefold(),
                item.model_id,
            )
        )
        return tuple(entries)


BUILT_IN_MODEL_CONNECTIONS: tuple[ModelConnection, ...] = (
    ModelConnection(
        connection_id="heartwood",
        label="Run with Heartwood",
        protocol="openai-compatible",
        model_prefix="openai/",
        source="built-in",
        credential_kind="none",
        base_url="http://127.0.0.1:8765/v1",
        catalog_endpoint="http://127.0.0.1:8765/v1/models",
        policy_endpoint="http://127.0.0.1:8765/v1/chat/completions",
        description="Models served by the runtime Heartwood manages for this project.",
        data_handling=ModelDataHandling(
            destination="same-compute",
            retention_authority="deployment",
            route="Heartwood-managed inference server in this compute environment",
        ),
    ),
    ModelConnection(
        connection_id=OPENAI_SUBSCRIPTION_CONNECTION_ID,
        label="Sign in with ChatGPT",
        protocol="subscription",
        model_prefix="openai/",
        source="built-in",
        credential_kind="managed-identity",
        policy_endpoint=OPENAI_SUBSCRIPTION_ENDPOINT,
        catalog_endpoint=None,
        description="Codex models available through a ChatGPT Plus or Pro subscription.",
        subscription_vendor="openai",
        data_handling=ModelDataHandling(
            destination="provider",
            retention_authority="provider",
            route="OpenAI through the signed-in ChatGPT subscription",
        ),
    ),
    ModelConnection(
        connection_id="openai",
        label="OpenAI API",
        protocol="openai",
        model_prefix="openai/",
        source="built-in",
        credential_kind="environment",
        api_key_env="OPENAI_API_KEY",
        catalog_endpoint="https://api.openai.com/v1/models",
        policy_endpoint="https://api.openai.com/v1/chat/completions",
        description="Models available to the supplied OpenAI credential.",
        data_handling=ModelDataHandling(
            destination="provider", retention_authority="provider", route="OpenAI API"
        ),
    ),
    ModelConnection(
        connection_id="anthropic",
        label="Anthropic",
        protocol="anthropic",
        model_prefix="anthropic/",
        source="built-in",
        credential_kind="environment",
        api_key_env="ANTHROPIC_API_KEY",
        catalog_endpoint="https://api.anthropic.com/v1/models",
        policy_endpoint="https://api.anthropic.com/v1/messages",
        description="Models available to the supplied Anthropic credential.",
        data_handling=ModelDataHandling(
            destination="provider", retention_authority="provider", route="Anthropic API"
        ),
    ),
    ModelConnection(
        connection_id="custom-api",
        label="Other compatible service",
        protocol="openai-compatible",
        model_prefix="openai/",
        source="user",
        credential_kind="environment",
        api_key_env="HEARTWOOD_CUSTOM_MODEL_API_KEY",
        catalog_endpoint=None,
        policy_endpoint=None,
        description="A service that implements the OpenAI API format.",
    ),
)


def model_connections_from_mapping(value: object) -> tuple[ModelConnection, ...]:
    """Validate configured connections and combine them with built-ins."""
    for connection in BUILT_IN_MODEL_CONNECTIONS:
        connection.validate(configurable=connection.connection_id == "custom-api")
    _reject_secret_values(value)
    try:
        manifest = ModelConnectionsManifest.model_validate(value)
    except ValidationError as error:
        details = "; ".join(
            f"{'.'.join(str(part) for part in item['loc']) or 'manifest'}: {item['msg']}"
            for item in error.errors(include_input=False, include_url=False)
        )
        raise ModelCatalogError(f"invalid model connections manifest: {details}") from None
    configured = tuple(_connection_from_manifest(item) for item in manifest.connections)
    connection_ids = [
        connection.connection_id for connection in (*BUILT_IN_MODEL_CONNECTIONS, *configured)
    ]
    if len(connection_ids) != len(set(connection_ids)):
        raise ModelCatalogError("model connection ids must be unique")
    return (*BUILT_IN_MODEL_CONNECTIONS, *configured)


def custom_model_connection(base_url: str, *, has_token: bool) -> ModelConnection:
    """Build a validated custom OpenAI-compatible connection."""
    normalized_base = base_url.strip().rstrip("/")
    _validate_base_url(normalized_base)
    requires_token = custom_model_connection_requires_token(normalized_base)
    credential_kind: CredentialKind = "environment" if has_token else "none"
    connection = ModelConnection(
        connection_id="custom-api",
        label="Custom API",
        protocol="openai-compatible",
        model_prefix="openai/",
        source="user",
        credential_kind=credential_kind,
        api_key_env="HEARTWOOD_CUSTOM_MODEL_API_KEY" if has_token else None,
        base_url=normalized_base,
        catalog_endpoint=f"{normalized_base}/models",
        policy_endpoint=f"{normalized_base}/chat/completions",
        description="A service that implements the OpenAI API format.",
    )
    if not has_token and requires_token:
        raise ModelCatalogError(
            "a remote custom API requires a token or managed platform connection"
        )
    connection.validate()
    return connection


def custom_model_connection_requires_token(base_url: str) -> bool:
    """Return whether a custom endpoint is remote and therefore needs a token."""
    normalized_base = base_url.strip().rstrip("/")
    _validate_base_url(normalized_base)
    parsed = urlsplit(normalized_base)
    return not (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1", "localhost"})


def _connection_from_manifest(value: ModelConnectionManifest) -> ModelConnection:
    connection = ModelConnection(
        connection_id=value.connection_id,
        label=value.label,
        protocol=value.protocol,
        model_prefix=value.model_prefix,
        source=value.source,
        credential_kind=value.credential_kind,
        policy_endpoint=value.policy_endpoint,
        catalog_endpoint=value.catalog_endpoint,
        base_url=value.base_url,
        api_key_env=value.api_key_env,
        api_key_file=value.api_key_file,
        api_version=value.api_version,
        aws_region_name=value.aws_region_name,
        aws_profile_name=value.aws_profile_name,
        description=value.description,
        static_models=tuple(value.static_models),
        subscription_vendor=value.subscription_vendor,
        data_handling=value.data_handling,
        ca_file=value.ca_file,
    )
    connection.validate()
    return connection


def _list_openai_models(
    connection: ModelConnection,
    api_key: str | None,
    *,
    env: Mapping[str, str] | None = None,
) -> Sequence[ProviderModel]:
    module = import_module("openai")
    options: dict[str, object] = {
        "api_key": api_key or "not-required",
        "max_retries": _CATALOG_MAX_RETRIES,
        "timeout": _CATALOG_TIMEOUT_SECONDS,
    }
    if connection.base_url is not None:
        options["base_url"] = connection.base_url
    if connection.connection_id == "heartwood":
        require_managed_base_url(connection.base_url)
        options["http_client"] = managed_inference_client(os.environ if env is None else env)
    elif connection.ca_file is not None:
        # The connection's authorities alone, without proxies from the environment.
        options["http_client"] = httpx.Client(
            verify=ssl.create_default_context(cafile=connection.ca_file),
            trust_env=False,
            follow_redirects=False,
            timeout=_CATALOG_TIMEOUT_SECONDS,
        )
    client = module.OpenAI(**options)
    try:
        return tuple(
            ProviderModel(model_id=cast(str, model.id), display_name=None)
            for model in client.models.list()
        )
    finally:
        client.close()


def _list_anthropic_models(
    connection: ModelConnection,
    api_key: str | None,
) -> Sequence[ProviderModel]:
    if not api_key:
        raise ModelCatalogError("model connection credential is unavailable")
    module = import_module("anthropic")
    options: dict[str, object] = {
        "api_key": api_key,
        "max_retries": _CATALOG_MAX_RETRIES,
        "timeout": _CATALOG_TIMEOUT_SECONDS,
    }
    if connection.base_url is not None:
        options["base_url"] = connection.base_url
    client = module.Anthropic(**options)
    try:
        return tuple(
            ProviderModel(
                model_id=cast(str, model.id),
                display_name=cast(str | None, getattr(model, "display_name", None)),
            )
            for model in client.models.list()
        )
    finally:
        client.close()


def _list_subscription_models(
    connection: ModelConnection,
    api_key: str | None,  # noqa: ARG001
) -> Sequence[ProviderModel]:
    if connection.subscription_vendor != "openai":
        raise ModelCatalogError("unsupported model subscription provider")
    return tuple(
        ProviderModel(model_id=model_id) for model_id in OpenHandsOpenAISubscription().models()
    )


def _model_compatibility(
    connection: ModelConnection,
    execution_model: str,
) -> tuple[ModelAvailability, str, int | None, bool | None]:
    prepare_openhands_import()
    verified = _verified_openhands_models(connection)
    model_name = connection.provider_model_id(execution_model)
    if connection.protocol == "subscription":
        return "available", "Provided by the pinned OpenHands subscription registry", None, True
    if execution_model in verified or model_name in verified:
        return "available", "Verified by the pinned OpenHands SDK", None, True
    try:
        litellm = import_module("litellm")
        model_info = cast(dict[str, Any], litellm.get_model_info(model=execution_model))
    except Exception:
        return "experimental", "Not verified by the pinned OpenHands SDK", None, None
    mode = model_info.get("mode")
    context_window = model_info.get("max_input_tokens") or model_info.get("max_tokens")
    resolved_context = context_window if isinstance(context_window, int) else None
    try:
        supports_tools = bool(litellm.supports_function_calling(model=execution_model))
    except Exception:
        supports_tools = None
    if isinstance(mode, str) and mode != "chat":
        return "unsupported", f"LiteLLM classifies this as {mode}", resolved_context, supports_tools
    return (
        "experimental",
        "Available from the provider but not verified by the pinned OpenHands SDK",
        resolved_context,
        supports_tools,
    )


def _declared_compatibility(
    metadata: PlatformModelMetadata,
) -> tuple[ModelAvailability, str, int | None, bool | None]:
    context_window = metadata.context_window_tokens
    if not _API_MODES.intersection(metadata.protocols):
        return (
            "unsupported",
            "The deployment declares no protocol Heartwood supports: "
            + ", ".join(metadata.protocols),
            context_window,
            metadata.supports_tool_calls,
        )
    # The operator's declaration stands in for OpenHands verification of the model.
    return "available", "Declared by the deployment", context_window, metadata.supports_tool_calls


def declared_input_limit(metadata: PlatformModelMetadata) -> int | None:
    """Return the declared input limit, or the window less the output limit when both are known."""
    if metadata.max_input_tokens is not None:
        return metadata.max_input_tokens
    if metadata.context_window_tokens is not None and metadata.max_output_tokens is not None:
        remainder = metadata.context_window_tokens - metadata.max_output_tokens
        return remainder if remainder > 0 else None
    return None


def declared_api_mode(metadata: PlatformModelMetadata) -> ApiMode | None:
    """Return the one declared OpenHands API mode, or None to let OpenHands choose."""
    modes = sorted(_API_MODES.intersection(metadata.protocols))
    return cast(ApiMode, modes[0]) if len(modes) == 1 else None


def _verified_openhands_models(connection: ModelConnection) -> set[str]:
    try:
        module = import_module("openhands.sdk.llm.utils.verified_models")
        models = cast(dict[str, list[str]], module.VERIFIED_MODELS)
    except Exception:
        return set()
    provider = "anthropic" if connection.protocol == "anthropic" else "openai"
    return set(models.get(provider, ()))


def _discovery_error_code(error: Exception) -> BackendErrorCode:
    """Classify a discovery failure as the agent runtime classifies the same response."""
    # Provider SDKs report HTTP refusals by status; an expired or refused credential is
    # 401 or 403, which the runtime vocabulary leaves unclassified for a 403.
    if getattr(error, "status_code", None) in {401, 403}:
        return BackendErrorCode.PROVIDER_AUTHENTICATION_FAILED
    prepare_openhands_import()
    from openhands.sdk.event.error_classification import classify_exception

    from heartwood.gateway._openhands_failures import classified_error_code

    code = classified_error_code(
        classify_exception(error),
        fallback=BackendErrorCode.PROVIDER_UNAVAILABLE,
    )
    # A model list involves no agent action, so other runtime outcomes do not apply.
    return code if code in _DISCOVERY_ERROR_CODES else BackendErrorCode.PROVIDER_UNAVAILABLE


_DISCOVERY_ERROR_CODES = frozenset(
    {
        BackendErrorCode.PROVIDER_AUTHENTICATION_FAILED,
        BackendErrorCode.PROVIDER_QUOTA_EXHAUSTED,
        BackendErrorCode.PROVIDER_RATE_LIMITED,
        BackendErrorCode.MODEL_CONFIGURATION_INVALID,
        BackendErrorCode.PROVIDER_UNAVAILABLE,
    }
)


def _validate_endpoint(value: str, name: str) -> None:
    try:
        normalized = normalize_endpoint(value)
    except PolicyInputError as error:
        raise ModelCatalogError(f"invalid {name}: {error}") from error
    if normalized != value:
        raise ModelCatalogError(f"{name} must be normalized as {normalized}")


def _validate_base_url(value: str) -> None:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ModelCatalogError("base_url must be an absolute HTTP or HTTPS URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ModelCatalogError("base_url cannot contain credentials, a query, or a fragment")
    try:
        port = parsed.port
    except ValueError as error:
        raise ModelCatalogError("base_url contains an invalid port") from error
    if port == 0:
        raise ModelCatalogError("base_url contains an invalid port")
    if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
        raise ModelCatalogError("remote model connections require HTTPS")


def _origin(value: str) -> tuple[str, str | None, int | None]:
    parsed = urlsplit(value)
    return parsed.scheme, parsed.hostname, parsed.port


def _reject_secret_values(value: object) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = str(key).lower().replace("-", "_")
            if any(marker in normalized for marker in _SECRET_FIELD_MARKERS) and key not in {
                "api_key_env",
                "api_key_file",
            }:
                raise ModelCatalogError("model connections cannot contain inline secret values")
            _reject_secret_values(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_secret_values(nested)
