# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Provider route configuration with file-based secret references."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypeAlias, cast

from heartwood.model_policy import PolicyInputError, normalize_endpoint

ProviderId: TypeAlias = Literal[
    "anthropic",
    "azure-openai",
    "bedrock",
    "llama-cpp",
    "ollama",
    "openai",
    "openai-compatible",
    "vertex-ai",
    "vllm",
]
ProviderAuth: TypeAlias = Literal["managed-identity", "none", "secret-file"]

_SUPPORTED_PROVIDERS: set[str] = {
    "anthropic",
    "azure-openai",
    "bedrock",
    "llama-cpp",
    "ollama",
    "openai",
    "openai-compatible",
    "vertex-ai",
    "vllm",
}
_SUPPORTED_AUTH: set[str] = {"managed-identity", "none", "secret-file"}
_SUPPORTED_CAPABILITY_TIERS = {"autonomous", "experimental", "supervised"}
_LOCAL_PROVIDERS = {"llama-cpp", "ollama", "openai-compatible", "vllm"}
_INLINE_SECRET_KEYS = {
    "access_key",
    "api_key",
    "client_secret",
    "password",
    "secret",
    "secret_access_key",
    "token",
}


class ProviderConfigError(ValueError):
    """Raised when provider route configuration is unsafe or malformed."""


@dataclass(frozen=True, slots=True)
class ProviderRoute:
    """One selectable provider route."""

    route_id: str
    provider: ProviderId
    endpoint: str
    model: str
    capability_tier: str
    auth: ProviderAuth
    secret_file: Path | None = None
    notes: str | None = None

    def safe_metadata(self) -> dict[str, str]:
        """Return non-secret metadata safe to persist in events or docs."""
        return {
            "route_id": self.route_id,
            "provider": self.provider,
            "endpoint": self.endpoint,
            "model": self.model,
            "capability_tier": self.capability_tier,
            "auth": self.auth,
        }


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """Validated provider route configuration."""

    schema_version: str
    routes: tuple[ProviderRoute, ...]
    default_route: str | None = None

    def route(self, route_id: str | None = None) -> ProviderRoute:
        """Return a configured route by id or the configured default route."""
        selected = route_id or self.default_route
        if selected is None:
            msg = "provider route id is required because no default_route is configured"
            raise ProviderConfigError(msg)
        for route in self.routes:
            if route.route_id == selected:
                return route
        msg = f"unknown provider route: {selected}"
        raise ProviderConfigError(msg)

    def secret_paths(self) -> tuple[Path, ...]:
        """Return all declared runtime secret-file paths."""
        return tuple(route.secret_file for route in self.routes if route.secret_file is not None)


def load_provider_config(path: Path) -> ProviderConfig:
    """Load and validate a provider route TOML file."""
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return provider_config_from_mapping(data)


def provider_config_from_mapping(data: dict[str, Any]) -> ProviderConfig:
    """Validate provider route data already loaded from TOML or another structured source."""
    _reject_inline_secrets(data)
    schema_version = _required_string(data, "schema_version")
    if schema_version != "heartwood.provider-config.v1":
        msg = f"unsupported provider config schema: {schema_version}"
        raise ProviderConfigError(msg)
    default_route = _optional_string(data.get("default_route"), "default_route")
    raw_routes = data.get("routes")
    if not isinstance(raw_routes, list) or not raw_routes:
        msg = "provider config must include at least one route"
        raise ProviderConfigError(msg)
    routes = tuple(_route_from_mapping(route) for route in raw_routes)
    route_ids = [route.route_id for route in routes]
    if len(route_ids) != len(set(route_ids)):
        msg = "provider route ids must be unique"
        raise ProviderConfigError(msg)
    config = ProviderConfig(
        schema_version=schema_version,
        default_route=default_route,
        routes=routes,
    )
    if default_route is not None:
        config.route(default_route)
    return config


def _route_from_mapping(data: object) -> ProviderRoute:
    if not isinstance(data, dict):
        msg = "provider route must be a table"
        raise ProviderConfigError(msg)
    _reject_inline_secrets(data)
    provider_value = _required_string(data, "provider")
    if provider_value not in _SUPPORTED_PROVIDERS:
        msg = f"unsupported provider: {provider_value}"
        raise ProviderConfigError(msg)
    capability_tier = _required_string(data, "capability_tier")
    if capability_tier not in _SUPPORTED_CAPABILITY_TIERS:
        msg = f"unsupported capability tier: {capability_tier}"
        raise ProviderConfigError(msg)
    auth_value = _required_string(data, "auth")
    if auth_value not in _SUPPORTED_AUTH:
        msg = f"unsupported provider auth mode: {auth_value}"
        raise ProviderConfigError(msg)
    endpoint = _required_string(data, "endpoint")
    try:
        normalized_endpoint = normalize_endpoint(endpoint)
    except PolicyInputError as error:
        msg = f"invalid provider endpoint: {error}"
        raise ProviderConfigError(msg) from error
    secret_file = _secret_file(data.get("secret_file"), auth=auth_value)
    if provider_value not in _LOCAL_PROVIDERS and auth_value == "none":
        msg = f"provider {provider_value} requires secret-file or managed-identity auth"
        raise ProviderConfigError(msg)
    if auth_value != "secret-file" and secret_file is not None:
        msg = "secret_file is only allowed with secret-file auth"
        raise ProviderConfigError(msg)
    return ProviderRoute(
        route_id=_required_string(data, "route_id"),
        provider=cast(ProviderId, provider_value),
        endpoint=normalized_endpoint,
        model=_required_string(data, "model"),
        capability_tier=capability_tier,
        auth=cast(ProviderAuth, auth_value),
        secret_file=secret_file,
        notes=_optional_string(data.get("notes"), "notes"),
    )


def _secret_file(value: object, *, auth: str) -> Path | None:
    if auth != "secret-file":
        if value is None:
            return None
        if not isinstance(value, str) or not value:
            msg = "secret_file must be a non-empty string"
            raise ProviderConfigError(msg)
        return Path(value)
    if not isinstance(value, str) or not value:
        msg = "secret-file auth requires secret_file"
        raise ProviderConfigError(msg)
    path = Path(value)
    if not path.is_absolute():
        msg = "secret_file must be an absolute runtime mount path"
        raise ProviderConfigError(msg)
    return path


def _required_string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        msg = f"{key} must be a non-empty string"
        raise ProviderConfigError(msg)
    return value


def _optional_string(value: object, key: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        msg = f"{key} must be a non-empty string"
        raise ProviderConfigError(msg)
    return value


def _reject_inline_secrets(data: dict[str, Any]) -> None:
    for key, value in data.items():
        if key in _INLINE_SECRET_KEYS:
            msg = f"inline secret field is not allowed: {key}"
            raise ProviderConfigError(msg)
        if isinstance(value, dict):
            _reject_inline_secrets(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _reject_inline_secrets(item)
