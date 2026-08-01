# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Adversarial tests for the gateway ingress contract."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from heartwood.gateway import (
    IngressConfigurationError,
    IngressPolicy,
    IngressRequestError,
)


def _scope(
    path: str = "/sessions/session-1/events",
    *,
    host: str = "127.0.0.1:8767",
    origin: str | None = "http://127.0.0.1:8767",
    client: str = "127.0.0.1",
    headers: Sequence[tuple[str, str]] = (),
    raw_path: bytes | None = None,
    query_string: bytes = b"",
) -> dict[str, object]:
    request_headers = [("host", host), *headers]
    if origin is not None:
        request_headers.append(("origin", origin))
    return {
        "type": "http",
        "method": "GET",
        "path": path,
        "raw_path": path.encode("utf-8") if raw_path is None else raw_path,
        "query_string": query_string,
        "headers": [
            (name.encode("ascii"), value.encode("latin-1")) for name, value in request_headers
        ],
        "client": (client, 43123),
    }


def test_direct_loopback_normalizes_one_prefixed_route() -> None:
    policy = IngressPolicy.create(external_base_path="/heartwood")

    request = policy.validate_scope(_scope("/heartwood/sessions/session-1/events"))

    assert request.path == "/sessions/session-1/events"
    assert request.query_string == ""
    assert request.external_origin == "http://127.0.0.1:8767"
    assert request.external_base_path == "/heartwood"
    assert policy.browser_base_path == "/heartwood"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"mode": "unknown"}, "unsupported gateway ingress mode"),
        ({"bind_port": 0}, "between 1 and 65535"),
        ({"bind_host": "gateway.example"}, "literal loopback"),
        (
            {
                "mode": "jupyter-proxy",
                "bind_host": "10.0.0.4",
                "external_origin": "https://notebooks.example",
                "external_base_path": "/proxy/8767",
            },
            "loopback gateway bind",
        ),
        ({"prefix_handling": "rewrite"}, "preserve or strip"),
        ({"external_origin": "https://heartwood.example"}, "loopback external origin"),
        ({"trusted_proxy_sources": ("127.0.0.1",)}, "trusted proxy metadata"),
        ({"prefix_handling": "strip"}, "must preserve"),
        (
            {
                "mode": "jupyter-proxy",
                "external_origin": "https://notebooks.example",
            },
            "exact external proxy base path",
        ),
        (
            {
                "mode": "jupyter-proxy",
                "external_origin": "https://notebooks.example",
                "external_base_path": "/proxy/8767",
                "trusted_identity_header": "x-heartwood-proxy",
                "trusted_identity": "platform",
            },
            "cannot configure trusted-proxy source or identity assertions",
        ),
        (
            {
                "mode": "jupyter-proxy",
                "external_origin": "https://notebooks.example",
                "external_base_path": "/proxy/8767",
                "prefix_handling": "preserve",
            },
            "must strip",
        ),
        (
            {
                "mode": "trusted-proxy",
                "bind_host": "0.0.0.0",
                "external_origin": "https://heartwood.example",
                "trusted_proxy_sources": ("10.0.0.4",),
                "host_loopback_publication": True,
            },
            "host-loopback publication boundary",
        ),
        ({"external_base_path": 42}, "must be a string"),
        ({"external_base_path": "/../gateway"}, "traversal"),
        ({"external_origin": "https://heartwood .example"}, "whitespace"),
        ({"external_origin": "https://heartwood.example:invalid"}, "invalid port"),
        ({"external_origin": "https://\ud800.example"}, "not valid IDNA"),
        ({"trusted_proxy_sources": ("not-a-network",)}, "invalid trusted proxy"),
        ({"trusted_identity_header": "x-heartwood-proxy"}, "configured together"),
        (
            {
                "trusted_identity_header": "authorization",
                "trusted_identity": "proxy",
            },
            "invalid or reserved",
        ),
        (
            {
                "trusted_identity_header": "x-heartwood-proxy",
                "trusted_identity": "",
            },
            "identity value is invalid",
        ),
        (
            {
                "mode": "trusted-proxy",
                "bind_host": "0.0.0.0",
                "external_origin": "",
                "trusted_proxy_sources": ("10.0.0.4",),
            },
            "requires the exact external origin",
        ),
        (
            {
                "mode": "trusted-proxy",
                "bind_host": "0.0.0.0",
                "trusted_proxy_sources": ("10.0.0.4",),
            },
            "requires the exact external origin",
        ),
    ],
)
def test_ingress_configuration_rejects_contradictory_routes(
    kwargs: dict[str, Any],
    message: str,
) -> None:
    with pytest.raises(IngressConfigurationError, match=message):
        IngressPolicy.create(**kwargs)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.0.2.20"])
