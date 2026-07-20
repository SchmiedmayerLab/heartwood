# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from pathlib import Path

import pytest

from heartwood.adapters.platform import GenericPlatformAdapter
from heartwood.gateway import (
    CredentialStore,
    CredentialStoreError,
    ModelCatalogService,
    ModelProfile,
    ProjectContext,
    ProviderModel,
    RestGateway,
    RestRequest,
    SessionGateway,
)


class FakeKeyring:
    priority = 1.0

    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.values.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.values[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        del self.values[(service, username)]


class FailingKeyring(FakeKeyring):
    def get_password(self, _service: str, _username: str) -> str | None:
        raise RuntimeError("synthetic keyring failure")


def test_process_and_keyring_credentials_never_enter_project_state(tmp_path: Path) -> None:
    keyring = FakeKeyring()
    store = CredentialStore(
        project_root=tmp_path,
        capabilities=GenericPlatformAdapter().capabilities(),
        env={"ENVIRONMENT_TOKEN": "environment-secret"},
        keyring_backend=keyring,
    )

    store.save("PROCESS_TOKEN", "process-secret")
    store.save("PERSISTED_TOKEN", "persisted-secret", remember=True)

    assert store.availability().persistence_available is True
    assert store.resolve("PROCESS_TOKEN") == "process-secret"
    assert store.resolve("ENVIRONMENT_TOKEN") == "environment-secret"
    assert store.resolve("PERSISTED_TOKEN") == "persisted-secret"
    assert store.status("PROCESS_TOKEN").source == "process"
    assert store.status("ENVIRONMENT_TOKEN").source == "environment"
    assert store.status("PERSISTED_TOKEN").source == "process"
    assert not any(tmp_path.iterdir())

    store.clear_process_values()
    assert store.status("PERSISTED_TOKEN").source == "keyring"
    store.forget("PERSISTED_TOKEN")
    assert store.resolve("PERSISTED_TOKEN") is None


def test_process_only_store_rejects_persistence_and_invalid_bindings(tmp_path: Path) -> None:
    store = CredentialStore(
        project_root=tmp_path,
        capabilities=GenericPlatformAdapter().capabilities(),
        env={},
        use_system_keyring=False,
    )

    assert store.availability().persistence_available is False
    with pytest.raises(CredentialStoreError, match="persistence is unavailable"):
        store.save("OPENAI_API_KEY", "secret", remember=True)
    with pytest.raises(CredentialStoreError, match="environment-style"):
        store.resolve("not a binding")
    with pytest.raises(CredentialStoreError, match="must not be empty"):
        store.save("OPENAI_API_KEY", " ")


def test_environment_credential_can_be_explicitly_remembered(tmp_path: Path) -> None:
    keyring = FakeKeyring()
    store = CredentialStore(
        project_root=tmp_path,
        capabilities=GenericPlatformAdapter().capabilities(),
        env={"OPENAI_API_KEY": "environment-secret"},
        keyring_backend=keyring,
    )
    gateway = SessionGateway(
        project=ProjectContext(tmp_path),
        env={"OPENAI_API_KEY": "environment-secret"},
        credential_store=store,
        model_catalog_service=ModelCatalogService(
            openai_lister=lambda _connection, _api_key: (ProviderModel("gpt-synthetic"),)
        ),
    )

    gateway.discover_models("openai", refresh=True, remember=True)
    store.clear_process_values()

    assert store.status("OPENAI_API_KEY").source == "environment"
    assert keyring.values


def test_failed_keyring_degrades_settings_and_returns_typed_validation_error(
    tmp_path: Path,
) -> None:
    store = CredentialStore(
        project_root=tmp_path,
        capabilities=GenericPlatformAdapter().capabilities(),
        env={},
        keyring_backend=FailingKeyring(),
    )
    gateway = SessionGateway(
        project=ProjectContext(tmp_path),
        env={},
        credential_store=store,
    )
    profile = ModelProfile(
        profile_id="openai",
        model="openai/gpt-synthetic",
        policy_endpoint="https://api.openai.com/v1/chat/completions",
        credential_kind="environment",
        api_key_env="OPENAI_API_KEY",
    )
    gateway.save_model_profile(profile)
    gateway.select_model_profile(profile.profile_id)
    rest = RestGateway(gateway)

    settings = rest.handle(RestRequest(method="GET", path="/settings/models"))
    validation = rest.handle(
        RestRequest(method="GET", path="/settings/models/validation?profile_id=openai")
    )

    assert settings.status_code == 200
    assert validation.status_code == 422
    credential_bindings = settings.body["credential_bindings"]
    assert isinstance(credential_bindings, list)
    credential = next(
        binding
        for binding in credential_bindings
        if isinstance(binding, dict) and binding.get("binding_id") == "OPENAI_API_KEY"
    )
    assert credential["source"] == "unavailable"
    error = credential["error"]
    assert isinstance(error, str)
    assert "credential store" in error.lower()
