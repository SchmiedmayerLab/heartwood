# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""OpenHands-owned subscription authentication behind a small gateway contract."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Coroutine, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from importlib import import_module
from typing import Any, Literal, Protocol, cast

from heartwood.gateway._openhands_models import (
    OpenHandsModelError,
    openai_subscription_models,
    prepare_openhands_import,
)

OPENAI_SUBSCRIPTION_CONNECTION_ID = "openai-subscription"
OPENAI_SUBSCRIPTION_VENDOR = "openai"
OPENAI_SUBSCRIPTION_ENDPOINT = "https://chatgpt.com/backend-api/codex/responses"
OPENAI_SUBSCRIPTION_CREDENTIAL_REFERENCE = "subscription:openai"

_DEVICE_LOGIN_LIFETIME_SECONDS = 15 * 60


class SubscriptionError(RuntimeError):
    """Raised when OpenHands subscription authentication cannot continue."""


@dataclass(frozen=True, slots=True)
class SubscriptionDeviceLogin:
    """Public, non-secret state for one device-code login."""

    login_id: str
    connection_id: str
    verification_url: str
    user_code: str
    poll_interval_seconds: int
    status: Literal["pending", "complete"]

    def safe_dict(self) -> dict[str, object]:
        """Return the browser-safe device login projection."""
        return {
            "schema_version": "heartwood.subscription-login.v1",
            "login_id": self.login_id,
            "connection_id": self.connection_id,
            "verification_url": self.verification_url,
            "user_code": self.user_code,
            "poll_interval_seconds": self.poll_interval_seconds,
            "status": self.status,
        }


class SubscriptionProvider(Protocol):
    """Authentication and catalog operations supplied by an upstream SDK."""

    connection_id: str
    vendor: str

    def models(self) -> Sequence[str]:
        """Return models from the upstream subscription registry."""

    def credential_available(self) -> bool:
        """Return whether the upstream credential cache contains an account."""

    def login(
        self,
        *,
        model: str,
        force_login: bool,
        open_browser: bool,
        auth_method: Literal["browser", "device_code"],
    ) -> None:
        """Run the upstream interactive login flow."""

    def start_device_login(self) -> SubscriptionDeviceLogin:
        """Start a browser-independent device-code flow."""

    def poll_device_login(self, login_id: str) -> SubscriptionDeviceLogin:
        """Poll one device-code flow without exposing its provider handle."""

    def logout(self) -> bool:
        """Remove the credential from the upstream credential cache."""


class SubscriptionLlm(Protocol):
    """Minimal LLM behavior consumed by the OpenHands adapter."""

    def model_copy(self, *, update: dict[str, object]) -> SubscriptionLlm:
        """Copy the model for the condenser."""

    def reset_metrics(self) -> None:
        """Reset model usage metrics."""


class _SubscriptionLlmFactory(Protocol):
    def subscription_login(self, **options: object) -> object:
        """Create an authenticated subscription LLM."""


class _OpenHandsSdkModule(Protocol):
    LLM: _SubscriptionLlmFactory


class _OpenHandsAuth(Protocol):
    def get_credentials(self) -> object | None:
        """Return cached credentials."""

    def start_device_login(self) -> Coroutine[Any, Any, object]:
        """Start device login."""

    def poll_device_login(
        self,
        provider_value: object,
        *,
        persist: bool,
    ) -> Coroutine[Any, Any, object | None]:
        """Poll device login."""

    def refresh_if_needed_sync(self) -> object | None:
        """Refresh and return cached credentials."""

    def create_llm(
        self,
        *,
        model: str,
        credentials: object,
        **options: object,
    ) -> SubscriptionLlm:
        """Create a subscription LLM."""

    def logout(self) -> bool:
        """Remove cached credentials."""


@dataclass(slots=True)
class _PendingDeviceLogin:
    provider_value: object
    public: SubscriptionDeviceLogin
    created_at: float


