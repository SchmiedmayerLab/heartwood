# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fakes import ISSUER, SigningKey, TokenReviews
from heartwood_platform.common.signing import Credentials, presign
from heartwood_platform.common.tokens import ResearcherTokens, ServiceTokens
from heartwood_platform.data.access import MAX_SECONDS, ObjectAccess, access_seconds
from heartwood_platform.data.app import create_app
from heartwood_platform.data.catalog import Catalog, content_sha256
from heartwood_platform.data.copy import CopyError, Plan, copy
from heartwood_platform.data.seed import SeedObject, SeedSettings, seed
from pydantic import ValidationError
from starlette.testclient import TestClient

_AUDIENCE = "heartwood-data"
_CONTROL = "system:serviceaccount:heartwood-platform:heartwood-control"
_ALICE = "researcher-1"
_BOB = "researcher-2"
_NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
_CONTENT = {"participants.csv": b"id,age\n1,40\n", "README.md": b"# Synthetic cohort\n"}


def _object(name: str, content: bytes) -> dict[str, object]:
    return {
        "name": name,
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "mime_type": "text/csv" if name.endswith(".csv") else "text/markdown",
    }


def _catalog() -> Catalog:
    created = "2026-11-01T00:00:00+00:00"
    return Catalog.model_validate(
        {
            "projects": [
                {
                    "id": "demo",
                    "members": [_ALICE, _BOB],
                    "datasets": [
                        {
                            "id": "synthetic-cohort",
                            "name": "Synthetic cohort",
                            "description": "A synthetic cohort.",
                            "created": created,
                            "objects": [_object(n, c) for n, c in _CONTENT.items()],
                        }
                    ],
                },
                {
                    "id": "other",
                    "members": [_ALICE],
                    "datasets": [
                        {
                            "id": "other-panel",
                            "name": "Other panel",
                            "description": "Another project's data.",
                            "created": created,
                            "objects": [_object("panel.csv", b"x\n")],
                        }
                    ],
                },
            ]
        }
    )


class Service:
    def __init__(self) -> None:
        self.key = SigningKey()
        self.reviews = TokenReviews()
        self.reviews.tokens["control-token"] = {
            "authenticated": True,
            "audiences": [_AUDIENCE],
            "user": {"username": _CONTROL},
        }
        self.catalog = _catalog()
        app = create_app(
            self.catalog,
            ResearcherTokens(ISSUER, _AUDIENCE, self.key.key_set),
            ServiceTokens(self.reviews.review, _AUDIENCE, frozenset({_CONTROL})),
            ObjectAccess(
                "https://versitygw:7070",
                "us-east-1",
                lambda: Credentials("data", "data-secret"),
                lambda: _NOW,
            ),
            hostname="heartwood-data.heartwood-platform.svc.cluster.local:8443",
            platform_id="reference-platform",
        )
        self.client = TestClient(app)

    def researcher(
        self, subject: str = _ALICE, project: str = "demo", **claims: object
    ) -> dict[str, str]:
        token = self.key.token(
            **{"aud": _AUDIENCE, "sub": subject, "azp": f"hw-proj-{project}", **claims}
        )
        return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def service() -> Service:
    return Service()


def test_signature_matches_the_published_example() -> None:
    url = presign(
        method="GET",
        endpoint="https://examplebucket.s3.amazonaws.com",
        path="/test.txt",
        credentials=Credentials("AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"),
        region="us-east-1",
        expires=86400,
        now=datetime(2013, 5, 24, tzinfo=UTC),
    )
    assert url.endswith(
        "X-Amz-Signature=aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404"
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"expires": 0},
        {"expires": 7 * 24 * 3600 + 1},
        {"now": datetime(2026, 1, 1)},
        {"endpoint": "https://store/prefix"},
        {"endpoint": "store:7070"},
        {"path": "bucket/key"},
    ],
)
def test_signing_refuses_invalid_requests(changes: dict[str, object]) -> None:
    arguments: dict[str, object] = {
        "method": "GET",
        "endpoint": "https://store:7070",
        "path": "/bucket/key",
        "credentials": Credentials("a", "b"),
        "region": "us-east-1",
        "expires": 60,
        "now": _NOW,
        **changes,
    }
    with pytest.raises(ValueError, match=r"\S"):
        presign(**arguments)  # type: ignore[arg-type]


def test_access_validity_grows_with_the_object_size_up_to_an_hour() -> None:
    assert access_seconds(0) == 300
    assert access_seconds(1) == 301
    assert access_seconds(100 * 1024 * 1024) == 400
    assert access_seconds(10**12) == MAX_SECONDS


def test_dataset_identifiers_are_unique_across_projects() -> None:
    document = _catalog().model_dump(mode="json")
    document["projects"][1]["datasets"][0]["id"] = "synthetic-cohort"
    with pytest.raises(ValidationError, match="unique across projects"):
        Catalog.model_validate(document)