def test_direct_loopback_rejects_non_loopback_bind(host: str) -> None:
    with pytest.raises(IngressConfigurationError, match="loopback bind"):
        IngressPolicy.create(bind_host=host)


def test_host_loopback_publication_requires_an_explicit_loopback_public_route() -> None:
    policy = IngressPolicy.create(
        bind_host="0.0.0.0",
        host_loopback_publication=True,
    )

    assert policy.external_origin == "http://127.0.0.1:8767"
    with pytest.raises(IngressConfigurationError, match="loopback external origin"):
        IngressPolicy.create(
            bind_host="0.0.0.0",
            external_origin="http://192.0.2.20:8767",
            host_loopback_publication=True,
        )


def test_ingress_normalizes_localhost_and_an_exact_base_route() -> None:
    policy = IngressPolicy.create(
        bind_host="localhost",
        external_base_path="/heartwood",
    )

    assert policy.bind_host == "127.0.0.1"
    assert policy.validate_scope(_scope("/heartwood")).path == "/"


def test_ingress_normalizes_unicode_origin_hostname_to_idna() -> None:
    policy = IngressPolicy.create(
        mode="jupyter-proxy",
        external_origin="https://m\u00fcnchen.example",
        external_base_path="/proxy/8767",
    )

    assert policy.external_origin == "https://xn--mnchen-3ya.example"


def test_direct_loopback_validates_the_actual_request_source() -> None:
    policy = IngressPolicy.create()

    with pytest.raises(IngressRequestError, match="local boundary"):
        policy.validate_scope(_scope(client="8.8.8.8"))

    published_policy = IngressPolicy.create(
        bind_host="0.0.0.0",
        host_loopback_publication=True,
    )
    assert published_policy.validate_scope(_scope(client="172.17.0.1")).path.startswith(
        "/sessions/"
    )
    with pytest.raises(IngressRequestError, match="local boundary"):
        published_policy.validate_scope(_scope(client="8.8.8.8"))


@pytest.mark.parametrize(
    ("headers", "message"),
    [
        ([("x-forwarded-host", "attacker.example")], "forwarded metadata"),
        ([("x-real-ip", "198.51.100.22")], "forwarded metadata"),
        ([("host", "attacker.example")], "must not repeat"),
        ([("origin", "https://attacker.example")], "must not repeat"),
        ([("sec-fetch-site", "cross-site")], "cross-origin"),
    ],
)
def test_direct_loopback_rejects_spoofed_browser_and_proxy_metadata(
    headers: Sequence[tuple[str, str]],
    message: str,
) -> None:
    policy = IngressPolicy.create()

    with pytest.raises(IngressRequestError, match=message):
        policy.validate_scope(_scope(headers=headers))


def test_direct_loopback_rejects_host_and_origin_injection() -> None:
    policy = IngressPolicy.create()

    with pytest.raises(IngressRequestError, match="request host"):
        policy.validate_scope(_scope(host="attacker.example"))
    with pytest.raises(IngressRequestError, match="request origin"):
        policy.validate_scope(_scope(origin="https://attacker.example"))


def test_direct_loopback_rejects_non_idna_request_authority() -> None:
    with pytest.raises(IngressRequestError, match="request authority is malformed"):
        IngressPolicy.create().validate_scope(_scope(host="\x80"))


def test_websocket_requires_the_exact_origin() -> None:
    policy = IngressPolicy.create()

    with pytest.raises(IngressRequestError, match="require an Origin"):
        policy.validate_scope(_scope(origin=None), websocket=True)
    with pytest.raises(IngressRequestError, match="request origin"):
        policy.validate_scope(
            _scope(origin="https://attacker.example"),
            websocket=True,
        )


