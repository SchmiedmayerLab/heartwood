# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from types import SimpleNamespace

import pytest

from heartwood.gateway._openhands_models import OpenHandsModelError
from heartwood.gateway._subscriptions import (
    OpenHandsOpenAISubscription,
    SubscriptionError,
    _run_async,
    _safe_subscription_error,
    create_openai_subscription_llm,
)


class _Auth:
    def __init__(self) -> None:
        self.credentials: object | None = None
        self.polls = 0
        self.created: tuple[str, object, dict[str, object]] | None = None

    def get_credentials(self) -> object | None:
        return self.credentials

    async def start_device_login(self) -> object:
        return SimpleNamespace(
            verification_url="https://auth.example/device",
            user_code="TEST-CODE",
            interval=3,
        )

    async def poll_device_login(
        self,
        _provider_value: object,
        *,
        persist: bool,
    ) -> object | None:
        assert persist
        self.polls += 1
        if self.polls == 1:
            return None
        self.credentials = object()
        return self.credentials

    def refresh_if_needed_sync(self) -> object | None:
        return self.credentials

    def create_llm(
        self,
        *,
        model: str,
        credentials: object,
        **options: object,
    ) -> object:
        self.created = (model, credentials, options)
        return SimpleNamespace()

    def logout(self) -> bool:
        existed = self.credentials is not None
        self.credentials = None
        return existed


def test_device_login_delegates_state_and_credentials_to_openhands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth = _Auth()
    provider = OpenHandsOpenAISubscription()
    monkeypatch.setattr(provider, "_auth_client", auth)
    monkeypatch.setattr(
        "heartwood.gateway._subscriptions.openai_subscription_models",
        lambda: ("gpt-current", "gpt-compact"),
    )

    assert provider.models() == ("gpt-current", "gpt-compact")
    assert not provider.credential_available()
    started = provider.start_device_login()
    assert started.safe_dict() == {
        "schema_version": "heartwood.subscription-login.v1",
        "login_id": started.login_id,
        "connection_id": "openai-subscription",
        "verification_url": "https://auth.example/device",
        "user_code": "TEST-CODE",
        "poll_interval_seconds": 3,
        "status": "pending",
    }
    assert provider.poll_device_login(started.login_id).status == "pending"
    assert provider.poll_device_login(started.login_id).status == "complete"
    assert provider.credential_available()
    assert provider.logout()
    assert not provider.credential_available()


def test_interactive_login_uses_openhands_subscription_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    provider = OpenHandsOpenAISubscription()
    monkeypatch.setattr(
        "heartwood.gateway._subscriptions._openhands_sdk_module",
        lambda: SimpleNamespace(
            LLM=SimpleNamespace(
                subscription_login=lambda **options: calls.append(options),
            )
        ),
    )

    provider.login(
        model="gpt-current",
        force_login=True,
        open_browser=False,
        auth_method="device_code",
    )

    assert calls == [
        {
            "vendor": "openai",
            "model": "gpt-current",
            "force_login": True,
            "open_browser": False,
            "auth_method": "device_code",
        }
    ]


def test_runtime_llm_uses_openhands_refresh_and_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth = _Auth()
    auth.credentials = object()
    monkeypatch.setattr(
        "heartwood.gateway._subscriptions._openhands_auth",
        lambda: auth,
    )

    llm = create_openai_subscription_llm(
        model="openai/gpt-current",
        options={"timeout": 30},
    )

    assert llm is not None
    assert auth.created == (
        "gpt-current",
        auth.credentials,
        {"timeout": 30},
    )


def test_runtime_llm_requires_existing_openhands_sign_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "heartwood.gateway._subscriptions._openhands_auth",
        _Auth,
    )

    with pytest.raises(SubscriptionError, match="Sign in with ChatGPT"):
        create_openai_subscription_llm(
            model="openai/gpt-current",
            options={},
        )


def test_model_registry_failure_is_exposed_as_a_subscription_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable() -> tuple[str, ...]:
        raise OpenHandsModelError("OpenHands registry unavailable")

    monkeypatch.setattr(
        "heartwood.gateway._subscriptions.openai_subscription_models",
        unavailable,
    )

    with pytest.raises(SubscriptionError, match="registry unavailable"):
        OpenHandsOpenAISubscription().models()


