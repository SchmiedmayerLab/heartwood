# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import base64
import json
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fakes import (
    AUDIENCE,
    HUB,
    ISSUER,
    OPERATOR,
    SYNTHETIC_PRICES,
    FakeKeycloak,
    FakeKubernetes,
    SigningKey,
    TokenReviews,
)
from heartwood_platform.common.kubernetes import Kubernetes
from heartwood_platform.common.tokens import ResearcherTokens, ServiceTokens
from heartwood_platform.control.__main__ import Settings, _actor_key
from heartwood_platform.control.app import create_app
from heartwood_platform.control.keycloak import Keycloak
from heartwood_platform.control.launch import LaunchService
from heartwood_platform.control.prices import PriceSheet
from heartwood_platform.control.registry import Project, Registry, Session
from heartwood_platform.control.sessions import ProvisionedSession, Sessions, SessionSettings
from starlette.testclient import TestClient
from test_hosted import HOSTED

from heartwood.schemas import PlatformLaunchContext

_HUB = {"Authorization": "Bearer hub-token"}
_ALICE = "0b9f5d1e-7c64-4f0e-9d53-6f6f3f0f1a01"
_BOB = "0b9f5d1e-7c64-4f0e-9d53-6f6f3f0f1a02"
_NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)

_OBJECTS = (
    Path(__file__).resolve().parents[2]
    / "chart"
    / "heartwood-platform"
    / "files"
    / "openshell-session.json"
)
_TEMPLATE = "https://hub.example.test/user/{subject}/{project}/oauth_callback"


