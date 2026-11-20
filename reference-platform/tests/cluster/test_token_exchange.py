# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Researcher sign-in and token exchange with the project clients on the cluster.

A synthetic researcher signs in as a browser would. The project client exchanges the
researcher's token for a token of one platform service, and the control service
accepts only a token for itself, from the client of a project the researcher is in.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from identity import (
    CONTROL,
    FIRST,
    SECOND,
    SESSIONS,
    claims,
    device_sign_in,
    exchange,
    launch_url,
    probe,
    request,
    secret,
    sign_in,
    spawner_token,
)
from kube import Cluster


@dataclass(frozen=True)
class Client:
    """A project client with the secret and redirect URI of one session."""

    client_id: str
    client_secret: str
    redirect_uri: str
    session: str


@dataclass(frozen=True)
class Platform:
    cluster: Cluster
    pod: str
    demo: Client
    other: Client

    def sign_in(self, username: str, client: Client, password: str | None = None) -> dict[str, Any]:
        return sign_in(
            self.cluster,
            self.pod,
            username=username,
            client_id=client.client_id,
            client_secret=client.client_secret,
            redirect_uri=client.redirect_uri,
            password=password,
        )

    def exchange(
        self,
        client: Client,
        subject_token: str,
        service: str | None,
        audience: str | None = None,
        *,
        basic: bool = False,
    ) -> tuple[int, dict[str, Any]]:
        return exchange(
            self.cluster,
            self.pod,
            client_id=client.client_id,
            client_secret=client.client_secret,
            subject_token=subject_token,
            service=service,
            audience=audience,
            basic=basic,
        )

    def launch_context(self, token: str) -> tuple[int, Any]:
        return request(self.cluster, self.pod, "GET", f"{CONTROL}/v1/launch-context", token)


@pytest.fixture(scope="module")
def platform(cluster: Cluster, probe_image: str) -> Iterator[Platform]:
    """Launch the first researcher in both projects, as the spawner would."""
    with probe(cluster, "hw-identity-probe", probe_image) as pod:
        clients = {}
        try:
            for project in ("demo", "other"):
                status, launch = request(
                    cluster, pod, "PUT", launch_url(FIRST, project), spawner_token(cluster)
                )
                assert status == 200, launch
                clients[project] = Client(
                    client_id=launch["client_id"],
                    client_secret=secret(
                        cluster, SESSIONS, f"hw-gw-{launch['session']}", "keycloak-client-secret"
                    ),
                    redirect_uri=launch["redirect_uri"],
                    session=launch["session"],
                )
            yield Platform(cluster, pod, clients["demo"], clients["other"])
        finally:
            for project in ("demo", "other"):
                request(cluster, pod, "DELETE", launch_url(FIRST, project), spawner_token(cluster))


@pytest.fixture(scope="module")
def researcher(platform: Platform) -> dict[str, Any]:
    """The tokens of the first researcher, signed in to the client of project demo."""
    return platform.sign_in("researcher-one", platform.demo)


def test_researcher_signs_in_with_the_subject_of_the_registry(
    researcher: dict[str, Any],
) -> None:
    access = claims(researcher["access_token"])
    assert access["sub"] == FIRST
    assert access["azp"] == "hw-proj-demo"
    identity = claims(researcher["id_token"])
    assert identity["sub"] == FIRST
    assert identity["aud"] == "hw-proj-demo"
    # The token of the researcher names no platform service.
    audiences = access.get("aud", [])
    assert "heartwood-control" not in ([audiences] if isinstance(audiences, str) else audiences)


def test_sign_in_with_a_wrong_password_issues_no_code(platform: Platform) -> None:
    answer = platform.sign_in("researcher-one", platform.demo, password="not-the-password")
    assert answer["error"] == "no-code"


def test_control_service_refuses_the_token_of_the_researcher(
    platform: Platform, researcher: dict[str, Any]
) -> None:
    status, _ = platform.launch_context(researcher["access_token"])
    assert status == 401


