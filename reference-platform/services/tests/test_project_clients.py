# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fakes import FakeKeycloak
from heartwood_platform.control.keycloak import Keycloak


@pytest.fixture
def server() -> FakeKeycloak:
    return FakeKeycloak()


def _keycloak(server: FakeKeycloak, now: list[float] | None = None) -> Keycloak:
    clock = (lambda: now[0]) if now is not None else (lambda: 0.0)
    return Keycloak(server.client(), "heartwood", "heartwood-control", lambda: server.secret, clock)


def test_project_client_is_confidential_with_token_exchange_and_service_scopes(
    server: FakeKeycloak,
) -> None:
    _keycloak(server).ensure_project_client("demo")

    client = server.by_client_id("hw-proj-demo")
    assert client == {
        "id": client["id"],
        "clientId": "hw-proj-demo",
        "protocol": "openid-connect",
        "publicClient": False,
        "standardFlowEnabled": True,
        "implicitFlowEnabled": False,
        "directAccessGrantsEnabled": False,
        "serviceAccountsEnabled": False,
        "redirectUris": [],
        "attributes": {"standard.token.exchange.enabled": "true"},
        "optionalClientScopes": ["svc-control", "svc-data", "svc-jobs"],
    }


def test_existing_project_client_is_left_unchanged(server: FakeKeycloak) -> None:
    keycloak = _keycloak(server)
    keycloak.ensure_project_client("demo")
    keycloak.add_redirect_uri("demo", "https://hub.example.test/a")
    before = dict(server.by_client_id("hw-proj-demo"))
    server.requests.clear()

    keycloak.ensure_project_client("demo")

    assert server.by_client_id("hw-proj-demo") == before
    assert server.requests == [("GET", "/admin/realms/heartwood/clients")]


def test_missing_service_scope_is_attached_to_an_existing_client(server: FakeKeycloak) -> None:
    keycloak = _keycloak(server)
    keycloak.ensure_project_client("demo")
    server.by_client_id("hw-proj-demo")["optionalClientScopes"].remove("svc-jobs")

    keycloak.ensure_project_client("demo")

    assert sorted(server.by_client_id("hw-proj-demo")["optionalClientScopes"]) == [
        "svc-control",
        "svc-data",
        "svc-jobs",
    ]


def test_redirect_uris_are_added_and_removed(server: FakeKeycloak) -> None:
    keycloak = _keycloak(server)
    keycloak.ensure_project_client("demo")

    keycloak.add_redirect_uri("demo", "https://hub.example.test/b")
    keycloak.add_redirect_uri("demo", "https://hub.example.test/a")
    keycloak.add_redirect_uri("demo", "https://hub.example.test/a")
    assert server.by_client_id("hw-proj-demo")["redirectUris"] == [
        "https://hub.example.test/a",
        "https://hub.example.test/b",
    ]

    keycloak.remove_redirect_uri("demo", "https://hub.example.test/a")
    keycloak.remove_redirect_uri("demo", "https://hub.example.test/missing")
    keycloak.remove_redirect_uri("absent", "https://hub.example.test/a")
    assert server.by_client_id("hw-proj-demo")["redirectUris"] == ["https://hub.example.test/b"]
    with pytest.raises(LookupError):
        keycloak.add_redirect_uri("absent", "https://hub.example.test/a")


def test_concurrent_redirect_uri_updates_of_one_client_lose_nothing(server: FakeKeycloak) -> None:
    keycloak = _keycloak(server)
    keycloak.ensure_project_client("demo")
    keycloak.ensure_project_client("other")
    # A slow update widens the window in which an unserialized writer would overwrite.
    server.update_delay = 0.01
    uris = [f"https://hub.example.test/user/{index}/demo/oauth_callback" for index in range(12)]

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(lambda uri: keycloak.add_redirect_uri("demo", uri), uris))
        list(pool.map(lambda uri: keycloak.add_redirect_uri("other", uri), uris[:2]))

    assert server.by_client_id("hw-proj-demo")["redirectUris"] == sorted(uris)
    assert len(server.by_client_id("hw-proj-other")["redirectUris"]) == 2


def test_administration_token_requests_the_admin_scope_and_is_reused(server: FakeKeycloak) -> None:
    now = [0.0]
    keycloak = _keycloak(server, now)

    keycloak.ensure_project_client("demo")
    keycloak.add_redirect_uri("demo", "https://hub.example.test/a")
    assert server.token_requests == [
        {
            "grant_type": "client_credentials",
            "client_id": "heartwood-control",
            "client_secret": "control-secret",
            "scope": "kc-admin-api",
        }
    ]

    now[0] = 31.0
    assert keycloak.client_secret("demo") == "secret-of-uuid-1"
    assert len(server.token_requests) == 2


def test_refused_credentials_raise(server: FakeKeycloak) -> None:
    keycloak = Keycloak(server.client(), "heartwood", "heartwood-control", lambda: "wrong")

    with pytest.raises(httpx.HTTPStatusError):
        keycloak.ensure_project_client("demo")
    assert server.clients == {}