def test_request_rejects_a_malformed_origin() -> None:
    with pytest.raises(IngressRequestError, match="origin is malformed"):
        IngressPolicy.create().validate_scope(_scope(origin="not-an-origin"))


def test_same_origin_http_request_can_omit_origin() -> None:
    request = IngressPolicy.create().validate_scope(
        _scope(
            origin=None,
            headers=[("sec-fetch-site", "same-origin")],
        )
    )

    assert request.path == "/sessions/session-1/events"


@pytest.mark.parametrize(
    ("path", "raw_path", "message"),
    [
        (
            "/sessions/session-1%2Fevents",
            b"/sessions/session-1%2Fevents",
            "percent-encoded",
        ),
        (
            "/sessions/session-1/events",
            b"/sessions/session-1%2Fevents",
            "percent-encoded",
        ),
        ("/sessions/../session-1/events", None, "traversal"),
        ("/sessions//session-1/events", None, "empty segment"),
        ("/sessions\\session-1", None, "forbidden character"),
    ],
)
def test_paths_reject_encoded_separators_and_ambiguous_segments(
    path: str,
    raw_path: bytes | None,
    message: str,
) -> None:
    with pytest.raises(IngressRequestError, match=message):
        IngressPolicy.create().validate_scope(_scope(path, raw_path=raw_path))


def test_request_rejects_a_second_asgi_root_path() -> None:
    scope = _scope()
    scope["root_path"] = "/proxy/8767"

    with pytest.raises(IngressRequestError, match="prefix once"):
        IngressPolicy.create().validate_scope(scope)


@pytest.mark.parametrize(
    ("query_string", "message"),
    [
        (b"\xff", "valid UTF-8"),
        (b"after=1\nignored=true", "forbidden character"),
        (b"x=" + (b"a" * 8_192), "too long"),
    ],
)
def test_request_rejects_malformed_or_oversized_query(
    query_string: bytes,
    message: str,
) -> None:
    with pytest.raises(IngressRequestError, match=message):
        IngressPolicy.create().validate_scope(_scope(query_string=query_string))


def test_jupyter_proxy_accepts_only_the_stripped_loopback_route() -> None:
    policy = IngressPolicy.create(
        mode="jupyter-proxy",
        external_origin="https://notebooks.firecloud.org",
        external_base_path="/proxy/project/runtime/jupyter/proxy/8767",
    )
    request = policy.validate_scope(
        _scope(
            "/sessions/session-1/events",
            host="127.0.0.1:8767",
            origin="https://notebooks.firecloud.org",
            headers=[
                (
                    "x-forwarded-context",
                    "/proxy/project/runtime/jupyter/proxy/8767",
                ),
                (
                    "x-proxycontextpath",
                    "/proxy/project/runtime/jupyter/proxy/8767",
                ),
                (
                    "x-forwarded-prefix",
                    "/proxy/project/runtime/jupyter/proxy/8767",
                ),
                ("x-forwarded-for", "198.51.100.22, 10.10.0.4"),
                ("x-forwarded-host", "notebooks.firecloud.org"),
                ("x-forwarded-proto", "https"),
            ],
        )
    )

    assert request.path == "/sessions/session-1/events"
    with pytest.raises(IngressRequestError, match="must be stripped"):
        policy.validate_scope(
            _scope(
                "/proxy/project/runtime/jupyter/proxy/8767/sessions/session-1/events",
                origin="https://notebooks.firecloud.org",
            )
        )
    with pytest.raises(IngressRequestError, match="loopback"):
        policy.validate_scope(
            _scope(
                origin="https://notebooks.firecloud.org",
                client="10.0.0.8",
            )
        )
    with pytest.raises(IngressRequestError, match="forwarded host"):
        policy.validate_scope(
            _scope(
                origin="https://notebooks.firecloud.org",
                headers=[
                    ("x-forwarded-host", "attacker.example"),
                    ("x-forwarded-proto", "https"),
                ],
            )
        )


