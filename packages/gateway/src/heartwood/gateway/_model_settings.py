# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Non-secret model profiles shared by the gateway, CLI, and web UI."""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Literal, TypeAlias, cast
from urllib.parse import urlsplit

from heartwood.model_policy import PolicyInputError, normalize_endpoint

CredentialKind: TypeAlias = Literal["environment", "file", "managed-identity", "none"]
CapabilityTier: TypeAlias = Literal["autonomous", "experimental", "supervised"]

_CREDENTIAL_KINDS = {"environment", "file", "managed-identity", "none"}
_CAPABILITY_TIERS = {"autonomous", "experimental", "supervised"}
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MODEL_SETTINGS_FIELDS = {"active_profile", "profiles", "schema_version"}
_MODEL_PROFILE_FIELDS = {
    "api_key_env",
    "api_key_file",
    "api_version",
    "aws_profile_name",
    "aws_region_name",
    "base_url",
    "capability_tier",
    "credential_kind",
    "credential_status",
    "description",
    "model",
    "policy_endpoint",
    "profile_id",
}


class ModelSettingsError(ValueError):
    """Raised when model settings are malformed or unsafe."""


@dataclass(frozen=True, slots=True)
class ModelProfile:
    """One OpenHands-compatible model profile without inline credentials."""

    profile_id: str
    model: str
    policy_endpoint: str
    capability_tier: CapabilityTier = "supervised"
    base_url: str | None = None
    credential_kind: CredentialKind = "environment"
    api_key_env: str | None = None
    api_key_file: str | None = None
    api_version: str | None = None
    aws_region_name: str | None = None
    aws_profile_name: str | None = None
    description: str | None = None

    def validate(self) -> None:
        """Validate profile identifiers, endpoints, and secret references."""
        if not self.profile_id or not self.profile_id.replace("-", "").replace("_", "").isalnum():
            msg = "profile_id must contain only letters, numbers, hyphens, or underscores"
            raise ModelSettingsError(msg)
        if not self.model or "/" not in self.model:
            msg = "model must be a LiteLLM model id such as openai/model-name"
            raise ModelSettingsError(msg)
        if self.capability_tier not in _CAPABILITY_TIERS:
            msg = f"unsupported capability tier: {self.capability_tier}"
            raise ModelSettingsError(msg)
        if self.credential_kind not in _CREDENTIAL_KINDS:
            msg = f"unsupported credential kind: {self.credential_kind}"
            raise ModelSettingsError(msg)
        try:
            normalized = normalize_endpoint(self.policy_endpoint)
        except PolicyInputError as error:
            msg = f"invalid policy_endpoint: {error}"
            raise ModelSettingsError(msg) from error
        if normalized != self.policy_endpoint:
            msg = f"policy_endpoint must be normalized as {normalized}"
            raise ModelSettingsError(msg)
        if self.base_url is not None:
            _validate_base_url(self.base_url)
            if _url_origin(self.base_url) != _url_origin(self.policy_endpoint):
                msg = "base_url and policy_endpoint must use the same origin"
                raise ModelSettingsError(msg)
        if self.credential_kind == "environment":
            if not self.api_key_env or _ENVIRONMENT_NAME.fullmatch(self.api_key_env) is None:
                msg = "environment credentials require a valid api_key_env name"
                raise ModelSettingsError(msg)
            if self.api_key_file is not None:
                msg = "api_key_file is only allowed for file credentials"
                raise ModelSettingsError(msg)
        elif self.credential_kind == "file":
            if not self.api_key_file or not Path(self.api_key_file).is_absolute():
                msg = "file credentials require an absolute api_key_file path"
                raise ModelSettingsError(msg)
            if self.api_key_env is not None:
                msg = "api_key_env is only allowed for environment credentials"
                raise ModelSettingsError(msg)
        elif self.api_key_env is not None or self.api_key_file is not None:
            msg = f"{self.credential_kind} credentials cannot declare API key references"
            raise ModelSettingsError(msg)
        if self.credential_kind == "none" and not self.is_local:
            msg = "credential kind none is allowed only for loopback model endpoints"
            raise ModelSettingsError(msg)

    @property
    def is_local(self) -> bool:
        """Return whether the policy endpoint resolves to loopback HTTP."""
        parsed = urlsplit(self.policy_endpoint)
        return parsed.scheme == "http" and parsed.hostname in _LOOPBACK_HOSTS

    def safe_dict(self) -> dict[str, object]:
        """Return profile metadata that is safe for APIs and logs."""
        return asdict(self)

    @property
    def credential_reference(self) -> str | None:
        """Return the non-secret reference evaluated by deployment policy."""
        if self.credential_kind == "environment":
            return self.api_key_env
        if self.credential_kind == "file":
            return self.api_key_file
        if self.credential_kind == "managed-identity":
            return "managed-identity"
        return None

    def credential_status(self, env: Mapping[str, str] | None = None) -> str:
        """Return a non-secret credential-reference status."""
        active_env = os.environ if env is None else env
        if self.credential_kind in {"managed-identity", "none"}:
            return "configured"
        try:
            self.resolve_api_key(active_env)
        except ModelSettingsError:
            return "missing"
        return "available"

    def resolve_api_key(self, env: Mapping[str, str] | None = None) -> str | None:
        """Resolve an API key at runtime without persisting it."""
        active_env = os.environ if env is None else env
        if self.credential_kind in {"managed-identity", "none"}:
            return None
        if self.credential_kind == "environment":
            value = active_env.get(cast(str, self.api_key_env))
            if not value:
                msg = f"credential environment variable is not available: {self.api_key_env}"
                raise ModelSettingsError(msg)
            return value
        path = Path(cast(str, self.api_key_file))
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError as error:
            msg = f"unable to read credential file: {path}"
            raise ModelSettingsError(msg) from error
        if not value:
            msg = f"credential file is empty: {path}"
            raise ModelSettingsError(msg)
        return value


