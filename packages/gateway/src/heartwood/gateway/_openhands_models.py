# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Small projections over OpenHands-owned model capabilities and registries."""

from __future__ import annotations

import os
from collections.abc import Sequence
from importlib import import_module
from typing import Protocol, cast


class OpenHandsModelError(RuntimeError):
    """Raised when the pinned OpenHands model registry is unavailable."""


class _ModelFeatures(Protocol):
    supports_responses_api: bool


class _ModelFeatureModule(Protocol):
    def get_features(self, model: str) -> _ModelFeatures:
        """Return OpenHands' capabilities for one model."""


class _ProviderModel(Protocol):
    id: str


class _AcpProvider(Protocol):
    available_models: Sequence[_ProviderModel]


class _AcpProviderModule(Protocol):
    def get_acp_provider(
        self,
        provider_id: str,
    ) -> _AcpProvider | None:
        """Return one OpenHands ACP provider."""


def model_uses_responses_api(model: str) -> bool:
    """Return the request path selected by the pinned OpenHands SDK."""
    prepare_openhands_import()
    try:
        module = cast(
            _ModelFeatureModule,
            import_module("openhands.sdk.llm.utils.model_features"),
        )
        return bool(module.get_features(model).supports_responses_api)
    except Exception as error:
        raise OpenHandsModelError("OpenHands model capabilities are unavailable") from error


def request_endpoint_for_model(model: str, default_endpoint: str) -> str:
    """Align policy with OpenHands' Chat Completions or Responses request path."""
    chat_suffix = "/chat/completions"
    responses_suffix = "/responses"
    if default_endpoint.endswith(chat_suffix):
        return (
            f"{default_endpoint.removesuffix(chat_suffix)}{responses_suffix}"
            if model_uses_responses_api(model)
            else default_endpoint
        )
    if default_endpoint.endswith(responses_suffix) and not model_uses_responses_api(model):
        return f"{default_endpoint.removesuffix(responses_suffix)}{chat_suffix}"
    return default_endpoint


def openai_subscription_models() -> tuple[str, ...]:
    """Return the ordered Codex model registry supplied by OpenHands."""
    prepare_openhands_import()
    try:
        module = cast(
            _AcpProviderModule,
            import_module("openhands.sdk.settings.acp_providers"),
        )
        provider = module.get_acp_provider("codex")
        if provider is None:
            raise OpenHandsModelError("OpenHands does not provide the ChatGPT model registry")
        return tuple(str(model.id) for model in provider.available_models)
    except OpenHandsModelError:
        raise
    except Exception as error:
        raise OpenHandsModelError(
            "OpenHands does not provide the ChatGPT model registry"
        ) from error


def prepare_openhands_import() -> None:
    """Apply the shared offline-safe OpenHands import defaults."""
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    os.environ.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")