@pytest.mark.parametrize("name", ["../escape", "a/b", ".hidden", "trailing.", ""])
def test_object_names_have_no_path(name: str) -> None:
    document = _catalog().model_dump(mode="json")
    document["projects"][0]["datasets"][0]["objects"][0]["name"] = name
    with pytest.raises(ValidationError):
        Catalog.model_validate(document)


def test_service_info_names_drs_without_a_token(service: Service) -> None:
    info = service.client.get("/ga4gh/drs/v1/service-info").json()
    assert info["type"] == {"group": "org.ga4gh", "artifact": "drs", "version": "1.5.0"}


def test_a_member_resolves_an_object_of_the_token_project(service: Service) -> None:
    response = service.client.get(
        "/ga4gh/drs/v1/objects/synthetic-cohort.participants.csv", headers=service.researcher()
    )
    assert response.status_code == 200
    content = _CONTENT["participants.csv"]
    assert response.json() == {
        "id": "synthetic-cohort.participants.csv",
        "name": "participants.csv",
        "self_uri": "drs://heartwood-data.heartwood-platform.svc.cluster.local:8443/"
        "synthetic-cohort.participants.csv",
        "size": len(content),
        "created_time": "2026-11-01T00:00:00+00:00",
        "mime_type": "text/csv",
        "description": "A synthetic cohort.",
        "checksums": [{"checksum": hashlib.sha256(content).hexdigest(), "type": "sha-256"}],
        "access_methods": [{"type": "https", "access_id": "https"}],
    }


def test_an_object_of_another_project_does_not_exist_for_the_caller(service: Service) -> None:
    # Alice is a member of both projects; her token for demo does not reach other's data.
    other = service.client.get(
        "/ga4gh/drs/v1/objects/other-panel.panel.csv", headers=service.researcher()
    )
    unknown = service.client.get(
        "/ga4gh/drs/v1/objects/no-such.object", headers=service.researcher()
    )
    access = service.client.get(
        "/ga4gh/drs/v1/objects/other-panel.panel.csv/access/https", headers=service.researcher()
    )
    assert other.status_code == unknown.status_code == access.status_code == 404
    assert other.json() == unknown.json()
    own = service.client.get(
        "/ga4gh/drs/v1/objects/other-panel.panel.csv",
        headers=service.researcher(project="other"),
    )
    assert own.status_code == 200


def test_tokens_of_non_members_other_audiences_and_no_token_are_refused(service: Service) -> None:
    path = "/ga4gh/drs/v1/objects/synthetic-cohort.participants.csv"
    assert service.client.get(path).status_code == 401
    assert (
        service.client.get(path, headers=service.researcher(aud="heartwood-control")).status_code
        == 401
    )
    assert (
        service.client.get(path, headers=service.researcher(azp="heartwood-cli")).status_code == 401
    )
    assert service.client.get(path, headers=service.researcher(_BOB, "other")).status_code == 403


def test_access_is_a_url_for_the_object_that_expires_with_its_size(service: Service) -> None:
    response = service.client.get(
        "/ga4gh/drs/v1/objects/synthetic-cohort.README.md/access/https",
        headers=service.researcher(),
    )
    assert response.status_code == 200
    url = urlsplit(response.json()["url"])
    query = parse_qs(url.query)
    assert (url.scheme, url.netloc, url.path) == (
        "https",
        "versitygw:7070",
        "/hw-project-demo/datasets/synthetic-cohort/README.md",
    )
    assert query["X-Amz-Expires"] == ["301"]
    assert query["X-Amz-Credential"][0].startswith("data/20261119/us-east-1/s3/")
    assert query["X-Amz-SignedHeaders"] == ["host"]
    missing = service.client.get(
        "/ga4gh/drs/v1/objects/synthetic-cohort.README.md/access/s3",
        headers=service.researcher(),
    )
    assert missing.status_code == 404


def _plan(service: Service, body: object, token: str = "control-token") -> Any:  # noqa: ANN401
    return service.client.post(
        "/v1/volume-plans", headers={"Authorization": f"Bearer {token}"}, json=body
    )


def test_the_control_service_receives_the_access_plan_of_a_members_project(
    service: Service,
) -> None:
    response = _plan(service, {"subject": _BOB, "project": "demo"})
    assert response.status_code == 200
    plan = response.json()
    assert plan["project"] == "demo"
    assert plan["content_sha256"] == content_sha256(service.catalog.entries("demo"))
    assert {item["path"] for item in plan["objects"]} == {
        "synthetic-cohort/participants.csv",
        "synthetic-cohort/README.md",
    }
    for item in plan["objects"]:
        assert "/hw-project-demo/datasets/synthetic-cohort/" in item["url"]
        assert datetime.fromisoformat(item["expires_at"]) == _NOW + timedelta(seconds=301)
    assert Plan.model_validate(plan).content_sha256 == plan["content_sha256"]