@pytest.mark.parametrize(
    ("headers", "message"),
    [
        ([("x-forwarded-context", "/proxy/8767")], "context metadata must include"),
        ([("x-forwarded-host", "notebooks.firecloud.org")], "must include host and protocol"),
        ([("x-forwarded-for", "not-an-ip")], "valid IP"),
        ([("x-real-ip", "198.51.100.22")], "unsupported forwarded metadata"),
        (
            [
                ("x-forwarded-context", "/proxy/other"),
                ("x-proxycontextpath", "/proxy/other"),
                ("x-forwarded-prefix", "/proxy/other"),
            ],
            "configured external base path",
        ),
    ],
)
def test_jupyter_proxy_rejects_ambiguous_forwarding_metadata(
    headers: Sequence[tuple[str, str]],
    message: str,
) -> None:
    policy = IngressPolicy.create(
        mode="jupyter-proxy",
        external_origin="https://notebooks.firecloud.org",
        external_base_path="/proxy/project/runtime/jupyter/proxy/8767",
    )

    with pytest.raises(IngressRequestError, match=message):
        policy.validate_scope(
            _scope(
                origin="https://notebooks.firecloud.org",
                headers=headers,
            )
        )


def test_jupyter_proxy_requires_the_external_origin() -> None:
    with pytest.raises(IngressConfigurationError, match="exact external origin"):
        IngressPolicy.create(
            mode="jupyter-proxy",
            external_base_path="/proxy/project/runtime/jupyter/proxy/8767",
        )


def test_trusted_proxy_validates_source_route_client_and_identity() -> None:
    policy = IngressPolicy.create(
        mode="trusted-proxy",
        bind_host="0.0.0.0",
        external_origin="https://heartwood.example",
        external_base_path="/research/heartwood",
        prefix_handling="strip",
        trusted_proxy_sources=("10.10.0.0/24",),
        trusted_identity_header="x-heartwood-proxy",
        trusted_identity="research-platform",
    )
    request = policy.validate_scope(
        _scope(
            host="heartwood.example",
            origin="https://heartwood.example",
            client="10.10.0.4",
            headers=[
                ("x-forwarded-for", "198.51.100.22"),
                ("x-forwarded-host", "heartwood.example"),
                ("x-forwarded-prefix", "/research/heartwood"),
                ("x-forwarded-proto", "https"),
                ("x-heartwood-proxy", "research-platform"),
            ],
        )
    )

    assert request.path == "/sessions/session-1/events"
    assert request.client_ip == "198.51.100.22"
    safe_configuration = policy.safe_dict()
    assert safe_configuration["trusted_proxy_sources"] == ("10.10.0.0/24",)
    assert safe_configuration["trusted_identity_configured"] is True
    assert "trusted_identity" not in safe_configuration


@pytest.mark.parametrize(
    ("header", "value", "message"),
    [
        ("x-forwarded-for", "not-an-ip", "valid IP"),
        ("x-forwarded-host", "attacker.example", "forwarded host"),
        ("x-forwarded-prefix", "/other", "forwarded prefix"),
        ("x-forwarded-proto", "http", "forwarded protocol"),
        ("x-heartwood-proxy", "other-platform", "proxy identity"),
    ],
)
def test_trusted_proxy_rejects_conflicting_forwarded_metadata(
    header: str,
    value: str,
    message: str,
) -> None:
    forwarded = {
        "x-forwarded-for": "198.51.100.22",
        "x-forwarded-host": "heartwood.example",
        "x-forwarded-prefix": "/research/heartwood",
        "x-forwarded-proto": "https",
        "x-heartwood-proxy": "research-platform",
    }
    forwarded[header] = value
    policy = IngressPolicy.create(
        mode="trusted-proxy",
        bind_host="0.0.0.0",
        external_origin="https://heartwood.example",
        external_base_path="/research/heartwood",
        prefix_handling="strip",
        trusted_proxy_sources=("10.10.0.0/24",),
        trusted_identity_header="x-heartwood-proxy",
        trusted_identity="research-platform",
    )

    with pytest.raises(IngressRequestError, match=message):
        policy.validate_scope(
            _scope(
                host="heartwood.example",
                origin="https://heartwood.example",
                client="10.10.0.4",
                headers=list(forwarded.items()),
            )
        )


