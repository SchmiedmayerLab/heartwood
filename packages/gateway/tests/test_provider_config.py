# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Tests for provider route configuration."""

from __future__ import annotations

from pathlib import Path

import pytest

from heartwood.gateway import (
    ProviderConfigError,
    load_provider_config,
    provider_config_from_mapping,
)


def _valid_route(**overrides: object) -> dict[str, object]:
    route: dict[str, object] = {
        "route_id": "local-loopback",
        "provider": "openai-compatible",
        "endpoint": "http://127.0.0.1:8765/v1/chat/completions",
        "model": "heartwood-local-runtime",
        "capability_tier": "supervised",
        "auth": "none",
    }
    route.update(overrides)
    return route


def _valid_config(**overrides: object) -> dict[str, object]:
    config: dict[str, object] = {
        "schema_version": "heartwood.provider-config.v1",
        "routes": [_valid_route()],
    }
    config.update(overrides)
    return config


def test_provider_config_loads_example_without_inline_secrets() -> None:
    config = load_provider_config(
        _repo_root() / "images" / "generic" / "providers" / "provider-routes.example.toml"
    )

    local = config.route("local-loopback")
    openai = config.route("openai")

    assert config.route().route_id == "local-loopback"
    assert local.provider == "openai-compatible"
    assert local.auth == "none"
    assert local.endpoint == "http://127.0.0.1:8765/v1/chat/completions"
    assert openai.auth == "secret-file"
    assert openai.secret_file == Path("/run/secrets/openai_api_key")
    assert Path("/run/secrets/openai_api_key") in config.secret_paths()
    assert "secret_file" not in openai.safe_metadata()


def test_provider_config_wraps_missing_file_errors(tmp_path: Path) -> None:
    missing = tmp_path / "missing.toml"

    with pytest.raises(ProviderConfigError, match="unable to read provider config"):
        load_provider_config(missing)


def test_provider_config_wraps_malformed_toml_errors(tmp_path: Path) -> None:
    malformed = tmp_path / "provider-routes.toml"
    malformed.write_text("not = [valid\n", encoding="utf-8")

    with pytest.raises(ProviderConfigError, match="invalid TOML in provider config"):
        load_provider_config(malformed)


def test_provider_config_rejects_inline_secret_values() -> None:
    with pytest.raises(ProviderConfigError, match="inline secret field"):
        provider_config_from_mapping(
            _valid_config(routes=[_valid_route(auth="secret-file", api_key="not-allowed")])
        )


def test_provider_config_requires_secret_file_for_secret_auth() -> None:
    with pytest.raises(ProviderConfigError, match="requires secret_file"):
        provider_config_from_mapping(
            _valid_config(
                routes=[
                    _valid_route(
                        route_id="openai",
                        provider="openai",
                        endpoint="https://api.openai.com/v1/chat/completions",
                        model="configured-by-platform",
                        auth="secret-file",
                    )
                ]
            )
        )


def test_provider_config_requires_auth_for_external_providers() -> None:
    with pytest.raises(ProviderConfigError, match="requires secret-file or managed-identity"):
        provider_config_from_mapping(
            _valid_config(
                routes=[
                    _valid_route(
                        route_id="openai",
                        provider="openai",
                        endpoint="https://api.openai.com/v1/chat/completions",
                        model="configured-by-platform",
                        auth="none",
                    )
                ]
            )
        )


def test_provider_config_rejects_unknown_route() -> None:
    config = provider_config_from_mapping(_valid_config())

    with pytest.raises(ProviderConfigError, match="unknown provider route"):
        config.route("missing")


def test_provider_config_requires_route_when_no_default_is_configured() -> None:
    config = provider_config_from_mapping(_valid_config())

    with pytest.raises(ProviderConfigError, match="route id is required"):
        config.route()


@pytest.mark.parametrize(
    ("config", "message"),
    [
        (_valid_config(schema_version="heartwood.provider-config.v0"), "unsupported"),
        (_valid_config(routes=[]), "at least one route"),
        (_valid_config(default_route="missing"), "unknown provider route"),
        (_valid_config(default_route=1), "default_route must be"),
        (_valid_config(routes=["not-a-table"]), "route must be a table"),
        (_valid_config(routes=[_valid_route(), _valid_route()]), "route ids must be unique"),
        (_valid_config(routes=[_valid_route(provider="unknown")]), "unsupported provider"),
        (
            _valid_config(routes=[_valid_route(capability_tier="unsafe")]),
            "unsupported capability tier",
        ),
        (_valid_config(routes=[_valid_route(auth="inline")]), "unsupported provider auth mode"),
        (_valid_config(routes=[_valid_route(endpoint="not-a-url")]), "invalid provider endpoint"),
        (
            _valid_config(routes=[_valid_route(auth="secret-file", secret_file="relative-key")]),
            "absolute runtime mount path",
        ),
        (
            _valid_config(routes=[_valid_route(auth="none", secret_file="/run/secrets/key")]),
            "only allowed with secret-file auth",
        ),
        (_valid_config(routes=[_valid_route(route_id="")]), "route_id must be"),
        (_valid_config(routes=[_valid_route(notes=1)]), "notes must be"),
    ],
)
def test_provider_config_rejects_malformed_inputs(
    config: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ProviderConfigError, match=message):
        provider_config_from_mapping(config)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]
