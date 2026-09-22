# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Tests for the gateway launch capability."""

from __future__ import annotations

import pytest

from heartwood.gateway import IngressConfigurationError, IngressPolicy, LaunchCapability

_SECRET = "operator" * 5


def test_generated_capability_issues_one_launch_link() -> None:
    capability = LaunchCapability.generate()
    link = capability.launch_url(IngressPolicy.create())

    assert link is not None
    assert link.startswith("http://127.0.0.1:8767/launch?token=")
    token = link.rsplit("=", 1)[1]
    assert not capability.consume_launch_token("wrong")
    assert not capability.consume_launch_token("\u00e9")
    assert capability.consume_launch_token(token)
    assert not capability.consume_launch_token(token)
    assert capability.launch_url(IngressPolicy.create()) is None


def test_environment_capability_is_consumed_and_prints_no_link() -> None:
    env = {"HEARTWOOD_GATEWAY_CAPABILITY": _SECRET, "PATH": "/usr/bin"}

    capability = LaunchCapability.from_environment(env)

    assert capability is not None
    assert env == {"PATH": "/usr/bin"}
    assert capability.launch_url(IngressPolicy.create()) is None
    assert capability.authorizes({"x-heartwood-capability": (f" {_SECRET} ",)})
    assert LaunchCapability.from_environment({}) is None


@pytest.mark.parametrize(
    "secret",
    ["short", "has space " + "x" * 30, "a;b" + "x" * 30, '"' + "x" * 32, "\u00e9" * 32],
)
def test_capability_rejects_unusable_secrets(secret: str) -> None:
    with pytest.raises(IngressConfigurationError):
        LaunchCapability(secret, launch_token=None)


def test_capability_reads_the_cookie_and_ignores_malformed_headers() -> None:
    capability = LaunchCapability(_SECRET, launch_token=None)

    assert capability.authorizes(
        {"cookie": ("_xsrf=abc; heartwood-capability=" + _SECRET + "; other=1",)}
    )
    assert not capability.authorizes({"cookie": ("heartwood-capability=" + _SECRET[:-1],)})
    assert capability.authorizes(
        {"cookie": ("heartwood-capability=stale; heartwood-capability=" + _SECRET,)}
    )
    assert not capability.authorizes({"x-heartwood-capability": ("\u00e9" * 40,)})
    assert not capability.authorizes(
        {"cookie": ("not a cookie;;;=",), "x-heartwood-capability": ()}
    )
    assert not capability.authorizes({})


def test_cookie_is_scoped_to_the_external_path_and_secured_over_https() -> None:
    capability = LaunchCapability(_SECRET, launch_token=None)
    loopback = capability.cookie_header(IngressPolicy.create())
    proxied = capability.cookie_header(
        IngressPolicy.create(
            mode="jupyter-proxy",
            external_origin="https://notebooks.example",
            external_base_path="/proxy/8767",
        )
    )

    assert loopback == f"heartwood-capability={_SECRET}; Path=/; HttpOnly; SameSite=Strict"
    assert proxied == (
        f"heartwood-capability={_SECRET}; Path=/proxy/8767; HttpOnly; SameSite=Strict; Secure"
    )