@pytest.mark.parametrize(
    "provider_value",
    [
        SimpleNamespace(verification_url=None, user_code="CODE", interval=3),
        SimpleNamespace(verification_url="https://auth.example", user_code=None, interval=3),
        SimpleNamespace(
            verification_url="https://auth.example",
            user_code="CODE",
            interval=0,
        ),
    ],
)
def test_device_login_rejects_invalid_openhands_state(
    provider_value: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth = _Auth()

    async def invalid_login() -> object:
        return provider_value

    monkeypatch.setattr(auth, "start_device_login", invalid_login)
    provider = OpenHandsOpenAISubscription()
    monkeypatch.setattr(provider, "_auth_client", auth)

    with pytest.raises(SubscriptionError, match="invalid device login"):
        provider.start_device_login()


def test_device_login_rejects_unknown_and_expired_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth = _Auth()
    provider = OpenHandsOpenAISubscription()
    monkeypatch.setattr(provider, "_auth_client", auth)

    with pytest.raises(SubscriptionError, match="unavailable or expired"):
        provider.poll_device_login("unknown")

    started = provider.start_device_login()
    expired_at = time.monotonic() + 16 * 60
    monkeypatch.setattr(
        "heartwood.gateway._subscriptions.time.monotonic",
        lambda: expired_at,
    )
    with pytest.raises(SubscriptionError, match="expired; start again"):
        provider.poll_device_login(started.login_id)
    with pytest.raises(SubscriptionError, match="unavailable or expired"):
        provider.poll_device_login(started.login_id)


def test_device_poll_failure_is_redacted_and_clears_pending_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth = _Auth()

    async def failed_poll(
        _provider_value: object,
        *,
        persist: bool,
    ) -> object | None:
        assert persist
        raise RuntimeError("authorization token leaked detail")

    monkeypatch.setattr(auth, "poll_device_login", failed_poll)
    provider = OpenHandsOpenAISubscription()
    monkeypatch.setattr(provider, "_auth_client", auth)
    started = provider.start_device_login()

    with pytest.raises(SubscriptionError, match="sign-in failed") as captured:
        provider.poll_device_login(started.login_id)
    assert "leaked" not in str(captured.value)
    with pytest.raises(SubscriptionError, match="unavailable or expired"):
        provider.poll_device_login(started.login_id)


@pytest.mark.parametrize(
    ("operation", "message"),
    [
        ("credentials", "sign-in failed"),
        ("login", "cancelled"),
        ("logout", "timed out"),
    ],
)
def test_subscription_provider_redacts_upstream_authentication_errors(
    operation: str,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenHandsOpenAISubscription()
    auth = _Auth()
    monkeypatch.setattr(provider, "_auth_client", auth)
    invoke: Callable[[], object]

    if operation == "credentials":
        monkeypatch.setattr(
            auth,
            "get_credentials",
            lambda: (_ for _ in ()).throw(RuntimeError("secret token detail")),
        )
        invoke = provider.credential_available
    elif operation == "login":
        monkeypatch.setattr(
            "heartwood.gateway._subscriptions._openhands_sdk_module",
            lambda: SimpleNamespace(
                LLM=SimpleNamespace(
                    subscription_login=lambda **_options: (_ for _ in ()).throw(
                        RuntimeError("access declined by user")
                    )
                )
            ),
        )

        def invoke() -> None:
            provider.login(model="gpt-current")

    else:
        monkeypatch.setattr(
            auth,
            "logout",
            lambda: (_ for _ in ()).throw(RuntimeError("request timed out")),
        )
        invoke = provider.logout

    with pytest.raises(SubscriptionError, match=message):
        invoke()


def test_runtime_llm_redacts_openhands_restore_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth = _Auth()
    auth.credentials = object()
    monkeypatch.setattr(
        auth,
        "create_llm",
        lambda **_options: (_ for _ in ()).throw(RuntimeError("credential payload")),
    )
    monkeypatch.setattr(
        "heartwood.gateway._subscriptions._openhands_auth",
        lambda: auth,
    )

    with pytest.raises(SubscriptionError, match="sign-in failed") as captured:
        create_openai_subscription_llm(model="openai/gpt-current", options={})

    assert "payload" not in str(captured.value)


def test_async_bridge_works_inside_an_existing_event_loop() -> None:
    async def operation() -> str:
        return "complete"

    async def invoke() -> str:
        return _run_async(operation())

    assert asyncio.run(invoke()) == "complete"


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (RuntimeError("user declined"), "cancelled"),
        (RuntimeError("request timeout"), "timed out"),
        (RuntimeError("model is not supported"), "does not support"),
        (RuntimeError("invalid auth token"), "sign-in failed"),
        (RuntimeError("unclassified failure"), "could not complete"),
    ],
)
def test_safe_subscription_errors_are_stable(error: Exception, message: str) -> None:
    assert message in _safe_subscription_error(error)
