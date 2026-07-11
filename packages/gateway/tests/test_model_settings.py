# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import json
import stat
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from heartwood.gateway import (
    MODEL_PRESETS,
    ModelProfile,
    ModelSettings,
    ModelSettingsError,
    ModelSettingsStore,
    model_profile_from_mapping,
    model_settings_from_mapping,
    model_settings_path,
)


def test_local_profile_needs_no_secret_and_reports_configured() -> None:
    profile = _local_profile()

    profile.validate()

    assert profile.is_local is True
    assert profile.credential_status({}) == "configured"
    assert profile.resolve_api_key({}) is None
    assert profile.credential_reference is None


def test_environment_and_file_credentials_resolve_only_at_runtime(tmp_path: Path) -> None:
    environment = ModelProfile(
        profile_id="api",
        model="openai/configured-model",
        policy_endpoint="https://api.openai.com/v1/chat/completions",
        credential_kind="environment",
        api_key_env="OPENAI_API_KEY",
    )
    secret_file = tmp_path / "provider.key"
    secret_file.write_text("file-secret\n", encoding="utf-8")
    file_profile = environment.__class__(
        profile_id="file-api",
        model=environment.model,
        policy_endpoint=environment.policy_endpoint,
        credential_kind="file",
        api_key_file=str(secret_file),
    )

    assert environment.credential_status({}) == "missing"
    assert environment.credential_status({"OPENAI_API_KEY": ""}) == "missing"
    assert environment.resolve_api_key({"OPENAI_API_KEY": "env-secret"}) == "env-secret"
    assert environment.credential_reference == "OPENAI_API_KEY"
    assert file_profile.credential_status({}) == "available"
    assert file_profile.resolve_api_key({}) == "file-secret"
    assert file_profile.credential_reference == str(secret_file)
    assert "env-secret" not in str(environment.safe_dict())
    assert "file-secret" not in str(file_profile.safe_dict())

    secret_file.write_text("   \n", encoding="utf-8")
    assert file_profile.credential_status({}) == "missing"
    with pytest.raises(ModelSettingsError, match="credential file is empty"):
        file_profile.resolve_api_key({})


def test_profiles_reject_inline_secrets_and_unauthenticated_remote_routes() -> None:
    with pytest.raises(ModelSettingsError, match="inline secret"):
        model_profile_from_mapping(
            {
                "profile_id": "unsafe",
                "model": "openai/model",
                "policy_endpoint": "https://api.openai.com/v1/chat/completions",
                "credential_kind": "environment",
                "api_key_env": "OPENAI_API_KEY",
                "api_key": "must-not-be-stored",
            }
        )

    with pytest.raises(ModelSettingsError, match="only for loopback"):
        ModelProfile(
            profile_id="unsafe",
            model="openai/model",
            policy_endpoint="https://api.openai.com/v1/chat/completions",
            credential_kind="none",
        ).validate()

    with pytest.raises(ModelSettingsError, match="inline secret"):
        model_profile_from_mapping(
            {
                **_local_profile().safe_dict(),
                "apiKey": "must-not-be-accepted",
            }
        )

    with pytest.raises(ModelSettingsError, match="unsupported fields"):
        model_profile_from_mapping(
            {
                **_local_profile().safe_dict(),
                "unexpected": "value",
            }
        )


def test_profile_accepts_bracketed_ipv6_loopback_without_credentials() -> None:
    profile = ModelProfile(
        profile_id="local-ipv6",
        model="openai/local-model",
        base_url="http://[::1]:8765/v1",
        policy_endpoint="http://[::1]:8765/v1/chat/completions",
        credential_kind="none",
    )

    profile.validate()

    assert profile.is_local is True


def test_settings_store_round_trips_atomically_without_secrets(tmp_path: Path) -> None:
    path = tmp_path / "settings" / "models.json"
    store = ModelSettingsStore(path)
    settings = ModelSettings().with_profile(_local_profile()).selecting("local")

    store.save(settings)

    assert store.load() == settings
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "local-model-secret" not in path.read_text(encoding="utf-8")


def test_settings_add_select_and_remove_profiles() -> None:
    settings = ModelSettings().with_profile(_local_profile()).selecting("local")

    removed = settings.without_profile("local")

    assert removed.active_profile is None
    assert removed.profiles == ()
    with pytest.raises(ModelSettingsError, match="unknown model profile"):
        removed.selecting("missing")


def test_mapping_validation_rejects_unknown_active_profile() -> None:
    with pytest.raises(ModelSettingsError, match="does not exist"):
        model_settings_from_mapping(
            {
                "schema_version": "heartwood.model-settings.v1",
                "active_profile": "missing",
                "profiles": [],
            }
        )


def test_presets_are_non_secret_openhands_configuration_hints() -> None:
    preset_ids = {preset.preset_id for preset in MODEL_PRESETS}

    assert preset_ids == {
        "anthropic",
        "azure-openai",
        "bedrock",
        "local-openai-compatible",
        "openai",
        "vertex-ai",
    }
    assert all("api_key" not in preset.safe_dict() for preset in MODEL_PRESETS)


def test_managed_identity_has_one_stable_policy_reference() -> None:
    profile = ModelProfile(
        profile_id="bedrock",
        model="bedrock/model",
        policy_endpoint="https://bedrock-runtime.us-west-2.amazonaws.com/model/model/invoke",
        credential_kind="managed-identity",
    )

    profile.validate()

    assert profile.credential_reference == "managed-identity"