class Platform:
    def __init__(self, directory: Path) -> None:
        self.key = SigningKey()
        self.keycloak = FakeKeycloak()
        self.reviews = TokenReviews()
        self.registry = Registry(directory / "control.sqlite3")
        self.registry.synchronize(
            [
                Project("demo", "grant-demo", "policy-1", frozenset({_ALICE, _BOB})),
                Project("other", "grant-other", "policy-1", frozenset({_ALICE})),
            ]
        )
        keycloak = Keycloak(
            self.keycloak.client(), "heartwood", "heartwood-control", lambda: "control-secret"
        )
        self.kubernetes = FakeKubernetes()
        sessions = Sessions(
            Kubernetes(self.kubernetes.client()),
            SessionSettings(
                control_namespace="heartwood-platform",
                control_account="heartwood-control",
                gateway_namespace="heartwood-sessions",
                openshell_objects=_OBJECTS.read_text(encoding="utf-8"),
                openshell_gateway_image="registry.example.test/gateway@sha256:" + "1" * 64,
                api_server=(),
                quota={"pods": "20"},
                container_defaults={"cpu": "500m"},
                container_default_requests={"cpu": "100m"},
                platform_ca="-----BEGIN CERTIFICATE-----\n",
                actor_key="synthetic-actor-key-0123456789abcdef",
            ),
            self.registry.session_ids,
        )
        launches = LaunchService(
            self.registry, keycloak, sessions, "reference-platform", _TEMPLATE, clock=lambda: _NOW
        )
        self.client = TestClient(
            create_app(
                launches,
                ResearcherTokens(ISSUER, AUDIENCE, self.key.key_set),
                ServiceTokens(self.reviews.review, AUDIENCE, frozenset({HUB})),
                PriceSheet.model_validate_json(json.dumps(SYNTHETIC_PRICES)),
                clock=lambda: _NOW,
                operators=ServiceTokens(self.reviews.review, AUDIENCE, frozenset({OPERATOR})),
            )
        )

    def launch(self, subject: str, project: str) -> dict[str, object]:
        response = self.client.put(
            f"/v1/researchers/{subject}/projects/{project}/launch", headers=_HUB
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert isinstance(body, dict)
        return body

    def researcher(self, subject: str, project: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.key.token(sub=subject, azp=f'hw-proj-{project}')}"}


@pytest.fixture
def platform(tmp_path: Path) -> Platform:
    return Platform(tmp_path)


def test_health_needs_no_token(platform: Platform) -> None:
    assert platform.client.get("/healthz").json() == {"status": "ok"}


def test_spawner_lists_the_projects_of_a_researcher(platform: Platform) -> None:
    def projects(subject: str) -> object:
        return platform.client.get(f"/v1/researchers/{subject}/projects", headers=_HUB).json()

    assert projects(_ALICE) == {"projects": ["demo", "other"]}
    assert projects(_BOB) == {"projects": ["demo"]}
    assert projects("unknown") == {"projects": []}


def test_launch_returns_the_registry_binding_and_prepares_the_project_client(
    platform: Platform,
) -> None:
    launch = platform.launch(_BOB, "demo")

    redirect_uri = f"https://hub.example.test/user/{_BOB}/demo/oauth_callback"
    context = PlatformLaunchContext.model_validate_json(
        platform.client.get("/v1/launch-context", headers=platform.researcher(_BOB, "demo")).content
    )
    assert launch == {
        "session": launch["session"],
        "subject": _BOB,
        "project": "demo",
        "client_id": "hw-proj-demo",
        "redirect_uri": redirect_uri,
        "launch_context": context.model_dump(mode="json"),
        "namespace": f"hw-s-{launch['session']}",
        "namespace_uid": launch["namespace_uid"],
        "kube_system_uid": "uid-0",
        "gateway_service_account": f"hw-gw-{launch['session']}",
        "openshell_endpoint": f"https://openshell.hw-s-{launch['session']}.svc.cluster.local:8080",
    }
    assert context == PlatformLaunchContext(
        project_context_id=str(launch["session"]),
        platform_id="reference-platform",
        project_id="demo",
        billing_attribution_ref="grant-demo",
        authority_ref="reference-platform",
        policy_ref="policy-1",
        issued_at=_NOW,
    )
    assert platform.keycloak.by_client_id("hw-proj-demo")["redirectUris"] == [redirect_uri]


def test_gateway_secret_holds_the_launch_and_the_credentials_and_the_reply_holds_none(
    platform: Platform,
) -> None:
    launch = platform.launch(_BOB, "demo")

    secret = platform.kubernetes.kind("Secret", "heartwood-sessions")[f"hw-gw-{launch['session']}"]
    files = {name: base64.b64decode(value).decode() for name, value in secret["data"].items()}
    assert json.loads(files.pop("launch.json")) == launch
    assert files == {
        "keycloak-client-secret": "secret-of-uuid-1",
        "openshell-ca.crt": f"ca.crt of hw-s-{launch['session']}",
        "openshell-tls.crt": f"tls.crt of hw-s-{launch['session']}",
        "openshell-tls.key": f"tls.key of hw-s-{launch['session']}",
        "platform-ca.crt": "-----BEGIN CERTIFICATE-----\n",
        "actor-key": "synthetic-actor-key-0123456789abcdef",
    }
    files.pop("platform-ca.crt")
    for credential in files.values():
        assert credential not in json.dumps(launch)


def test_relaunch_reuses_the_session_and_researchers_share_the_project_client(
    platform: Platform,
) -> None:
    first = platform.launch(_BOB, "demo")

    assert platform.launch(_BOB, "demo")["session"] == first["session"]
    assert platform.launch(_ALICE, "demo")["session"] != first["session"]
    assert len(platform.keycloak.clients) == 1
    assert len(platform.keycloak.by_client_id("hw-proj-demo")["redirectUris"]) == 2


@pytest.mark.parametrize(
    ("subject", "project", "status"),
    [
        (_BOB, "other", 403),
        (_BOB, "missing", 403),
        ("unknown", "demo", 403),
        (_BOB, "Demo", 422),
        ("bob@example.test", "demo", 422),
        ("x" * 65, "demo", 422),
    ],
)
def test_launch_outside_the_registry_is_refused_and_changes_nothing(
    platform: Platform, subject: str, project: str, status: int
) -> None:
    response = platform.client.put(
        f"/v1/researchers/{subject}/projects/{project}/launch", headers=_HUB
    )

    assert response.status_code == status
    assert platform.keycloak.clients == {}
    assert platform.registry.session(subject, project) is None
    assert platform.kubernetes.calls == []


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer other-token"},
        {"Authorization": "Basic hub-token"},
        {"Authorization": "Bearer"},
    ],
)
def test_service_calls_need_an_accepted_service_account_token(
    platform: Platform, headers: dict[str, str]
) -> None:
    launch = f"/v1/researchers/{_BOB}/projects/demo/launch"

    assert (
        platform.client.get(f"/v1/researchers/{_BOB}/projects", headers=headers).status_code == 401
    )
    assert platform.client.put(launch, headers=headers).status_code == 401
    assert platform.client.delete(launch, headers=headers).status_code == 401
    assert platform.client.get("/v1/launch-context", headers=headers).status_code == 401
    assert platform.keycloak.clients == {}