def test_project_client_exchanges_for_one_service_audience(
    platform: Platform, researcher: dict[str, Any]
) -> None:
    status, exchanged = platform.exchange(
        platform.demo, researcher["access_token"], "control", "heartwood-control"
    )
    assert status == 200, exchanged.get("error_description")
    token = claims(exchanged["access_token"])
    assert token["aud"] == "heartwood-control"
    assert token["azp"] == "hw-proj-demo"
    assert token["sub"] == FIRST
    assert token["exp"] <= claims(researcher["access_token"])["exp"]
    assert "refresh_token" not in exchanged

    status, context = platform.launch_context(exchanged["access_token"])
    assert status == 200, context
    assert context["project_id"] == "demo"
    assert context["project_context_id"] == platform.demo.session


def test_exchange_names_its_scope_and_an_offered_audience(
    platform: Platform, researcher: dict[str, Any]
) -> None:
    token = researcher["access_token"]
    # The audience scopes are optional, so an audience without its scope is not offered.
    status, refused = platform.exchange(platform.demo, token, None, "heartwood-control")
    assert status == 400
    assert refused["error"] == "invalid_request"
    status, refused = platform.exchange(platform.demo, token, "control", "heartwood-jobs")
    assert status == 400
    assert refused["error"] == "invalid_request"


def test_another_project_client_cannot_exchange_the_token(
    platform: Platform, researcher: dict[str, Any]
) -> None:
    status, refused = platform.exchange(
        platform.other, researcher["access_token"], "control", "heartwood-control"
    )
    assert status in {400, 403}
    assert "access_token" not in refused


def test_control_service_refuses_a_token_for_another_service(
    platform: Platform, researcher: dict[str, Any]
) -> None:
    status, exchanged = platform.exchange(
        platform.demo, researcher["access_token"], "data", "heartwood-data"
    )
    assert status == 200, exchanged.get("error_description")
    assert claims(exchanged["access_token"])["aud"] == "heartwood-data"

    status, _ = platform.launch_context(exchanged["access_token"])
    assert status == 401


def test_exchanged_token_names_the_project_of_its_client(platform: Platform) -> None:
    """A token that the client of one project obtained never answers for another."""
    tokens = platform.sign_in("researcher-one", platform.other)
    status, exchanged = platform.exchange(
        platform.other, tokens["access_token"], "control", "heartwood-control"
    )
    assert status == 200, exchanged.get("error_description")
    assert claims(exchanged["access_token"])["azp"] == "hw-proj-other"
    status, context = platform.launch_context(exchanged["access_token"])
    assert status == 200, context
    assert context["project_id"] == "other"
    assert context["project_context_id"] == platform.other.session


def test_registry_refuses_a_researcher_outside_the_project(platform: Platform) -> None:
    """Keycloak lets any account sign in to a project client; the registry decides."""
    tokens = platform.sign_in("researcher-two", platform.other)
    assert claims(tokens["access_token"])["sub"] == SECOND
    status, exchanged = platform.exchange(
        platform.other, tokens["access_token"], "control", "heartwood-control"
    )
    assert status == 200, exchanged.get("error_description")

    status, _ = platform.launch_context(exchanged["access_token"])
    assert status == 403


def test_command_line_token_is_exchanged_by_the_project_client(platform: Platform) -> None:
    """The device sign-in with a project scope gives a token that the gateway exchanges."""
    tokens = device_sign_in(
        platform.cluster, platform.pod, username="researcher-one", scope="openid project-demo"
    )
    access = claims(tokens["access_token"])
    assert access["sub"] == FIRST
    assert access["azp"] == "heartwood-cli"
    audiences = access["aud"] if isinstance(access["aud"], list) else [access["aud"]]
    assert "hw-proj-demo" in audiences
    assert "hw-proj-other" not in audiences

    # The gateway authenticates with HTTP Basic; the client of project demo may exchange.
    status, exchanged = platform.exchange(
        platform.demo, tokens["access_token"], "control", "heartwood-control", basic=True
    )
    assert status == 200, exchanged.get("error_description")
    token = claims(exchanged["access_token"])
    assert (token["sub"], token["azp"], token["aud"]) == (
        FIRST,
        "hw-proj-demo",
        "heartwood-control",
    )
    status, context = platform.launch_context(exchanged["access_token"])
    assert status == 200, context
    assert context["project_id"] == "demo"

    # The client of project other is not in the token's audience.
    status, refused = platform.exchange(
        platform.other, tokens["access_token"], "control", "heartwood-control", basic=True
    )
    assert status in {400, 403}, (status, refused.get("error"))
    assert "access_token" not in refused