def test_trusted_proxy_rejects_untrusted_source_and_partial_metadata() -> None:
    policy = IngressPolicy.create(
        mode="trusted-proxy",
        bind_host="0.0.0.0",
        external_origin="https://heartwood.example",
        trusted_proxy_sources=("10.10.0.0/24",),
    )

    with pytest.raises(IngressRequestError, match="trusted proxy source"):
        policy.validate_scope(
            _scope(
                host="heartwood.example",
                origin="https://heartwood.example",
                client="10.20.0.4",
            )
        )
    with pytest.raises(IngressRequestError, match="must include client"):
        policy.validate_scope(
            _scope(
                host="heartwood.example",
                origin="https://heartwood.example",
                client="10.10.0.4",
                headers=[("x-forwarded-host", "heartwood.example")],
            )
        )
    with pytest.raises(IngressRequestError, match="must include client"):
        policy.validate_scope(
            _scope(
                host="heartwood.example",
                origin="https://heartwood.example",
                client="10.10.0.4",
            )
        )


def test_trusted_proxy_rejects_unmodeled_forwarding_metadata() -> None:
    policy = IngressPolicy.create(
        mode="trusted-proxy",
        bind_host="0.0.0.0",
        external_origin="https://heartwood.example",
        trusted_proxy_sources=("10.10.0.0/24",),
    )

    with pytest.raises(IngressRequestError, match="unsupported forwarded metadata"):
        policy.validate_scope(
            _scope(
                host="heartwood.example",
                origin="https://heartwood.example",
                client="10.10.0.4",
                headers=[("x-real-ip", "198.51.100.22")],
            )
        )


def test_trusted_proxy_rejects_jupyter_context_metadata() -> None:
    policy = IngressPolicy.create(
        mode="trusted-proxy",
        bind_host="0.0.0.0",
        external_origin="https://heartwood.example",
        trusted_proxy_sources=("10.10.0.0/24",),
    )

    with pytest.raises(IngressRequestError, match="unsupported forwarded metadata"):
        policy.validate_scope(
            _scope(
                host="heartwood.example",
                origin="https://heartwood.example",
                client="10.10.0.4",
                headers=[
                    ("x-forwarded-context", "/"),
                    ("x-forwarded-for", "198.51.100.22"),
                    ("x-forwarded-host", "heartwood.example"),
                    ("x-forwarded-prefix", "/"),
                    ("x-forwarded-proto", "https"),
                ],
            )
        )


@pytest.mark.parametrize(
    ("headers", "message"),
    [
        (
            [
                ("forwarded", "for=198.51.100.22;proto=https"),
                ("x-forwarded-for", "198.51.100.22"),
                ("x-forwarded-host", "heartwood.example"),
                ("x-forwarded-prefix", "/"),
                ("x-forwarded-proto", "https"),
            ],
            "RFC Forwarded",
        ),
        (
            [
                ("x-original-url", "/sessions"),
                ("x-forwarded-for", "198.51.100.22"),
                ("x-forwarded-host", "heartwood.example"),
                ("x-forwarded-prefix", "/"),
                ("x-forwarded-proto", "https"),
            ],
            "rewritten URL",
        ),
        (
            [
                ("x-forwarded-for", "198.51.100.22, 198.51.100.23"),
                ("x-forwarded-host", "heartwood.example"),
                ("x-forwarded-prefix", "/"),
                ("x-forwarded-proto", "https"),
            ],
            "one address",
        ),
        (
            [
                ("x-forwarded-for", "198.51.100.22"),
                ("x-forwarded-host", "heartwood.example"),
                ("x-forwarded-prefix", "%2f"),
                ("x-forwarded-proto", "https"),
            ],
            "prefix is malformed",
        ),
    ],
)
def test_trusted_proxy_rejects_ambiguous_forwarding_sets(
    headers: Sequence[tuple[str, str]],
    message: str,
) -> None:
    policy = IngressPolicy.create(
        mode="trusted-proxy",
        bind_host="0.0.0.0",
        external_origin="https://heartwood.example",
        trusted_proxy_sources=("10.10.0.0/24",),
    )

    with pytest.raises(IngressRequestError, match=message):
        policy.validate_scope(
            _scope(
                host="heartwood.example",
                origin="https://heartwood.example",
                client="10.10.0.4",
                headers=headers,
            )
        )