def test_researcher_token_does_not_open_the_service_interface(platform: Platform) -> None:
    platform.launch(_BOB, "demo")
    headers = platform.researcher(_BOB, "demo")

    launch = f"/v1/researchers/{_ALICE}/projects/demo/launch"
    assert platform.client.put(launch, headers=headers).status_code == 401
    assert (
        platform.client.get(f"/v1/researchers/{_BOB}/projects", headers=headers).status_code == 401
    )
    assert platform.client.get("/v1/launch-context", headers=_HUB).status_code == 401


def test_launch_context_follows_the_token_and_the_registry(platform: Platform) -> None:
    platform.launch(_BOB, "demo")
    platform.launch(_ALICE, "other")

    def status(subject: str, project: str) -> int:
        headers = platform.researcher(subject, project)
        response = platform.client.get("/v1/launch-context", headers=headers)
        return int(response.status_code)

    assert status(_BOB, "demo") == 200
    assert status(_ALICE, "other") == 200
    # A member without a session, a session's project without membership, and a
    # token of another project client.
    assert status(_ALICE, "demo") == 404
    assert status(_BOB, "other") == 403
    assert status("unknown", "demo") == 403

    platform.registry.synchronize([Project("demo", "grant-demo", "policy-1", frozenset({_ALICE}))])
    assert status(_BOB, "demo") == 403


def test_deletion_removes_the_session_and_its_redirect_uri(platform: Platform) -> None:
    first = platform.launch(_BOB, "demo")
    kept = platform.launch(_ALICE, "demo")
    launch = f"/v1/researchers/{_BOB}/projects/demo/launch"

    assert platform.client.delete(launch, headers=_HUB).status_code == 204
    assert platform.client.delete(launch, headers=_HUB).status_code == 204

    assert platform.keycloak.by_client_id("hw-proj-demo")["redirectUris"] == [kept["redirect_uri"]]
    headers = platform.researcher(_BOB, "demo")
    assert platform.client.get("/v1/launch-context", headers=headers).status_code == 404
    assert f"/api/v1/namespaces/hw-s-{first['session']}" not in platform.kubernetes.objects
    assert f"/api/v1/namespaces/hw-s-{kept['session']}" in platform.kubernetes.objects
    assert platform.launch(_BOB, "demo")["session"] != first["session"]


def test_keycloak_failure_is_reported_as_an_unavailable_component(platform: Platform) -> None:
    platform.keycloak.secret = "rotated"

    response = platform.client.put(f"/v1/researchers/{_BOB}/projects/demo/launch", headers=_HUB)

    assert response.status_code == 502
    assert response.json() == {"error": "a platform component is unavailable"}