@dataclass(frozen=True, slots=True)
class ModelSettings:
    """Versioned collection of model profiles and the selected profile."""

    schema_version: str = "heartwood.model-settings.v1"
    active_profile: str | None = None
    profiles: tuple[ModelProfile, ...] = ()

    def validate(self) -> None:
        """Validate the settings collection."""
        if self.schema_version != "heartwood.model-settings.v1":
            msg = f"unsupported model settings schema: {self.schema_version}"
            raise ModelSettingsError(msg)
        for profile in self.profiles:
            profile.validate()
        profile_ids = [profile.profile_id for profile in self.profiles]
        if len(profile_ids) != len(set(profile_ids)):
            msg = "model profile ids must be unique"
            raise ModelSettingsError(msg)
        if self.active_profile is not None and self.active_profile not in profile_ids:
            msg = f"active model profile does not exist: {self.active_profile}"
            raise ModelSettingsError(msg)

    def profile(self, profile_id: str | None = None) -> ModelProfile:
        """Return a selected profile."""
        selected = self.active_profile if profile_id is None else profile_id
        if selected is None:
            msg = "no active model profile is configured"
            raise ModelSettingsError(msg)
        for profile in self.profiles:
            if profile.profile_id == selected:
                return profile
        msg = f"unknown model profile: {selected}"
        raise ModelSettingsError(msg)

    def with_profile(self, profile: ModelProfile) -> ModelSettings:
        """Add or replace one profile."""
        profile.validate()
        profiles = tuple(item for item in self.profiles if item.profile_id != profile.profile_id)
        updated = replace(self, profiles=(*profiles, profile))
        updated.validate()
        return updated

    def without_profile(self, profile_id: str) -> ModelSettings:
        """Remove one profile and clear selection when needed."""
        profiles = tuple(item for item in self.profiles if item.profile_id != profile_id)
        if len(profiles) == len(self.profiles):
            msg = f"unknown model profile: {profile_id}"
            raise ModelSettingsError(msg)
        active = None if self.active_profile == profile_id else self.active_profile
        updated = replace(self, active_profile=active, profiles=profiles)
        updated.validate()
        return updated

    def selecting(self, profile_id: str) -> ModelSettings:
        """Select one existing profile."""
        self.profile(profile_id)
        return replace(self, active_profile=profile_id)

    def safe_dict(self, env: Mapping[str, str] | None = None) -> dict[str, object]:
        """Return API-safe settings with credential status only."""
        return {
            "schema_version": self.schema_version,
            "active_profile": self.active_profile,
            "profiles": [
                {**profile.safe_dict(), "credential_status": profile.credential_status(env)}
                for profile in self.profiles
            ],
        }


