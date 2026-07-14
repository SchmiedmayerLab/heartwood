# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Model connections and upstream-backed catalog discovery."""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from importlib import import_module
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from urllib.parse import urlsplit

from heartwood.gateway._model_settings import CredentialKind
from heartwood.model_policy import PolicyInputError, normalize_endpoint

type ConnectionProtocol = Literal["anthropic", "openai", "openai-compatible", "static"]
type ConnectionSource = Literal["built-in", "platform", "user"]
type ModelAvailability = Literal["available", "experimental", "unsupported"]

_CONNECTION_PROTOCOLS = {"anthropic", "openai", "openai-compatible", "static"}
_CONNECTION_SOURCES = {"built-in", "platform", "user"}
_CREDENTIAL_KINDS = {"environment", "file", "managed-identity", "none"}
_CATALOG_TIMEOUT_SECONDS = 30.0
_CATALOG_MAX_RETRIES = 1
_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_CONNECTION_FIELDS = {
    "api_key_env",
    "api_key_file",
    "api_version",
    "aws_profile_name",
    "aws_region_name",
    "base_url",
    "catalog_endpoint",
    "connection_id",
    "credential_kind",
    "description",
    "label",
    "model_prefix",
    "policy_endpoint",
    "protocol",
    "source",
    "static_models",
}
_SECRET_FIELD_MARKERS = ("api_key", "apikey", "password", "secret", "token")


class ModelCatalogError(ValueError):
    """Raised when model connection metadata or discovery is invalid."""


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
            if self.protocol != "static" and self.catalog_endpoint is None:
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

    def safe_dict(self, env: Mapping[str, str]) -> dict[str, object]:
        """Return API-safe connection metadata."""
        return {
            **asdict(self),
            "accepts_token": self.credential_kind == "environment",
            "credential_status": self.credential_status(env),
        }


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
        compatibility: Callable[
            [ModelConnection, str],
            tuple[ModelAvailability, str, int | None, bool | None],
        ]
        | None = None,
        cache_ttl_seconds: int = 300,
    ) -> None:
        self._openai_lister = openai_lister or _list_openai_models
        self._anthropic_lister = anthropic_lister or _list_anthropic_models
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
            else:
                discovered = tuple(self._openai_lister(connection, api_key))
        except ModelCatalogError:
            raise
        except Exception as error:
            raise ModelCatalogError(_safe_discovery_error(error)) from error
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
            availability, reason, context_window, supports_tools = self._compatibility(
                connection, execution_model
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
        connection_id="local",
        label="Local",
        protocol="openai-compatible",
        model_prefix="openai/",
        source="built-in",
        credential_kind="none",
        base_url="http://127.0.0.1:8765/v1",
        catalog_endpoint="http://127.0.0.1:8765/v1/models",
        policy_endpoint="http://127.0.0.1:8765/v1/chat/completions",
        description="Models reported by the local runtime.",
    ),
    ModelConnection(
        connection_id="openai",
        label="OpenAI",
        protocol="openai",
        model_prefix="openai/",
        source="built-in",
        credential_kind="environment",
        api_key_env="OPENAI_API_KEY",
        catalog_endpoint="https://api.openai.com/v1/models",
        policy_endpoint="https://api.openai.com/v1/chat/completions",
        description="Models available to the supplied OpenAI credential.",
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
    ),
    ModelConnection(
        connection_id="custom-api",
        label="Custom API",
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


def load_model_connections(path: Path | None) -> tuple[ModelConnection, ...]:
    """Load platform-provided connections and combine them with built-ins."""
    if path is None:
        for connection in BUILT_IN_MODEL_CONNECTIONS:
            connection.validate(configurable=connection.connection_id == "custom-api")
        return BUILT_IN_MODEL_CONNECTIONS
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ModelCatalogError(f"unable to load model connections {path}") from error
    return model_connections_from_mapping(value)


def model_connections_from_mapping(value: object) -> tuple[ModelConnection, ...]:
    """Validate configured connections and combine them with built-ins."""
    for connection in BUILT_IN_MODEL_CONNECTIONS:
        connection.validate(configurable=connection.connection_id == "custom-api")
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != "heartwood.model-connections.v1"
    ):
        raise ModelCatalogError("unsupported model connections schema")
    raw_connections = value.get("connections")
    if not isinstance(raw_connections, list):
        raise ModelCatalogError("model connections must be a list")
    _reject_secret_values(value)
    configured = tuple(_connection_from_mapping(item) for item in raw_connections)
    if any(connection.source != "platform" for connection in configured):
        raise ModelCatalogError("configured model connections must use source platform")
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
    parsed = urlsplit(normalized_base)
    loopback = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1", "localhost"}
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
    if not has_token and not loopback:
        raise ModelCatalogError(
            "a remote custom API requires a token or managed platform connection"
        )
    connection.validate()
    return connection


