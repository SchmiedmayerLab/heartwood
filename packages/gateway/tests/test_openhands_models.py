# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from heartwood.gateway._openhands_models import (
    OpenHandsModelError,
    model_uses_responses_api,
    openai_subscription_models,
    prepare_openhands_import,
    request_endpoint_for_model,
)


def test_openhands_capabilities_select_the_request_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    features = {
        "openai/gpt-responses": SimpleNamespace(supports_responses_api=True),
        "openai/gpt-chat": SimpleNamespace(supports_responses_api=False),
    }
    monkeypatch.setattr(
        "heartwood.gateway._openhands_models.import_module",
        lambda _name: SimpleNamespace(get_features=features.__getitem__),
    )

    assert model_uses_responses_api("openai/gpt-responses")
    assert not model_uses_responses_api("openai/gpt-chat")
    assert (
        request_endpoint_for_model(
            "openai/gpt-responses",
            "https://api.example/v1/chat/completions",
        )
        == "https://api.example/v1/responses"
    )
    assert (
        request_endpoint_for_model(
            "openai/gpt-chat",
            "https://api.example/v1/chat/completions",
        )
        == "https://api.example/v1/chat/completions"
    )
    assert (
        request_endpoint_for_model(
            "openai/gpt-chat",
            "https://api.example/v1/responses",
        )
        == "https://api.example/v1/chat/completions"
    )
    assert (
        request_endpoint_for_model(
            "openai/gpt-responses",
            "https://api.example/v1/responses",
        )
        == "https://api.example/v1/responses"
    )
    assert (
        request_endpoint_for_model(
            "openai/gpt-responses",
            "https://api.example/v1/messages",
        )
        == "https://api.example/v1/messages"
    )


def test_openhands_capability_failures_are_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(_name: str) -> object:
        raise ImportError("sensitive provider detail")

    monkeypatch.setattr("heartwood.gateway._openhands_models.import_module", unavailable)

    with pytest.raises(OpenHandsModelError, match="capabilities are unavailable") as captured:
        model_uses_responses_api("openai/model")

    assert "sensitive" not in str(captured.value)


def test_openhands_subscription_registry_is_the_model_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = SimpleNamespace(
        available_models=(
            SimpleNamespace(id="gpt-current"),
            SimpleNamespace(id="gpt-compact"),
        )
    )
    monkeypatch.setattr(
        "heartwood.gateway._openhands_models.import_module",
        lambda _name: SimpleNamespace(
            get_acp_provider=lambda provider_id: provider if provider_id == "codex" else None
        ),
    )

    assert openai_subscription_models() == ("gpt-current", "gpt-compact")


@pytest.mark.parametrize("failure", ["missing-provider", "import-error"])
def test_openhands_subscription_registry_fails_closed(
    failure: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if failure == "missing-provider":
        module = SimpleNamespace(get_acp_provider=lambda _provider_id: None)
        monkeypatch.setattr(
            "heartwood.gateway._openhands_models.import_module",
            lambda _name: module,
        )
    else:

        def unavailable(_name: str) -> object:
            raise ImportError("internal module path")

        monkeypatch.setattr("heartwood.gateway._openhands_models.import_module", unavailable)

    with pytest.raises(OpenHandsModelError, match="ChatGPT model registry") as captured:
        openai_subscription_models()

    assert "internal module path" not in str(captured.value)


def test_openhands_import_defaults_do_not_replace_operator_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "operator-value")
    monkeypatch.delenv("OPENHANDS_SUPPRESS_BANNER", raising=False)

    prepare_openhands_import()

    assert os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] == "operator-value"
    assert os.environ["OPENHANDS_SUPPRESS_BANNER"] == "1"
