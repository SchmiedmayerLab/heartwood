# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Tests for provider route invocation adapters."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

from heartwood.adapters import ModelCallRequest, ModelInvocationRequest
from heartwood.adapters.model import (
    ProviderConfigError,
    ProviderInvocationError,
    ProviderRoute,
    ProviderRouteModelProviderAdapter,
    invoke_provider_route,
    provider_config_from_mapping,
)
from heartwood.schemas import PolicyProfile


class _ProviderHandler(BaseHTTPRequestHandler):
    received_authorization: ClassVar[str | None] = None
    received_model: ClassVar[str | None] = None
    received_prompt: ClassVar[str | None] = None

    def do_POST(self) -> None:
        """Handle one synthetic provider request."""
        body = self.rfile.read(int(self.headers.get("content-length", "0")))
        payload = json.loads(body.decode("utf-8"))
        _ProviderHandler.received_authorization = self.headers.get("authorization")
        _ProviderHandler.received_model = payload["model"]
        _ProviderHandler.received_prompt = payload["messages"][1]["content"]
        response = json.dumps(
            {
                "id": "provider-test",
                "model": payload["model"],
                "object": "chat.completion",
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1},
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, _format: str, *_args: object) -> None:
        """Suppress test HTTP logs."""


class _RedirectProviderHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        """Return a redirect that provider invocation must reject."""
        self.send_response(302)
        self.send_header("location", "/redirected")
        self.end_headers()

    def log_message(self, _format: str, *_args: object) -> None:
        """Suppress test HTTP logs."""


