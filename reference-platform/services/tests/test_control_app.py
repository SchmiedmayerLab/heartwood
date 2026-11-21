# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import base64
import json
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from fakes import (
    AUDIENCE,
    HUB,
    ISSUER,
    JOBS,
    OPERATOR,
    SYNTHETIC_PRICES,
    FakeKeycloak,
    FakeKubernetes,
    SigningKey,
    TokenReviews,
)
from heartwood_platform.common.kubernetes import Kubernetes
from heartwood_platform.common.tokens import ResearcherTokens, ServiceTokens
from heartwood_platform.control.__main__ import Settings
from heartwood_platform.control.app import create_app
from heartwood_platform.control.budgets import Allocation, Ledger
from heartwood_platform.control.gateway_certificates import GatewayCertificates
from heartwood_platform.control.inference import SessionRoute
from heartwood_platform.control.keycloak import Keycloak
from heartwood_platform.control.launch import LaunchService
from heartwood_platform.control.prices import PriceSheet
from heartwood_platform.control.project_data import MountedDataset, ProjectData, ProjectDataSettings
from heartwood_platform.control.registry import NotAMemberError, Project, Registry, Session
from heartwood_platform.control.researchers import read_key, researcher_id
from heartwood_platform.control.sessions import ProvisionedSession, Sessions, SessionSettings
from httpx2 import Response
from starlette.testclient import TestClient
from test_gateway_certificates import issuer_directory, synthetic_authority
from test_hosted import HOSTED

from heartwood.schemas import PlatformLaunchContext

_HUB = {"Authorization": "Bearer hub-token"}
_DATA = "system:serviceaccount:heartwood-platform:heartwood-data"
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
_TEMPLATE = "https://hub.example.test/user/{researcher}/{project}/oauth_callback"
_RESEARCHER_KEY = b"synthetic-researcher-id-key-0123456789"
_ACTOR_KEY = secrets.token_urlsafe(32)


def _id(subject: str) -> str:
    """Return the researcher identifier that names a subject in paths and URLs."""
    return researcher_id(ISSUER, subject, _RESEARCHER_KEY)


class FakeInference:
    """The model route: records the keys it gives and what each session incurred."""

    headroom = Decimal("0.25")

    def __init__(self) -> None:
        self.incurred: dict[str, Decimal] = {}
        self.settlements: list[str] = []
        self.budgets: dict[str, Decimal] = {}
        self.rotated: list[str] = []
        self.forgotten: list[str] = []

    def prepare(
        self, session: str, project: str, reservation: Decimal, route: SessionRoute
    ) -> dict[str, str]:
        del project
        assert route.endpoint.startswith("https://openshell.")
        self.budgets[session] = reservation - self.headroom
        return {"model-discovery-key": f"discovery-key-of-{session}"}

    def rotate(self, session: str, project: str, route: SessionRoute) -> None:
        del project, route
        self.rotated.append(session)

    def settle(self, session: str) -> Decimal:
        self.settlements.append(session)
        return self.incurred.get(session, Decimal(0))

    def settled(self, session: str) -> bool:
        return session in self.settlements and session not in self.forgotten

    def forget(self, session: str) -> None:
        self.forgotten.append(session)