def test_settings_path_can_be_overridden_without_using_home(tmp_path: Path) -> None:
    workspace = tmp_path / "state" / "sessions"

    assert model_settings_path(workspace, {}) == workspace.parent / "models.json"
    assert model_settings_path(
        workspace, {"HEARTWOOD_MODEL_SETTINGS": "/run/heartwood/models.json"}
    ) == Path("/run/heartwood/models.json")


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"profile_id": "bad/id"}, "profile_id"),
        ({"model": "model-without-provider"}, "LiteLLM model id"),
        ({"capability_tier": "unsupported"}, "capability tier"),
        ({"credential_kind": "inline"}, "credential kind"),
        ({"policy_endpoint": "not-a-url"}, "invalid policy_endpoint"),
        (
            {"policy_endpoint": "http://LOCALHOST:8765/v1/chat/completions"},
            "must be normalized",
        ),
        ({"base_url": "relative/path"}, "absolute HTTP"),
        ({"base_url": "https://user:pass@example.com"}, "cannot contain"),
        ({"base_url": "http://127.0.0.1:9000/v1"}, "same origin"),
    ],
)
def test_profile_validation_rejects_malformed_configuration(
    changes: dict[str, object],
    message: str,
) -> None:
    profile = replace(_local_profile(), **cast(Any, changes))

    with pytest.raises(ModelSettingsError, match=message):
        profile.validate()


def test_profile_validation_enforces_credential_reference_shapes(tmp_path: Path) -> None:
    remote = ModelProfile(
        profile_id="remote",
        model="openai/model",
        policy_endpoint="https://api.openai.com/v1/chat/completions",
        credential_kind="environment",
        api_key_env="OPENAI_API_KEY",
    )

    invalid_profiles = (
        replace(remote, api_key_env=None),
        replace(remote, api_key_env="1INVALID"),
        replace(remote, api_key_file="/run/secrets/key"),
        replace(remote, credential_kind="file", api_key_env=None, api_key_file="relative"),
        replace(
            remote,
            credential_kind="file",
            api_key_env="OPENAI_API_KEY",
            api_key_file="/run/secrets/key",
        ),
        replace(remote, credential_kind="managed-identity", api_key_env="OPENAI_API_KEY"),
    )
    for profile in invalid_profiles:
        with pytest.raises(ModelSettingsError):
            profile.validate()

    with pytest.raises(ModelSettingsError, match="not available"):
        remote.resolve_api_key({})
    missing_file = replace(
        remote,
        credential_kind="file",
        api_key_env=None,
        api_key_file=str(tmp_path / "missing"),
    )
    assert missing_file.credential_status({}) == "missing"
    with pytest.raises(ModelSettingsError, match="unable to read"):
        missing_file.resolve_api_key({})
    empty_file = tmp_path / "empty"
    empty_file.write_text("", encoding="utf-8")
    with pytest.raises(ModelSettingsError, match="empty"):
        replace(missing_file, api_key_file=str(empty_file)).resolve_api_key({})


def test_settings_validation_and_store_report_malformed_state(tmp_path: Path) -> None:
    profile = _local_profile()
    invalid_settings = (
        ModelSettings(schema_version="unsupported"),
        ModelSettings(profiles=(profile, profile)),
        ModelSettings(active_profile="missing"),
    )
    for settings in invalid_settings:
        with pytest.raises(ModelSettingsError):
            settings.validate()

    with pytest.raises(ModelSettingsError, match="unknown model profile"):
        ModelSettings().without_profile("missing")

    path = tmp_path / "models.json"
    path.write_text("{", encoding="utf-8")
    with pytest.raises(ModelSettingsError, match="unable to load"):
        ModelSettingsStore(path).load()


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ([], "must be an object"),
        (
            {"schema_version": "heartwood.model-settings.v1", "profiles": {}},
            "profiles must be a list",
        ),
        ({"schema_version": "heartwood.model-settings.v1", "profiles": ["bad"]}, "profile"),
        (
            {"schema_version": "heartwood.model-settings.v1", "active_profile": 7},
            "active_profile",
        ),
    ],
)
def test_settings_mapping_rejects_malformed_values(value: object, message: str) -> None:
    with pytest.raises(ModelSettingsError, match=message):
        model_settings_from_mapping(value)


def test_mapping_rejects_nested_secret_values() -> None:
    value = {
        "schema_version": "heartwood.model-settings.v1",
        "profiles": [
            {
                **_local_profile().safe_dict(),
                "metadata": [{"token": "must-not-persist"}],
            }
        ],
    }

    with pytest.raises(ModelSettingsError, match="inline secret"):
        model_settings_from_mapping(value)


def test_profile_mapping_applies_defaults_and_rejects_empty_optional_values() -> None:
    mapped = model_profile_from_mapping(
        {
            "profile_id": "remote",
            "model": "openai/model",
            "policy_endpoint": "https://api.openai.com/v1/chat/completions",
            "api_key_env": "OPENAI_API_KEY",
        }
    )

    assert mapped.capability_tier == "supervised"
    assert mapped.credential_kind == "environment"
    with pytest.raises(ModelSettingsError, match="description"):
        model_profile_from_mapping({**mapped.safe_dict(), "description": ""})


def test_store_rejects_secret_field_in_existing_json(tmp_path: Path) -> None:
    path = tmp_path / "models.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "heartwood.model-settings.v1",
                "profiles": [{**_local_profile().safe_dict(), "password": "secret"}],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ModelSettingsError, match="inline secret"):
        ModelSettingsStore(path).load()


def _local_profile() -> ModelProfile:
    return ModelProfile(
        profile_id="local",
        model="openai/local-model",
        base_url="http://127.0.0.1:8765/v1",
        policy_endpoint="http://127.0.0.1:8765/v1/chat/completions",
        credential_kind="none",
        description="Local OpenAI-compatible runtime.",
    )