def _connection_from_mapping(value: object) -> ModelConnection:
    if not isinstance(value, dict):
        raise ModelCatalogError("model connection must be an object")
    unsupported = set(value) - _CONNECTION_FIELDS
    if unsupported:
        raise ModelCatalogError(
            f"model connection contains unsupported fields: {', '.join(sorted(unsupported))}"
        )
    static_models = value.get("static_models", [])
    if not isinstance(static_models, list) or not all(
        isinstance(item, str) for item in static_models
    ):
        raise ModelCatalogError("static_models must be a list of strings")
    connection = ModelConnection(
        connection_id=_required_string(value, "connection_id"),
        label=_required_string(value, "label"),
        protocol=cast(ConnectionProtocol, _required_string(value, "protocol")),
        model_prefix=_required_string(value, "model_prefix"),
        source=cast(ConnectionSource, value.get("source", "platform")),
        credential_kind=cast(CredentialKind, _required_string(value, "credential_kind")),
        policy_endpoint=_optional_string(value, "policy_endpoint"),
        catalog_endpoint=_optional_string(value, "catalog_endpoint"),
        base_url=_optional_string(value, "base_url"),
        api_key_env=_optional_string(value, "api_key_env"),
        api_key_file=_optional_string(value, "api_key_file"),
        api_version=_optional_string(value, "api_version"),
        aws_region_name=_optional_string(value, "aws_region_name"),
        aws_profile_name=_optional_string(value, "aws_profile_name"),
        description=cast(str, value.get("description", "")),
        static_models=tuple(cast(list[str], static_models)),
    )
    if not isinstance(connection.description, str):
        raise ModelCatalogError("description must be a string")
    connection.validate()
    return connection


def _list_openai_models(
    connection: ModelConnection,
    api_key: str | None,
) -> Sequence[ProviderModel]:
    module = import_module("openai")
    options: dict[str, object] = {
        "api_key": api_key or "not-required",
        "max_retries": _CATALOG_MAX_RETRIES,
        "timeout": _CATALOG_TIMEOUT_SECONDS,
    }
    if connection.base_url is not None:
        options["base_url"] = connection.base_url
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


def _model_compatibility(
    connection: ModelConnection,
    execution_model: str,
) -> tuple[ModelAvailability, str, int | None, bool | None]:
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    os.environ.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")
    verified = _verified_openhands_models(connection)
    model_name = connection.provider_model_id(execution_model)
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


def _verified_openhands_models(connection: ModelConnection) -> set[str]:
    try:
        module = import_module("openhands.sdk.llm.utils.verified_models")
        models = cast(dict[str, list[str]], module.VERIFIED_MODELS)
    except Exception:
        return set()
    provider = "anthropic" if connection.protocol == "anthropic" else "openai"
    return set(models.get(provider, ()))


def _safe_discovery_error(error: Exception) -> str:
    name = type(error).__name__.lower()
    if "authentication" in name or "permission" in name:
        return "model provider rejected the configured credential"
    if "timeout" in name:
        return "model provider catalog request timed out"
    if "connection" in name:
        return "model provider catalog is unavailable"
    return "model provider catalog request failed"


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


def _required_string(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise ModelCatalogError(f"{key} must be a non-empty string")
    return item


def _optional_string(value: Mapping[str, object], key: str) -> str | None:
    item = value.get(key)
    if item is None:
        return None
    if not isinstance(item, str) or not item:
        raise ModelCatalogError(f"{key} must be a non-empty string when provided")
    return item