def test_trusted_proxy_rejects_duplicate_forwarded_metadata() -> None:
    policy = IngressPolicy.create(
        mode="trusted-proxy",
        bind_host="0.0.0.0",
        external_origin="https://heartwood.example",
        trusted_proxy_sources=("10.10.0.0/24",),
    )

    with pytest.raises(IngressRequestError, match="must not repeat"):
        policy.validate_scope(
            _scope(
                host="heartwood.example",
                origin="https://heartwood.example",
                client="10.10.0.4",
                headers=[
                    ("x-forwarded-for", "198.51.100.22"),
                    ("x-forwarded-for", "198.51.100.23"),
                    ("x-forwarded-host", "heartwood.example"),
                    ("x-forwarded-prefix", "/"),
                    ("x-forwarded-proto", "https"),
                ],
            )
        )


@pytest.mark.parametrize(
    "source",
    ["0.0.0.0/0", "::/0"],
)
def test_trusted_proxy_rejects_wildcard_source_ranges(source: str) -> None:
    with pytest.raises(IngressConfigurationError, match="wildcard"):
        IngressPolicy.create(
            mode="trusted-proxy",
            bind_host="0.0.0.0",
            external_origin="https://heartwood.example",
            trusted_proxy_sources=(source,),
        )


@pytest.mark.parametrize(
    "origin",
    ["https://*", "https://*.example.org"],
)
def test_ingress_rejects_wildcard_external_origins(origin: str) -> None:
    with pytest.raises(IngressConfigurationError, match="external origin"):
        IngressPolicy.create(
            mode="trusted-proxy",
            bind_host="0.0.0.0",
            external_origin=origin,
            trusted_proxy_sources=("10.10.0.0/24",),
        )


def test_ingress_rejects_malformed_asgi_scope_values() -> None:
    policy = IngressPolicy.create()
    malformed_headers = _scope()
    malformed_headers["headers"] = "not-headers"
    malformed_header_item = _scope()
    malformed_header_item["headers"] = [b"not-a-pair"]
    list_header_pairs = _scope()
    list_header_pairs["headers"] = [
        [b"host", b"127.0.0.1:8767"],
        [b"origin", b"http://127.0.0.1:8767"],
    ]
    non_ascii_header = _scope()
    non_ascii_header["headers"] = [(b"\xff", b"value")]
    missing_host = _scope()
    missing_host["headers"] = [(b"origin", b"http://127.0.0.1:8767")]
    malformed_host = _scope()
    malformed_host["headers"] = [
        (b"host", b"127.0.0.1:\n8767"),
        (b"origin", b"http://127.0.0.1:8767"),
    ]
    empty_authority = _scope(host=":")
    invalid_port = _scope(host="127.0.0.1:not-a-port")
    malformed_client_shape = _scope()
    malformed_client_shape["client"] = "not-a-client"
    malformed_client = _scope()
    malformed_client["client"] = ("not-an-ip", 43123)
    malformed_path = _scope()
    malformed_path["path"] = 42
    implicit_raw_path = _scope()
    implicit_raw_path.pop("raw_path")
    malformed_raw_path = _scope()
    malformed_raw_path["raw_path"] = "not-bytes"
    non_utf8_raw_path = _scope()
    non_utf8_raw_path["raw_path"] = b"/sessions/\xff"
    mismatched_path = _scope(raw_path=b"/other")
    malformed_query = _scope()
    malformed_query["query_string"] = "not-bytes"

    assert policy.validate_scope(implicit_raw_path).path == "/sessions/session-1/events"
    assert policy.validate_scope(list_header_pairs).path == "/sessions/session-1/events"
    for scope, message in (
        (malformed_headers, "headers are malformed"),
        (malformed_header_item, "headers are malformed"),
        (non_ascii_header, "header name is not ASCII"),
        (missing_host, "requires one host header"),
        (malformed_host, "host header is malformed"),
        (empty_authority, "authority is malformed"),
        (invalid_port, "authority has an invalid port"),
        (malformed_client_shape, "could not identify"),
        (malformed_client, "not a valid IP"),
        (malformed_path, "path must be absolute"),
        (malformed_raw_path, "raw request path is malformed"),
        (non_utf8_raw_path, "path is not valid UTF-8"),
        (mismatched_path, "do not agree"),
        (malformed_query, "query is malformed"),
    ):
        with pytest.raises(IngressRequestError, match=message):
            policy.validate_scope(scope)