class Platform:
    def __init__(
        self,
        directory: Path,
        prices: PriceSheet | None = None,
        *,
        grants: dict[str, MountedDataset] | None = None,
    ) -> None:
        self.key = SigningKey()
        self.keycloak = FakeKeycloak()
        self.reviews = TokenReviews()
        self.reviews.tokens["data-token"] = {
            "authenticated": True,
            "audiences": [AUDIENCE],
            "user": {"username": _DATA},
        }
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
                actor_key=_ACTOR_KEY,
            ),
            self.registry.session_ids,
        )
        prices = prices or PriceSheet.model_validate_json(json.dumps(SYNTHETIC_PRICES))
        self.ledger = Ledger(directory / "control.sqlite3", prices.denomination, clock=lambda: _NOW)
        self.ledger.synchronize(
            [
                Allocation("demo", "inference", Decimal(5)),
                Allocation("demo", "compute", Decimal(50)),
                Allocation("other", "inference", Decimal("1.5")),
            ]
        )
        self.inference = FakeInference()
        project_data = None
        if grants:
            settings = ProjectDataSettings(
                data_url="https://data.test",
                token_file=directory / "absent",
                copy_image="test@sha256:" + "0" * 64,
                storage_class="copy",
                workspace="research",
                mount_path="/data",
                object_store_namespace="platform",
                object_store_labels={},
                object_store_port=7070,
                mounted_datasets=grants,
            )
            self.mount_settings = settings
            project_data = ProjectData(
                Kubernetes(self.kubernetes.client()),
                settings,
                httpx.Client(
                    transport=httpx.MockTransport(
                        lambda _: pytest.fail("mounted launch requested a copy")
                    )
                ),
            )
        self.launches = launches = LaunchService(
            self.registry,
            keycloak,
            sessions,
            "reference-platform",
            _TEMPLATE,
            _id,
            clock=lambda: _NOW,
            ledger=self.ledger,
            session_reservation=Decimal(1),
            inference=self.inference,
            project_data=project_data,
            certificates=GatewayCertificates(
                issuer_directory(directory / "issuer", synthetic_authority(), _NOW),
                "hw-gateways",
                "heartwood-sessions",
                clock=lambda: _NOW,
            ),
        )
        self.client = TestClient(
            create_app(
                launches,
                ResearcherTokens(ISSUER, AUDIENCE, self.key.key_set),
                ServiceTokens(self.reviews.review, AUDIENCE, frozenset({HUB})),
                prices,
                self.ledger,
                clock=lambda: _NOW,
                operators=ServiceTokens(self.reviews.review, AUDIENCE, frozenset({OPERATOR})),
                contexts=ServiceTokens(self.reviews.review, AUDIENCE, frozenset({_DATA, JOBS})),
                jobs=ServiceTokens(self.reviews.review, AUDIENCE, frozenset({JOBS})),
            )
        )

    def launch(self, subject: str, project: str) -> dict[str, Any]:
        response = self.client.put(
            f"/v1/researchers/{_id(subject)}/projects/{project}/launch", headers=_HUB
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
        return platform.client.get(f"/v1/researchers/{_id(subject)}/projects", headers=_HUB).json()

    assert projects(_ALICE) == {"projects": ["demo", "other"]}
    assert projects(_BOB) == {"projects": ["demo"]}
    assert projects("unknown") == {"projects": []}


def test_launch_returns_the_registry_binding_and_prepares_the_project_client(
    platform: Platform,
) -> None:
    launch = platform.launch(_BOB, "demo")

    redirect_uri = f"https://hub.example.test/user/{_id(_BOB)}/demo/oauth_callback"
    context = PlatformLaunchContext.model_validate_json(
        platform.client.get("/v1/launch-context", headers=platform.researcher(_BOB, "demo")).content
    )
    assert launch == {
        "session": launch["session"],
        "researcher": _id(_BOB),
        "project": "demo",
        "client_id": "hw-proj-demo",
        "redirect_uri": redirect_uri,
        "launch_context": context.model_dump(mode="json"),
        "namespace": f"hw-s-{launch['session']}",
        "namespace_uid": launch["namespace_uid"],
        "kube_system_uid": "uid-0",
        "gateway_service_account": f"hw-gw-{launch['session']}",
        "openshell_endpoint": f"https://openshell.hw-s-{launch['session']}.svc.cluster.local:8080",
        "project_data": None,
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
    certificate, key = files.pop("gateway-tls.crt"), files.pop("gateway-tls.key")
    assert certificate.count("-----BEGIN CERTIFICATE-----") == 2
    assert key.startswith("-----BEGIN PRIVATE KEY-----")
    assert key not in json.dumps(launch)
    assert files == {
        "model-discovery-key": f"discovery-key-of-{launch['session']}",
        "keycloak-client-secret": "secret-of-uuid-1",
        "openshell-ca.crt": f"ca.crt of hw-s-{launch['session']}",
        "openshell-tls.crt": f"tls.crt of hw-s-{launch['session']}",
        "openshell-tls.key": f"tls.key of hw-s-{launch['session']}",
        "platform-ca.crt": "-----BEGIN CERTIFICATE-----\n",
        "actor-key": _ACTOR_KEY,
    }
    files.pop("platform-ca.crt")
    for credential in files.values():
        assert credential not in json.dumps(launch)


def test_relaunch_reuses_the_session_and_researchers_share_the_project_client(
    platform: Platform,
) -> None:
    first = platform.launch(_BOB, "demo")

    secret = platform.kubernetes.kind("Secret", "heartwood-sessions")[f"hw-gw-{first['session']}"]
    key = secret["data"]["gateway-tls.key"]
    assert platform.launch(_BOB, "demo")["session"] == first["session"]
    # A relaunch writes a new key and certificate of the gateway.
    secret = platform.kubernetes.kind("Secret", "heartwood-sessions")[f"hw-gw-{first['session']}"]
    assert secret["data"]["gateway-tls.key"] != key
    assert platform.launch(_ALICE, "demo")["session"] != first["session"]
    assert len(platform.keycloak.clients) == 1
    assert len(platform.keycloak.by_client_id("hw-proj-demo")["redirectUris"]) == 2


@pytest.mark.parametrize(
    ("researcher", "project", "status"),
    [
        (_id(_BOB), "other", 403),
        (_id(_BOB), "missing", 403),
        (_id("unknown"), "demo", 403),
        # An identifier under another key names nobody.
        (researcher_id(ISSUER, _BOB, b"another-synthetic-key-0123456789ab"), "demo", 403),
        (_id(_BOB), "Demo", 422),
        # Paths name researchers by identifier, never by subject.
        (_BOB, "demo", 422),
        ("bob@example.test", "demo", 422),
        (_id(_BOB).upper(), "demo", 422),
        (_id(_BOB)[:-1], "demo", 422),
    ],
)
def test_launch_outside_the_registry_is_refused_and_changes_nothing(
    platform: Platform, researcher: str, project: str, status: int
) -> None:
    response = platform.client.put(
        f"/v1/researchers/{researcher}/projects/{project}/launch", headers=_HUB
    )

    assert response.status_code == status
    assert platform.keycloak.clients == {}
    assert platform.registry.session_ids() == []
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
    launch = f"/v1/researchers/{_id(_BOB)}/projects/demo/launch"

    assert (
        platform.client.get(f"/v1/researchers/{_id(_BOB)}/projects", headers=headers).status_code
        == 401
    )
    assert platform.client.put(launch, headers=headers).status_code == 401
    assert platform.client.delete(launch, headers=headers).status_code == 401
    assert platform.client.get("/v1/launch-context", headers=headers).status_code == 401
    assert platform.keycloak.clients == {}


def test_researcher_token_does_not_open_the_service_interface(platform: Platform) -> None:
    platform.launch(_BOB, "demo")
    headers = platform.researcher(_BOB, "demo")

    launch = f"/v1/researchers/{_id(_ALICE)}/projects/demo/launch"
    assert platform.client.put(launch, headers=headers).status_code == 401
    assert (
        platform.client.get(f"/v1/researchers/{_id(_BOB)}/projects", headers=headers).status_code
        == 401
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
    launch = f"/v1/researchers/{_id(_BOB)}/projects/demo/launch"

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

    response = platform.client.put(
        f"/v1/researchers/{_id(_BOB)}/projects/demo/launch", headers=_HUB
    )

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
    inference: dict[str, Any] = {
        "session_reservation": "1",
        "litellm_url": "https://litellm.heartwood-platform.svc.cluster.local:4000",
        "master_key_file": "/etc/heartwood-control/litellm/master-key",
        "models": [
            {"name": "synthetic-coder", "max_input_tokens": 32768, "max_output_tokens": 4096}
        ],
        "openshell_cli": "/usr/local/bin/openshell",
    }
    settings = {
        "platform_id": "reference-platform",
        "database": str(tmp_path / "control.sqlite3"),
        "audience": AUDIENCE,
        "service_callers": [HUB],
        "redirect_uri_template": _TEMPLATE,
        "researcher_id_key_file": "/etc/heartwood-control/researcher-ids/key",
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
        "gateway_tls": {
            "issuer_directory": "/var/run/heartwood-control/gateway-issuer",
            "service": "hw-gateways",
        },
        "projects": [project],
        "synthetic_prices": SYNTHETIC_PRICES,
        "inference": inference,
    }
    assert Settings.model_validate_json(json.dumps(settings)).projects[0].id == "demo"
    members = {**project, "members": ["auth0|64ab", "synthetic@example.test", _BOB]}
    loaded = Settings.model_validate_json(json.dumps({**settings, "projects": [members]}))
    assert loaded.projects[0].members == frozenset(members["members"])

    for change in (
        {"projects": [project, project]},
        {"projects": [{**project, "members": [" padded"]}]},
        {"projects": [{**project, "members": ["zero\u200bwidth"]}]},
        {"projects": [{**project, "members": [""]}]},
        {"redirect_uri_template": "https://hub.example.test/user/{subject}/{project}"},
        {"redirect_uri_template": "https://hub.example.test/{0}/{researcher}/{project}"},
        {"platform_id": "two words"},
        # Keycloak is reached only over HTTPS.
        {"keycloak": {**keycloak, "url": "http://keycloak:8080"}},
        {"synthetic_prices": {**SYNTHETIC_PRICES, "runtime_profiles": "none"}},
        # The service serves only HTTPS, so its certificate and key are required.
        {"tls": {"certificate_file": "/etc/heartwood-control/tls/tls.crt"}},
        # A session reserves more than one request can cost, and budgets are amounts of
        # known kinds.
        {"inference": {**inference, "session_reservation": "0"}},
        {"inference": {**inference, "session_reservation": "0.0225"}},
        # Every model of the route has token prices, and the worker uses one of them.
        {
            "inference": {
                **inference,
                "models": [{"name": "unpriced", "max_input_tokens": 1, "max_output_tokens": 1}],
            }
        },
        {"hosted": {**HOSTED, "model_route": {**HOSTED["model_route"], "model": "other"}}},
        {"projects": [{**project, "budgets": {"inference": "-1"}}]},
        {"projects": [{**project, "budgets": {"storage": "1"}}]},
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
    ledger = Ledger(
        tmp_path / "control.sqlite3",
        PriceSheet.model_validate_json(json.dumps(SYNTHETIC_PRICES)).denomination,
    )
    ledger.synchronize([Allocation("demo", "inference", Decimal(5))])
    launches = LaunchService(
        registry,
        clients,
        sessions,
        "reference-platform",
        _TEMPLATE,
        _id,
        ledger=ledger,
        session_reservation=Decimal(1),
    )

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
        platform.client.delete(f"/v1/researchers/{_id(_ALICE)}/projects/demo/launch", headers=_HUB)
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
    assert read_key(key) == "synthetic-actor-key-0123456789abcdef"

    key.write_bytes(b"synthetic-actor-key-0123456789ab\xff")
    with pytest.raises(ValueError, match="not UTF-8") as error:
        read_key(key)
    assert error.value.__cause__ is None
    key.write_text("k" * 4097, encoding="utf-8")
    with pytest.raises(ValueError, match="exceeds 4096 bytes"):
        read_key(key)
    key.write_text("short\n", encoding="utf-8")
    with pytest.raises(ValueError, match="at least 32 bytes"):
        read_key(key)
    key.unlink()
    with pytest.raises(FileNotFoundError):
        read_key(key)


@pytest.mark.parametrize(
    "subject",
    ["auth0|64ab12cd34ef56ab78cd90ef", "109876543210987654321", "synthetic@example.test"],
)
def test_any_subject_launches_by_its_identifier_and_no_url_names_it(
    platform: Platform, subject: str
) -> None:
    platform.registry.synchronize(
        [Project("demo", "grant-demo", "policy-1", frozenset({_ALICE, _BOB, subject}))]
    )

    projects = platform.client.get(f"/v1/researchers/{_id(subject)}/projects", headers=_HUB)
    launch = platform.launch(subject, "demo")

    assert projects.json() == {"projects": ["demo"]}
    assert launch["researcher"] == _id(subject)
    assert (
        launch["redirect_uri"]
        == f"https://hub.example.test/user/{_id(subject)}/demo/oauth_callback"
    )
    assert platform.keycloak.by_client_id("hw-proj-demo")["redirectUris"] == [
        launch["redirect_uri"]
    ]
    assert subject not in json.dumps(launch)
    secret = platform.kubernetes.kind("Secret", "heartwood-sessions")[f"hw-gw-{launch['session']}"]
    assert subject not in base64.b64decode(secret["data"]["launch.json"]).decode()


def test_a_removed_member_removes_its_session_by_its_identifier(platform: Platform) -> None:
    launch = platform.launch(_BOB, "demo")
    platform.registry.synchronize([Project("demo", "grant-demo", "policy-1", frozenset({_ALICE}))])

    assert platform.registry.subjects() == sorted({_ALICE, _BOB})
    response = platform.client.delete(
        f"/v1/researchers/{_id(_BOB)}/projects/demo/launch", headers=_HUB
    )

    assert response.status_code == 204
    assert launch["session"] not in platform.registry.session_ids()
    assert platform.registry.subjects() == [_ALICE]


def test_removing_the_launch_of_an_identifier_that_names_nobody_succeeds(
    platform: Platform,
) -> None:
    response = platform.client.delete(
        f"/v1/researchers/{_id('unknown')}/projects/demo/launch", headers=_HUB
    )

    assert response.status_code == 204
    assert platform.kubernetes.calls == []
    assert platform.keycloak.clients == {}


def test_an_identifier_that_two_subjects_share_names_neither(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "control.sqlite3")
    registry.synchronize([Project("demo", "grant-demo", "policy-1", frozenset({_ALICE, _BOB}))])
    keycloak = FakeKeycloak()
    clients = Keycloak(keycloak.client(), "heartwood", "heartwood-control", lambda: keycloak.secret)
    shared = "r" + "0" * 32
    launches = LaunchService(
        registry,
        clients,
        cast(Any, None),
        "reference-platform",
        _TEMPLATE,
        lambda _: shared,
        ledger=Ledger(
            tmp_path / "control.sqlite3",
            PriceSheet.model_validate_json(json.dumps(SYNTHETIC_PRICES)).denomination,
        ),
        session_reservation=Decimal(1),
    )

    with pytest.raises(NotAMemberError):
        launches.subject_of(shared)


def test_the_data_and_job_services_read_the_launch_context_of_a_members_session(
    platform: Platform,
) -> None:
    launch = platform.launch(_BOB, "demo")

    def read(subject: str, project: str, headers: dict[str, str]) -> Any:  # noqa: ANN401
        return platform.client.post(
            f"/v1/projects/{project}/launch-context", json={"subject": subject}, headers=headers
        )

    for token in ("data-token", "jobs-token"):
        service = {"Authorization": f"Bearer {token}"}
        found = read(_BOB, "demo", service)
        assert found.status_code == 200
        # Only the identity of the launch context, without its billing binding.
        assert found.json() == {"project_context_id": launch["session"]}
        assert read(_ALICE, "demo", service).status_code == 404
        assert read(_BOB, "other", service).status_code == 403
    # The spawner, operators and researchers do not read launch contexts this way.
    for headers in (
        _HUB,
        {"Authorization": "Bearer operator-token"},
        {"Authorization": "Bearer unknown"},
        platform.researcher(_BOB, "demo"),
    ):
        assert read(_BOB, "demo", headers).status_code == 401
    # The subject is a valid subject in the body, and the body stays small.
    for body in ({"subject": " padded"}, {"subject": _BOB, "project": "demo"}, {}):
        response = platform.client.post(
            "/v1/projects/demo/launch-context",
            json=body,
            headers={"Authorization": "Bearer data-token"},
        )
        assert response.status_code == 422
    large = platform.client.post(
        "/v1/projects/demo/launch-context",
        content=b"x" * 5000,
        headers={"Authorization": "Bearer data-token"},
    )
    assert large.status_code == 413
    assert (
        platform.client.get(
            "/v1/projects/demo/launch-context", headers={"Authorization": "Bearer data-token"}
        ).status_code
        == 405
    )


@pytest.mark.parametrize("token", ["data-token", "operator-token", "hub-token"])
def test_only_jobs_can_access_job_budgets(platform: Platform, token: str) -> None:
    body = {"runtime_profile_ref": "synthetic-batch-small", "seconds": 10}
    headers = {"Authorization": f"Bearer {token}"}
    path = "/v1/projects/demo/job-reservations/op"
    for method, endpoint in (
        ("POST", "/v1/projects/demo/job-quotes"),
        ("PUT", path),
        ("POST", path + "/settlement"),
    ):
        assert (
            platform.client.request(method, endpoint, json=body, headers=headers).status_code == 401
        )
    assert platform.ledger.balances("demo")[0].revision == 1


def test_job_accounting_replays_at_the_original_rate(platform: Platform) -> None:
    headers = {"Authorization": "Bearer jobs-token"}
    path = "/v1/projects/demo/job-reservations/op"
    body = {"runtime_profile_ref": "synthetic-batch-small", "seconds": 60}

    held = platform.client.put(path, json=body, headers=headers)
    assert held.status_code == 200
    assert held.json()["amount"] == "0.018"
    replay = {**body, "runtime_profile_ref": "removed-profile"}
    assert platform.client.put(path, json=replay, headers=headers).json() == held.json()
    assert platform.ledger.balances("demo")[0].revision == 2
    settlement = path + "/settlement"
    # The executor can run past the maximum admissible limit.
    settled = platform.client.post(settlement, json={**replay, "seconds": 604801}, headers=headers)
    assert settled.status_code == 200
    assert settled.json()["incurred"] == "181.4403"
    assert platform.client.post(settlement, json=body, headers=headers).json() == settled.json()
    assert platform.client.put(path, json=body, headers=headers).json()["error"] == "settled"
    balance = platform.ledger.balances("demo")[0]
    assert (balance.reserved, balance.spent, balance.revision) == (
        Decimal(0),
        Decimal("181.4403"),
        3,
    )


def test_release_before_reserve_fences_an_operation_without_an_allocation(
    platform: Platform,
) -> None:
    path = "/v1/projects/other/job-reservations/op"
    headers = {"Authorization": "Bearer jobs-token"}
    body = {"runtime_profile_ref": "synthetic-batch-small", "seconds": 10}
    settled = platform.client.post(path + "/settlement", json=body, headers=headers)
    assert settled.json()["incurred"] == "0"
    platform.ledger.synchronize([Allocation("other", "compute", Decimal(1))])
    assert platform.client.put(path, json=body, headers=headers).json()["error"] == "settled"
    assert platform.ledger.balances("other")[0].available == 1


def test_competing_jobs_cannot_partially_reserve_the_last_funds(platform: Platform) -> None:
    platform.ledger.synchronize([Allocation("demo", "compute", Decimal("0.02"))])
    start = threading.Barrier(2)

    def reserve(operation: str) -> int:
        start.wait(timeout=5)
        response = platform.client.put(
            f"/v1/projects/demo/job-reservations/{operation}",
            json={"runtime_profile_ref": "synthetic-batch-small", "seconds": 60},
            headers={"Authorization": "Bearer jobs-token"},
        )
        return response.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(reserve, ("op-1", "op-2"))) == [200, 409]
    balance = platform.ledger.balances("demo")[0]
    assert balance.job_reservations == (1, Decimal("0.018"))
    assert balance.available == Decimal("0.002")


def test_restarted_ledger_keeps_its_denomination_and_original_job_rate(tmp_path: Path) -> None:
    headers = {"Authorization": "Bearer jobs-token"}
    path = "/v1/projects/demo/job-reservations/original"
    usage = {"runtime_profile_ref": "synthetic-batch-small", "seconds": 60}
    platform = Platform(tmp_path)
    held = platform.client.put(path, json=usage, headers=headers).json()
    assert held["amount"] == "0.018"
    assert held["denomination"] == {"kind": "currency", "unit": "USD"}
    config = json.loads(json.dumps(SYNTHETIC_PRICES))
    for sku in config["sku_prices"]:
        sku["PricingCurrency"] = "EUR"
    with pytest.raises(ValueError, match="Ledger denomination is currency USD"):
        Platform(tmp_path, PriceSheet.model_validate_json(json.dumps(config)))
    for sku in config["sku_prices"]:
        sku["PricingCurrency"] = "USD"
        if sku["SkuId"] == "synthetic-batch-small":
            sku["UnitPrice"] = "0.20"
    for profile in config["runtime_profiles"]:
        if profile["runtime_profile_ref"] == "synthetic-batch-small":
            profile["rates"][0]["quantity_per_pricing_unit"] = "10"
    platform = Platform(tmp_path, PriceSheet.model_validate_json(json.dumps(config)))
    assert platform.client.put(path, json=usage, headers=headers).json() == held
    billed = platform.client.post(
        path + "/settlement", json={**usage, "seconds": 10}, headers=headers
    ).json()
    assert billed["incurred"] == "0.003"
    assert billed["denomination"] == held["denomination"]
    assert platform.client.post(path + "/settlement", json=usage, headers=headers).json() == billed
    fresh = platform.client.put(path + "-new", json=usage, headers=headers).json()
    assert Decimal(fresh["amount"]) == Decimal("1.2")


def test_mounted_versions_are_scoped_to_members_and_revoked_with_the_session(
    tmp_path: Path,
) -> None:
    a = MountedDataset(id="cohort", version="a" * 64, size=2**40, objects=9)
    b = a.model_copy(update={"version": "b" * 64})
    platform = Platform(tmp_path, grants={"demo": a, "other": b})

    def request(subject: str, project: str, grant: MountedDataset) -> Response:
        return platform.client.put(
            f"/v1/researchers/{_id(subject)}/projects/{project}/launch",
            params={"dataset": grant.selection},
            headers=_HUB,
        )

    assert request(_BOB, "other", b).status_code == 403
    assert request(_BOB, "demo", b).status_code == 403
    assert platform.registry.session_ids() == []
    response = request(_BOB, "demo", a)
    assert response.status_code == 200, response.text
    first = response.json()
    assert first["project_data"]["content_sha256"] == a.version
    assert platform.launch(_BOB, "demo")["project_data"] == first["project_data"]
    second = platform.launch(_ALICE, "other")
    for launch, project, grant in [(first, "demo", a), (second, "other", b)]:
        ns = launch["namespace"]
        volume = platform.kubernetes.kind("PersistentVolume")[ns + "-dataset"]
        spec = volume["spec"]
        assert spec["claimRef"]["uid"] == launch["project_data"]["claim_uid"]
        assert spec["claimRef"]["namespace"] == ns
        assert spec["accessModes"] == ["ReadOnlyMany"]
        assert spec["csi"]["readOnly"] is True
        assert spec["csi"]["volumeAttributes"] == {
            "bucketName": f"hw-project-{project}",
            "authenticationSource": "driver",
        }
        assert f"prefix datasets/cohort/{grant.version}/" in spec["mountOptions"]
        assert volume["metadata"]["ownerReferences"][0]["uid"] == launch["namespace_uid"]
        claim = platform.kubernetes.kind("PersistentVolumeClaim", ns)["hw-mounted-data"]
        assert claim["spec"]["volumeName"] == volume["metadata"]["name"]
        assert claim["metadata"]["labels"]["openshell.ai/sandbox-attachable"] == "true"

    pv = platform.kubernetes.kind("PersistentVolume")[first["namespace"] + "-dataset"]
    pv["spec"]["claimRef"]["uid"] = "different-claim"
    assert request(_BOB, "demo", a).status_code == 503
    platform.registry.synchronize(
        [Project("other", "grant-other", "policy-1", frozenset({_ALICE}))]
    )
    platform.launches.reconcile_dataset_grants()
    assert platform.registry.session_ids() == [second["session"]]
    assert not platform.kubernetes.kind("PersistentVolumeClaim", first["namespace"])
    assert platform.kubernetes.kind("PersistentVolumeClaim", second["namespace"])


def test_mount_recovery_and_replacement_use_distinct_session_claims(tmp_path: Path) -> None:
    old = MountedDataset(id="sequence", version="1" * 64, size=2**40, objects=15)
    new = old.model_copy(update={"version": "2" * 64})
    platform = Platform(tmp_path, grants={"demo": old})
    path = f"/v1/researchers/{_id(_BOB)}/projects/demo/launch"
    failure = ("POST", "/api/v1/persistentvolumes")
    platform.kubernetes.failing.add(failure)
    assert platform.client.put(path, headers=_HUB).status_code == 502
    (session,) = platform.registry.session_ids()
    namespace = "hw-s-" + session
    claim = platform.kubernetes.kind("PersistentVolumeClaim", namespace)["hw-mounted-data"]
    assert "openshell.ai/sandbox-attachable" not in claim["metadata"]["labels"]
    platform.kubernetes.failing.remove(failure)
    launch = platform.launch(_BOB, "demo")
    assert launch["project_data"]["claim_uid"] == claim["metadata"]["uid"]
    # Startup reads the persisted old selection and the replacement operator settings.
    platform.mount_settings.mounted_datasets["demo"] = new
    platform.launches.reconcile_dataset_grants()
    assert not platform.kubernetes.kind("PersistentVolumeClaim", namespace)
    assert platform.registry.session_ids() == []
    assert (
        platform.client.put(path, params={"dataset": old.selection}, headers=_HUB).status_code
        == 403
    )
    replacement = platform.launch(_BOB, "demo")
    assert replacement["namespace"] != namespace
    assert replacement["project_data"]["claim_uid"] != launch["project_data"]["claim_uid"]
    assert replacement["project_data"]["content_sha256"] == new.version


def test_dataset_revocation_retires_namespace_before_binding_cleanup_and_retries(
    tmp_path: Path,
) -> None:
    old = MountedDataset(id="cohort", version="1" * 64, size=1024, objects=1)
    platform = Platform(tmp_path, grants={"demo": old})
    launch = platform.launch(_BOB, "demo")
    namespace = launch["namespace"]
    platform.mount_settings.mounted_datasets.pop("demo")
    binding = (
        "PATCH",
        "/apis/rbac.authorization.k8s.io/v1/clusterrolebindings/heartwood-openshell-node-reader",
    )
    platform.kubernetes.failing.add(binding)
    with pytest.raises(ExceptionGroup, match="retire all expired dataset sessions"):
        platform.launches.reconcile_dataset_grants()
    assert f"/api/v1/namespaces/{namespace}" not in platform.kubernetes.objects
    assert not platform.kubernetes.kind("PersistentVolumeClaim", namespace)
    assert Registry(tmp_path / "control.sqlite3").session_ids() == [launch["session"]]
    platform.kubernetes.failing.remove(binding)
    # Restoring the same grant during a failed retirement must not resurrect the session.
    platform.mount_settings.mounted_datasets["demo"] = old
    platform.launches.reconcile_dataset_grants()
    assert platform.registry.session_ids() == []
    assert platform.launch(_BOB, "demo")["namespace"] != namespace