class OpenHandsOpenAISubscription:
    """Delegate ChatGPT subscription authentication entirely to OpenHands."""

    connection_id = OPENAI_SUBSCRIPTION_CONNECTION_ID
    vendor = OPENAI_SUBSCRIPTION_VENDOR

    def __init__(self) -> None:
        self._pending: dict[str, _PendingDeviceLogin] = {}
        self._auth_client: _OpenHandsAuth | None = None

    def models(self) -> Sequence[str]:
        """Return the ordered model picker owned by OpenHands."""
        prepare_openhands_import()
        try:
            return openai_subscription_models()
        except OpenHandsModelError as error:
            raise SubscriptionError(str(error)) from error

    def credential_available(self) -> bool:
        """Return whether OpenHands has a cached ChatGPT account."""
        try:
            return self._auth().get_credentials() is not None
        except Exception as error:
            raise SubscriptionError(_safe_subscription_error(error)) from error

    def login(
        self,
        *,
        model: str,
        force_login: bool = False,
        open_browser: bool = True,
        auth_method: Literal["browser", "device_code"] = "browser",
    ) -> None:
        """Use OpenHands' complete consent, OAuth, cache, and refresh flow."""
        try:
            sdk = _openhands_sdk_module()
            sdk.LLM.subscription_login(
                vendor=self.vendor,
                model=model,
                force_login=force_login,
                open_browser=open_browser,
                auth_method=auth_method,
            )
        except Exception as error:
            raise SubscriptionError(_safe_subscription_error(error)) from error

    def start_device_login(self) -> SubscriptionDeviceLogin:
        """Start OpenHands' device-code flow and retain only its opaque handle."""
        try:
            self._prune_expired_pending()
            provider_value = _run_async(self._auth().start_device_login())
            verification_url = getattr(provider_value, "verification_url", None)
            user_code = getattr(provider_value, "user_code", None)
            poll_interval = getattr(provider_value, "interval", None)
            if (
                not isinstance(verification_url, str)
                or not isinstance(user_code, str)
                or not isinstance(poll_interval, int)
                or poll_interval < 1
            ):
                raise SubscriptionError("OpenHands returned an invalid device login")
            login_id = uuid.uuid4().hex
            public = SubscriptionDeviceLogin(
                login_id=login_id,
                connection_id=self.connection_id,
                verification_url=verification_url,
                user_code=user_code,
                poll_interval_seconds=poll_interval,
                status="pending",
            )
            self._pending[login_id] = _PendingDeviceLogin(
                provider_value=provider_value,
                public=public,
                created_at=time.monotonic(),
            )
            return public
        except SubscriptionError:
            raise
        except Exception as error:
            raise SubscriptionError(_safe_subscription_error(error)) from error

    def poll_device_login(self, login_id: str) -> SubscriptionDeviceLogin:
        """Poll once through OpenHands and persist credentials only after success."""
        pending = self._pending.get(login_id)
        if pending is None:
            raise SubscriptionError("the ChatGPT sign-in request is unavailable or expired")
        if time.monotonic() - pending.created_at > _DEVICE_LOGIN_LIFETIME_SECONDS:
            self._pending.pop(login_id, None)
            raise SubscriptionError("the ChatGPT sign-in request expired; start again")
        try:
            credentials = _run_async(
                self._auth().poll_device_login(
                    pending.provider_value,
                    persist=True,
                )
            )
        except Exception as error:
            self._pending.pop(login_id, None)
            raise SubscriptionError(_safe_subscription_error(error)) from error
        if credentials is None:
            return pending.public
        self._pending.pop(login_id, None)
        return SubscriptionDeviceLogin(
            login_id=pending.public.login_id,
            connection_id=pending.public.connection_id,
            verification_url=pending.public.verification_url,
            user_code=pending.public.user_code,
            poll_interval_seconds=pending.public.poll_interval_seconds,
            status="complete",
        )

    def logout(self) -> bool:
        """Let OpenHands remove its cached ChatGPT account."""
        try:
            self._pending.clear()
            return bool(self._auth().logout())
        except Exception as error:
            raise SubscriptionError(_safe_subscription_error(error)) from error

    def _auth(self) -> _OpenHandsAuth:
        if self._auth_client is None:
            self._auth_client = _openhands_auth()
        return self._auth_client

    def _prune_expired_pending(self) -> None:
        now = time.monotonic()
        expired = (
            login_id
            for login_id, pending in self._pending.items()
            if now - pending.created_at > _DEVICE_LOGIN_LIFETIME_SECONDS
        )
        for login_id in tuple(expired):
            self._pending.pop(login_id, None)


def create_openai_subscription_llm(
    *,
    model: str,
    options: dict[str, Any],
) -> SubscriptionLlm:
    """Restore a subscription LLM with OpenHands' credential refresh and transport."""
    try:
        auth = _openhands_auth()
        credentials = auth.refresh_if_needed_sync()
        if credentials is None:
            raise SubscriptionError("Sign in with ChatGPT before starting this model")
        provider_model = model.removeprefix("openai/")
        return auth.create_llm(
            model=provider_model,
            credentials=credentials,
            **options,
        )
    except SubscriptionError:
        raise
    except Exception as error:
        raise SubscriptionError(_safe_subscription_error(error)) from error


def _openhands_sdk_module() -> _OpenHandsSdkModule:
    prepare_openhands_import()
    return cast(_OpenHandsSdkModule, import_module("openhands.sdk"))


def _openhands_auth() -> _OpenHandsAuth:
    prepare_openhands_import()
    module = import_module("openhands.sdk.llm.auth")
    return cast(_OpenHandsAuth, module.OpenAISubscriptionAuth())


def _run_async[Result](operation: Coroutine[Any, Any, Result]) -> Result:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(operation)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="heartwood-subscription") as executor:
        future = executor.submit(asyncio.run, operation)
        return future.result()


def _safe_subscription_error(error: Exception) -> str:
    message = str(error).lower()
    if "declined" in message:
        return "ChatGPT sign-in was cancelled"
    if "timeout" in message or "timed out" in message:
        return "ChatGPT sign-in timed out; start again"
    if "not supported" in message and "model" in message:
        return "OpenHands does not support that model for ChatGPT sign-in"
    if "credential" in message or "token" in message or "auth" in message:
        return "ChatGPT sign-in failed; sign in again"
    return "OpenHands could not complete ChatGPT sign-in"