class _InvalidJsonProviderHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        """Return a non-JSON response body."""
        response = b"not json"
        self.send_response(200)
        self.send_header("content-type", "text/plain")
        self.send_header("content-length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, _format: str, *_args: object) -> None:
        """Suppress test HTTP logs."""


def test_provider_route_invokes_openai_compatible_endpoint_with_secret_file(
    tmp_path: Path,
) -> None:
    secret = tmp_path / "provider.key"
    secret.write_text("synthetic-provider-secret\n", encoding="utf-8")
    with _ProviderServer() as endpoint:
        route = ProviderRoute(
            route_id="in-boundary-openai",
            provider="openai",
            endpoint=endpoint,
            model="synthetic-provider-model",
            capability_tier="supervised",
            auth="secret-file",
            secret_file=secret,
        )

        response = invoke_provider_route(route, prompt_length=37)

    assert isinstance(response, dict)
    assert response["model"] == "synthetic-provider-model"
    assert _ProviderHandler.received_authorization == "Bearer synthetic-provider-secret"
    assert _ProviderHandler.received_model == "synthetic-provider-model"
    assert "37" in str(_ProviderHandler.received_prompt)
    assert "synthetic-provider-secret" not in json.dumps(response)


def test_provider_route_adapter_requires_policy_allow_before_invocation(
    tmp_path: Path,
) -> None:
    secret = tmp_path / "provider.key"
    secret.write_text("synthetic-provider-secret\n", encoding="utf-8")
    with _ProviderServer() as endpoint:
        route = ProviderRoute(
            route_id="local-openai-compatible",
            provider="openai-compatible",
            endpoint=endpoint,
            model="synthetic-provider-model",
            capability_tier="supervised",
            auth="secret-file",
            secret_file=secret,
        )
        adapter = ProviderRouteModelProviderAdapter(
            policy_profile=PolicyProfile(
                policy_id="test-policy",
                platform_id="generic",
                allowed_model_endpoints=(endpoint,),
            ),
            route=route,
        )

        decision = adapter.evaluate_model_call(
            ModelCallRequest(
                endpoint=endpoint,
                capability_tier="supervised",
                purpose="provider route local-openai-compatible",
            )
        )
        response = adapter.invoke_model_call(
            ModelInvocationRequest(
                endpoint=endpoint,
                model="synthetic-provider-model",
                prompt_length=12,
                purpose="provider route local-openai-compatible",
            )
        )

    assert decision.decision == "allow"
    assert isinstance(response, dict)
    assert response["id"] == "provider-test"


def test_provider_route_adapter_rejects_mismatched_invocation_request() -> None:
    route = ProviderRoute(
        route_id="local-openai-compatible",
        provider="openai-compatible",
        endpoint="http://127.0.0.1:1/v1/chat/completions",
        model="synthetic-provider-model",
        capability_tier="supervised",
        auth="none",
    )
    adapter = ProviderRouteModelProviderAdapter(
        policy_profile=PolicyProfile(
            policy_id="test-policy",
            platform_id="generic",
            allowed_model_endpoints=(route.endpoint,),
        ),
        route=route,
    )

    assert adapter.provider_id == "provider-route:local-openai-compatible"
    assert adapter.capability_tier == "supervised"
    with pytest.raises(ProviderInvocationError, match="endpoint"):
        adapter.invoke_model_call(
            ModelInvocationRequest(
                endpoint="http://127.0.0.1:2/v1/chat/completions",
                model=route.model,
                prompt_length=12,
                purpose="provider route local-openai-compatible",
            )
        )
    with pytest.raises(ProviderInvocationError, match="model"):
        adapter.invoke_model_call(
            ModelInvocationRequest(
                endpoint=route.endpoint,
                model="different-model",
                prompt_length=12,
                purpose="provider route local-openai-compatible",
            )
        )


def test_provider_route_invocation_rejects_managed_identity_without_platform_adapter() -> None:
    route = ProviderRoute(
        route_id="vertex",
        provider="vertex-ai",
        endpoint="https://vertex.example.invalid/v1/chat/completions",
        model="configured-by-platform",
        capability_tier="supervised",
        auth="managed-identity",
    )

    with pytest.raises(ProviderInvocationError, match="managed-identity"):
        invoke_provider_route(route, prompt_length=1)


def test_provider_route_invocation_validates_secret_files(tmp_path: Path) -> None:
    route = ProviderRoute(
        route_id="openai",
        provider="openai",
        endpoint="https://api.openai.example.invalid/v1/chat/completions",
        model="synthetic-provider-model",
        capability_tier="supervised",
        auth="secret-file",
    )
    with pytest.raises(ProviderInvocationError, match="not configured"):
        invoke_provider_route(route, prompt_length=1)

    missing_route = ProviderRoute(
        route_id="openai",
        provider="openai",
        endpoint=route.endpoint,
        model=route.model,
        capability_tier=route.capability_tier,
        auth="secret-file",
        secret_file=tmp_path / "missing.key",
    )
    with pytest.raises(ProviderInvocationError, match="unavailable"):
        invoke_provider_route(missing_route, prompt_length=1)

    empty = tmp_path / "empty.key"
    empty.write_text("\n", encoding="utf-8")
    empty_route = ProviderRoute(
        route_id="openai",
        provider="openai",
        endpoint=route.endpoint,
        model=route.model,
        capability_tier=route.capability_tier,
        auth="secret-file",
        secret_file=empty,
    )
    with pytest.raises(ProviderInvocationError, match="empty"):
        invoke_provider_route(empty_route, prompt_length=1)


def test_provider_route_invocation_rejects_redirects() -> None:
    with _ProviderServer(_RedirectProviderHandler) as endpoint:
        route = ProviderRoute(
            route_id="local-openai-compatible",
            provider="openai-compatible",
            endpoint=endpoint,
            model="synthetic-provider-model",
            capability_tier="supervised",
            auth="none",
        )

        with pytest.raises(ProviderInvocationError, match="redirect"):
            invoke_provider_route(route, prompt_length=1)


def test_provider_route_invocation_rejects_invalid_json() -> None:
    with _ProviderServer(_InvalidJsonProviderHandler) as endpoint:
        route = ProviderRoute(
            route_id="local-openai-compatible",
            provider="openai-compatible",
            endpoint=endpoint,
            model="synthetic-provider-model",
            capability_tier="supervised",
            auth="none",
        )

        with pytest.raises(ProviderInvocationError, match="JSONDecodeError"):
            invoke_provider_route(route, prompt_length=1)


def test_provider_config_rejects_nested_inline_secrets() -> None:
    with pytest.raises(ProviderConfigError, match="inline secret"):
        provider_config_from_mapping(
            {
                "schema_version": "heartwood.provider-config.v1",
                "routes": [
                    {
                        "route_id": "local",
                        "provider": "openai-compatible",
                        "endpoint": "http://127.0.0.1:8765/v1/chat/completions",
                        "model": "synthetic-provider-model",
                        "capability_tier": "supervised",
                        "auth": "none",
                        "metadata": {"token": "not-allowed"},
                    }
                ],
            }
        )


def test_provider_config_rejects_secret_file_shape_for_managed_identity() -> None:
    with pytest.raises(ProviderConfigError, match="secret_file"):
        provider_config_from_mapping(
            {
                "schema_version": "heartwood.provider-config.v1",
                "routes": [
                    {
                        "route_id": "vertex",
                        "provider": "vertex-ai",
                        "endpoint": "https://vertex.example.invalid/v1/chat/completions",
                        "model": "configured-by-platform",
                        "capability_tier": "supervised",
                        "auth": "managed-identity",
                        "secret_file": 7,
                    }
                ],
            }
        )


class _ProviderServer:
    def __init__(
        self,
        handler: type[BaseHTTPRequestHandler] = _ProviderHandler,
    ) -> None:
        self.handler = handler

    def __enter__(self) -> str:
        _ProviderHandler.received_authorization = None
        _ProviderHandler.received_model = None
        _ProviderHandler.received_prompt = None
        self.server = HTTPServer(("127.0.0.1", 0), self.handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        address = self.server.server_address
        host_value = address[0]
        host = host_value.decode("ascii") if isinstance(host_value, bytes) else str(host_value)
        port = int(address[1])
        return f"http://{host}:{port}/v1/chat/completions"

    def __exit__(self, *_exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