def test_settings_refuse_duplicate_projects_and_other_template_fields(tmp_path: Path) -> None:
    project = {"id": "demo", "billing_attribution_ref": "grant", "policy_ref": "policy"}
    keycloak = {
        "url": "https://keycloak:8443",
        "ca_file": "/nonexistent/ca.crt",
        "issuer": ISSUER,
        "realm": "heartwood",
        "client_id": "heartwood-control",
        "client_secret_file": "/nonexistent",
    }
    settings = {
        "platform_id": "reference-platform",
        "database": str(tmp_path / "control.sqlite3"),
        "audience": AUDIENCE,
        "service_callers": [HUB],
        "redirect_uri_template": _TEMPLATE,
        "keycloak": keycloak,
        "tls": {
            "certificate_file": "/etc/heartwood-control/tls/tls.crt",
            "key_file": "/etc/heartwood-control/tls/tls.key",
        },
        "hosted": HOSTED,
        "sessions": {
            "control_namespace": "heartwood-platform",
            "control_account": "heartwood-control",
            "gateway_namespace": "heartwood-sessions",
            "openshell_objects_file": str(_OBJECTS),
            "openshell_gateway_image": "registry.example.test/gateway@sha256:" + "1" * 64,
            "api_server": [{"cidr": "10.96.0.1/32", "port": 443}],
            "quota": {"pods": "20"},
            "container_defaults": {"cpu": "500m"},
            "container_default_requests": {"cpu": "100m"},
            "platform_ca_file": "/etc/heartwood-control/platform-ca/ca.crt",
            "actor_key_file": "/etc/heartwood-control/actor-key/actor-key",
        },
        "projects": [project],
        "synthetic_prices": SYNTHETIC_PRICES,
    }
    assert Settings.model_validate_json(json.dumps(settings)).projects[0].id == "demo"

    for change in (
        {"projects": [project, project]},
        {"redirect_uri_template": "https://hub.example.test/{0}/{subject}/{project}"},
        {"platform_id": "two words"},
        # Keycloak is reached only over HTTPS.
        {"keycloak": {**keycloak, "url": "http://keycloak:8080"}},
        {"synthetic_prices": {**SYNTHETIC_PRICES, "runtime_profiles": "none"}},
        # The service serves only HTTPS, so its certificate and key are required.
        {"tls": {"certificate_file": "/etc/heartwood-control/tls/tls.crt"}},
    ):
        with pytest.raises(ValueError, match="validation error"):
            Settings.model_validate_json(json.dumps({**settings, **change}))


def test_deletion_during_a_launch_waits_and_then_removes_everything(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "control.sqlite3")
    registry.synchronize([Project("demo", "grant-demo", "policy-1", frozenset({_BOB}))])
    events: list[str] = []

    class SlowSessions:
        """A launch during which a deletion of the same session arrives."""

        def provision(self, session: str) -> ProvisionedSession:
            self.deletion = threading.Thread(target=launches.delete, args=(_BOB, "demo"))
            self.deletion.start()
            self.deletion.join(timeout=0.3)
            events.append("provisioned")
            return ProvisionedSession(
                namespace=f"hw-s-{session}",
                namespace_uid="uid-1",
                kube_system_uid="uid-0",
                gateway_account=f"hw-gw-{session}",
                openshell_endpoint="https://openshell.example.test:8080",
                openshell_client={},
            )

        def write_gateway_secret(self, session: str, files: dict[str, str]) -> None:
            del session, files
            events.append("secret written")

        def remove(self, session: str) -> None:
            del session
            events.append("removed")

    sessions = SlowSessions()
    keycloak = FakeKeycloak()
    clients = Keycloak(keycloak.client(), "heartwood", "heartwood-control", lambda: keycloak.secret)
    launches = LaunchService(registry, clients, sessions, "reference-platform", _TEMPLATE)

    launches.prepare(_BOB, "demo")
    sessions.deletion.join()

    assert events == ["provisioned", "secret written", "removed"]
    assert registry.session(_BOB, "demo") is None
    assert keycloak.by_client_id("hw-proj-demo")["redirectUris"] == []


_OPERATOR = {"Authorization": "Bearer operator-token"}


def _gateway_secret(platform: Platform, session: object) -> str:
    secret = platform.kubernetes.kind("Secret", "heartwood-sessions")[f"hw-gw-{session}"]
    return base64.b64decode(secret["data"]["keycloak-client-secret"]).decode()


def test_an_operator_rotates_a_project_client_secret_into_every_gateway_secret(
    platform: Platform,
) -> None:
    alice, bob = platform.launch(_ALICE, "demo"), platform.launch(_BOB, "demo")
    other = platform.launch(_ALICE, "other")
    before = _gateway_secret(platform, alice["session"])

    response = platform.client.post("/v1/projects/demo/client-secret", headers=_OPERATOR)

    assert response.status_code == 200, response.text
    assert set(response.json()["sessions"]) == {alice["session"], bob["session"]}
    assert response.json()["failed"] == []
    after = _gateway_secret(platform, alice["session"])
    assert after != before
    assert _gateway_secret(platform, bob["session"]) == after
    # Another project's client and its gateways keep their secret.
    assert _gateway_secret(platform, other["session"]) == "secret-of-uuid-2"