@pytest.mark.parametrize(
    ("token", "body", "status"),
    [
        ("control-token", {"subject": _BOB, "project": "other"}, 403),
        ("control-token", {"subject": _BOB, "project": "missing"}, 403),
        ("control-token", {"subject": _BOB}, 400),
        ("control-token", {"subject": _BOB, "project": "demo", "extra": 1}, 400),
        ("hub-token", {"subject": _BOB, "project": "demo"}, 401),
        ("unknown", {"subject": _BOB, "project": "demo"}, 401),
    ],
)
def test_access_plans_are_refused_to_other_callers_and_non_members(
    service: Service, token: str, body: object, status: int
) -> None:
    assert _plan(service, body, token).status_code == status


def test_a_researcher_token_receives_no_access_plan(service: Service) -> None:
    response = service.client.post(
        "/v1/volume-plans",
        headers=service.researcher(),
        json={"subject": _ALICE, "project": "demo"},
    )
    assert response.status_code == 401


def test_a_large_plan_request_is_refused_before_authentication(service: Service) -> None:
    response = service.client.post(
        "/v1/volume-plans", headers={"Authorization": "Bearer control-token"}, content=b"x" * 5000
    )
    assert response.status_code == 413


def _copy_plan(objects: dict[str, bytes]) -> Plan:
    service = Service()
    plan = _plan(service, {"subject": _BOB, "project": "demo"}).json()
    plan["objects"] = [item for item in plan["objects"] if item["path"].split("/")[1] in objects]
    entries = [e for e in service.catalog.entries("demo") if e.item.name in objects]
    plan["content_sha256"] = content_sha256(entries)
    return Plan.model_validate(plan)


def _store(objects: dict[str, bytes], status: int = 200) -> httpx.Client:
    def handle(request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[1]
        return httpx.Response(status, content=objects.get(name, b""))

    return httpx.Client(transport=httpx.MockTransport(handle))


def test_copy_writes_verified_objects_readable_by_every_user(tmp_path: Path) -> None:
    # The volume's root belongs to another user, so the copy leaves its mode alone.
    tmp_path.chmod(0o777)
    (tmp_path / "lost+found").mkdir()
    (tmp_path / "leftover").write_text("from a failed job")
    copy(_copy_plan(_CONTENT), _store(_CONTENT), tmp_path)
    written = tmp_path / "synthetic-cohort" / "participants.csv"
    assert written.read_bytes() == _CONTENT["participants.csv"]
    assert written.stat().st_mode & 0o777 == 0o644
    assert (tmp_path / "synthetic-cohort").stat().st_mode & 0o777 == 0o755
    assert sorted(path.name for path in tmp_path.iterdir()) == ["lost+found", "synthetic-cohort"]
    assert tmp_path.stat().st_mode & 0o777 == 0o777


@pytest.mark.parametrize(
    ("served", "status", "message"),
    [
        (
            {"participants.csv": b"id,age\n1,41\n", "README.md": _CONTENT["README.md"]},
            200,
            "checksum",
        ),
        ({"participants.csv": b"id,age\n1,40\nmore\n", "README.md": b""}, 200, "more bytes"),
        (_CONTENT, 403, "answered 403"),
    ],
)
def test_copy_fails_for_an_object_that_differs_from_the_catalog(
    tmp_path: Path, served: dict[str, bytes], status: int, message: str
) -> None:
    with pytest.raises(CopyError, match=message):
        copy(_copy_plan(_CONTENT), _store(served, status), tmp_path)
    assert not list(tmp_path.rglob("*.csv"))


def test_copy_refuses_a_plan_whose_objects_do_not_match_its_digest(tmp_path: Path) -> None:
    plan = _copy_plan(_CONTENT).model_copy(update={"content_sha256": "0" * 64})
    with pytest.raises(CopyError, match="content digest"):
        copy(plan, _store(_CONTENT), tmp_path)


def test_seeding_writes_each_object_and_no_policy(tmp_path: Path) -> None:
    for name, value in {"root-access": "root", "root-secret": "s"}.items():
        (tmp_path / name).write_text(value)
    (tmp_path / "object").write_bytes(b"content")
    settings = SeedSettings(
        endpoint="https://versitygw:7070",
        region="us-east-1",
        ca_file=tmp_path / "ca.crt",
        root_access_key_file=tmp_path / "root-access",
        root_secret_key_file=tmp_path / "root-secret",
        objects=(
            SeedObject(
                bucket="hw-project-demo",
                key="datasets/synthetic-cohort/object",
                file=tmp_path / "object",
                sha256=hashlib.sha256(b"content").hexdigest(),
            ),
        ),
    )
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    seed(settings, httpx.Client(transport=httpx.MockTransport(handle)), lambda: _NOW)
    # The bucket policies have their own writer, heartwood_platform.common.bucket_policies.
    assert [(r.method, r.url.path) for r in requests] == [
        ("PUT", "/hw-project-demo/datasets/synthetic-cohort/object"),
    ]
    assert requests[0].content == b"content"
    (tmp_path / "object").write_bytes(b"changed")
    with pytest.raises(RuntimeError, match="checksum"):
        seed(settings, httpx.Client(transport=httpx.MockTransport(handle)), lambda: _NOW)