class ModelSettingsStore:
    """Load and atomically persist non-secret model settings."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> ModelSettings:
        """Load settings or return an empty collection when absent."""
        if not self.path.exists():
            return ModelSettings()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            msg = f"unable to load model settings {self.path}: {error}"
            raise ModelSettingsError(msg) from error
        return model_settings_from_mapping(raw)

    def save(self, settings: ModelSettings) -> None:
        """Persist settings atomically without writing credentials."""
        settings.validate()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": settings.schema_version,
            "active_profile": settings.active_profile,
            "profiles": [profile.safe_dict() for profile in settings.profiles],
        }
        fd, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        temporary_path = Path(temporary)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump(payload, file, indent=2, sort_keys=True)
                file.write("\n")
            temporary_path.chmod(0o600)
            temporary_path.replace(self.path)
        finally:
            temporary_path.unlink(missing_ok=True)


@dataclass(frozen=True, slots=True)
class ModelPreset:
    """Non-secret defaults for one common OpenHands/LiteLLM provider path."""

    preset_id: str
    label: str
    model_prefix: str
    credential_kind: CredentialKind
    api_key_env: str | None = None
    base_url: str | None = None
    policy_endpoint: str | None = None
    description: str = ""

    def safe_dict(self) -> dict[str, object]:
        """Return serializable preset metadata."""
        return asdict(self)


MODEL_PRESETS: tuple[ModelPreset, ...] = (
    ModelPreset(
        preset_id="local-openai-compatible",
        label="Local OpenAI-Compatible",
        model_prefix="openai/",
        credential_kind="none",
        base_url="http://127.0.0.1:8765/v1",
        policy_endpoint="http://127.0.0.1:8765/v1/chat/completions",
        description="Ollama, vLLM, SGLang, llama.cpp, or another loopback endpoint.",
    ),
    ModelPreset(
        preset_id="openai",
        label="OpenAI",
        model_prefix="openai/",
        credential_kind="environment",
        api_key_env="OPENAI_API_KEY",
        policy_endpoint="https://api.openai.com/v1/chat/completions",
        description="Requires separate institutional authorization for regulated data.",
    ),
    ModelPreset(
        preset_id="anthropic",
        label="Anthropic",
        model_prefix="anthropic/",
        credential_kind="environment",
        api_key_env="ANTHROPIC_API_KEY",
        policy_endpoint="https://api.anthropic.com/v1/messages",
        description="Requires separate institutional authorization for regulated data.",
    ),
    ModelPreset(
        preset_id="azure-openai",
        label="Azure OpenAI",
        model_prefix="azure/",
        credential_kind="environment",
        api_key_env="AZURE_API_KEY",
        description="Requires deployment-specific base and policy endpoint URLs.",
    ),
    ModelPreset(
        preset_id="bedrock",
        label="Amazon Bedrock",
        model_prefix="bedrock/",
        credential_kind="managed-identity",
        description="Uses the platform AWS identity and deployment-specific policy endpoint.",
    ),
    ModelPreset(
        preset_id="vertex-ai",
        label="Google Vertex AI",
        model_prefix="vertex_ai/",
        credential_kind="managed-identity",
        description="Uses the platform Google identity and deployment-specific policy endpoint.",
    ),
)


def model_profile_from_preset(preset_id: str, model_name: str) -> ModelProfile:
    """Build a selected provider profile from a safe preset and model name."""
    preset = next((item for item in MODEL_PRESETS if item.preset_id == preset_id), None)
    if preset is None:
        msg = f"unknown model provider preset: {preset_id}"
        raise ModelSettingsError(msg)
    if preset.policy_endpoint is None:
        msg = f"{preset.label} requires advanced endpoint configuration"
        raise ModelSettingsError(msg)
    normalized_name = model_name.strip()
    if not normalized_name:
        msg = "model name must be a non-empty string"
        raise ModelSettingsError(msg)
    if any(character.isspace() for character in normalized_name):
        msg = "model name cannot contain whitespace"
        raise ModelSettingsError(msg)
    model = (
        normalized_name
        if normalized_name.startswith(preset.model_prefix)
        else f"{preset.model_prefix}{normalized_name}"
    )
    profile = ModelProfile(
        profile_id=preset.preset_id,
        model=model,
        policy_endpoint=preset.policy_endpoint,
        base_url=preset.base_url,
        credential_kind=preset.credential_kind,
        api_key_env=preset.api_key_env,
        description=preset.description,
    )
    profile.validate()
    return profile


def model_settings_path(workspace: Path, env: Mapping[str, str] | None = None) -> Path:
    """Resolve the shared settings path for a session workspace."""
    active_env = os.environ if env is None else env
    configured = active_env.get("HEARTWOOD_MODEL_SETTINGS")
    if configured:
        return Path(configured)
    return workspace.parent / "models.json"


def model_settings_from_mapping(value: object) -> ModelSettings:
    """Validate model settings parsed from JSON or an API request."""
    if not isinstance(value, dict):
        msg = "model settings must be an object"
        raise ModelSettingsError(msg)
    _reject_secret_values(value)
    _reject_unknown_fields(value, _MODEL_SETTINGS_FIELDS, "model settings")
    raw_profiles = value.get("profiles", [])
    if not isinstance(raw_profiles, list):
        msg = "model settings profiles must be a list"
        raise ModelSettingsError(msg)
    settings = ModelSettings(
        schema_version=_string(value, "schema_version"),
        active_profile=_optional_string(value.get("active_profile"), "active_profile"),
        profiles=tuple(model_profile_from_mapping(item) for item in raw_profiles),
    )
    settings.validate()
    return settings


def model_profile_from_mapping(value: object) -> ModelProfile:
    """Validate one profile parsed from JSON or an API request."""
    if not isinstance(value, dict):
        msg = "model profile must be an object"
        raise ModelSettingsError(msg)
    _reject_secret_values(value)
    _reject_unknown_fields(value, _MODEL_PROFILE_FIELDS, "model profile")
    capability = _optional_string(value.get("capability_tier"), "capability_tier")
    credential = _optional_string(value.get("credential_kind"), "credential_kind")
    profile = ModelProfile(
        profile_id=_string(value, "profile_id"),
        model=_string(value, "model"),
        policy_endpoint=_string(value, "policy_endpoint"),
        capability_tier=cast(CapabilityTier, capability or "supervised"),
        base_url=_optional_string(value.get("base_url"), "base_url"),
        credential_kind=cast(CredentialKind, credential or "environment"),
        api_key_env=_optional_string(value.get("api_key_env"), "api_key_env"),
        api_key_file=_optional_string(value.get("api_key_file"), "api_key_file"),
        api_version=_optional_string(value.get("api_version"), "api_version"),
        aws_region_name=_optional_string(value.get("aws_region_name"), "aws_region_name"),
        aws_profile_name=_optional_string(value.get("aws_profile_name"), "aws_profile_name"),
        description=_optional_string(value.get("description"), "description"),
    )
    profile.validate()
    return profile


def _validate_base_url(value: str) -> None:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        msg = "base_url must be an absolute HTTP or HTTPS URL"
        raise ModelSettingsError(msg)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        msg = "base_url cannot contain credentials, a query, or a fragment"
        raise ModelSettingsError(msg)


def _url_origin(value: str) -> tuple[str, str | None, int | None]:
    parsed = urlsplit(value)
    default_port = 443 if parsed.scheme == "https" else 80 if parsed.scheme == "http" else None
    return parsed.scheme, parsed.hostname, parsed.port or default_port


def _string(value: Mapping[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        msg = f"{key} must be a non-empty string"
        raise ModelSettingsError(msg)
    return item


def _optional_string(value: object, key: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        msg = f"{key} must be a non-empty string"
        raise ModelSettingsError(msg)
    return value


def _reject_secret_values(value: Mapping[str, Any]) -> None:
    forbidden = {
        "accesstoken",
        "apikey",
        "authorization",
        "clientsecret",
        "password",
        "secret",
        "token",
    }
    for key, item in value.items():
        normalized_key = "".join(character for character in key.lower() if character.isalnum())
        if normalized_key in forbidden:
            msg = f"inline secret field is not allowed: {key}"
            raise ModelSettingsError(msg)
        if isinstance(item, dict):
            _reject_secret_values(item)
        elif isinstance(item, list):
            for child in item:
                if isinstance(child, dict):
                    _reject_secret_values(child)


def _reject_unknown_fields(
    value: Mapping[str, Any],
    allowed: set[str],
    label: str,
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        msg = f"{label} contains unsupported fields: {', '.join(unknown)}"
        raise ModelSettingsError(msg)