def test_only_operators_rotate_and_only_existing_projects(platform: Platform) -> None:
    for headers in ({}, _HUB, {"Authorization": "Bearer unknown"}):
        response = platform.client.post("/v1/projects/demo/client-secret", headers=headers)
        assert response.status_code == 401
    response = platform.client.post("/v1/projects/missing/client-secret", headers=_OPERATOR)
    assert response.status_code == 404
    response = platform.client.post("/v1/projects/Not_A_Project/client-secret", headers=_OPERATOR)
    assert response.status_code == 422
    assert platform.keycloak.rotations == {}


def test_a_project_without_a_client_has_nothing_to_rotate(platform: Platform) -> None:
    response = platform.client.post("/v1/projects/demo/client-secret", headers=_OPERATOR)

    assert response.status_code == 409
    assert response.json() == {"error": "the project has no client yet"}
    assert platform.keycloak.rotations == {}


def test_a_former_members_session_keeps_its_secret(platform: Platform) -> None:
    alice, bob = platform.launch(_ALICE, "demo"), platform.launch(_BOB, "demo")
    before = _gateway_secret(platform, bob["session"])
    platform.registry.synchronize(
        [
            Project("demo", "grant-demo", "policy-1", frozenset({_ALICE})),
            Project("other", "grant-other", "policy-1", frozenset({_ALICE})),
        ]
    )

    response = platform.client.post("/v1/projects/demo/client-secret", headers=_OPERATOR)

    assert response.status_code == 200
    assert response.json()["sessions"] == [alice["session"]]
    assert _gateway_secret(platform, bob["session"]) == before


def test_a_session_deleted_during_a_rotation_is_not_created_again(platform: Platform) -> None:
    alice = platform.launch(_ALICE, "demo")
    original = platform.registry.sessions_of

    def deleted_after_listing(project: str) -> list[Session]:
        listed = original(project)
        platform.client.delete(f"/v1/researchers/{_ALICE}/projects/demo/launch", headers=_HUB)
        return listed

    platform.registry.sessions_of = deleted_after_listing  # type: ignore[method-assign]
    response = platform.client.post("/v1/projects/demo/client-secret", headers=_OPERATOR)

    assert response.status_code == 200
    assert response.json()["sessions"] == []
    assert platform.registry.session(_ALICE, "demo") is None
    assert f"hw-gw-{alice['session']}" not in platform.kubernetes.kind(
        "Secret", "heartwood-sessions"
    )


def test_a_session_that_cannot_be_prepared_is_reported_and_the_others_are_rewritten(
    platform: Platform,
) -> None:
    alice, bob = platform.launch(_ALICE, "demo"), platform.launch(_BOB, "demo")
    platform.kubernetes.failing.add(("GET", f"/api/v1/namespaces/hw-s-{alice['session']}"))

    response = platform.client.post("/v1/projects/demo/client-secret", headers=_OPERATOR)

    assert response.status_code == 200
    assert response.json() == {"sessions": [bob["session"]], "failed": [alice["session"]]}


def test_the_actor_key_must_be_present_and_long_enough(tmp_path: Path) -> None:
    key = tmp_path / "actor-key"
    key.write_text("synthetic-actor-key-0123456789abcdef\n", encoding="utf-8")
    assert _actor_key(key) == "synthetic-actor-key-0123456789abcdef"

    key.write_bytes(b"synthetic-actor-key-0123456789ab\xff")
    with pytest.raises(ValueError, match="not UTF-8") as error:
        _actor_key(key)
    assert error.value.__cause__ is None
    key.write_text("k" * 4097, encoding="utf-8")
    with pytest.raises(ValueError, match="exceeds 4096 bytes"):
        _actor_key(key)
    key.write_text("short\n", encoding="utf-8")
    with pytest.raises(ValueError, match="at least 32 bytes"):
        _actor_key(key)
    key.unlink()
    with pytest.raises(FileNotFoundError):
        _actor_key(key)
