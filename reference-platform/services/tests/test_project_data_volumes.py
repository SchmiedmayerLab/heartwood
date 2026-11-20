# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fakes import FakeKubernetes
from heartwood_platform.common.kubernetes import Kubernetes
from heartwood_platform.control.project_data import (
    ProjectData,
    ProjectDataSettings,
    claim_name,
)
from heartwood_platform.control.sessions import SessionError

_NAMESPACE = "hw-s-0123456789abcdef"
_SESSION = "0123456789abcdef"
_IMAGE = "registry.example.test/heartwood-platform-services@sha256:" + "2" * 64
_CLAIMS = f"/api/v1/namespaces/{_NAMESPACE}/persistentvolumeclaims"
_JOBS = f"/apis/batch/v1/namespaces/{_NAMESPACE}/jobs"
_SECRETS = f"/api/v1/namespaces/{_NAMESPACE}/secrets"


class DataService:
    """Answers access plans; each plan carries new URLs."""

    def __init__(self) -> None:
        self.content = "a" * 64
        self.project = "demo"
        self.objects: list[dict[str, Any]] = [
            {"id": "cohort.participants.csv", "path": "cohort/participants.csv", "size": 12},
            {"id": "cohort.README.md", "path": "cohort/README.md", "size": 3},
        ]
        self.requests: list[httpx.Request] = []

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self._handle))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        expires = (datetime.now(UTC) + timedelta(seconds=301)).isoformat()
        return httpx.Response(
            200,
            json={
                "project": self.project,
                "content_sha256": self.content,
                "objects": [
                    {
                        **item,
                        "sha256": "b" * 64,
                        "url": f"https://versitygw:7070/{item['path']}?X-Amz-Signature="
                        f"{len(self.requests)}",
                        "expires_at": expires,
                    }
                    for item in self.objects
                ],
            },
        )


class Fixture:
    def __init__(self, directory: Path) -> None:
        self.kubernetes = FakeKubernetes()
        self.data = DataService()
        token = directory / "token"
        token.write_text("control-data-token\n")
        self.settings = ProjectDataSettings(
            data_url="https://heartwood-data.heartwood-platform.svc.cluster.local:8443",
            token_file=token,
            copy_image=_IMAGE,
            storage_class="heartwood-project-data",
            workspace="research",
            mount_path="/data",
            object_store_namespace="heartwood-platform",
            object_store_labels={"app.kubernetes.io/component": "versitygw"},
            object_store_port=7070,
        )
        self.project_data = ProjectData(
            Kubernetes(self.kubernetes.client()),
            self.settings,
            self.data.client(),
            sleep=lambda _: None,
        )

    def fill(self) -> Any:  # noqa: ANN401
        return self.project_data.fill(_SESSION, _NAMESPACE, "researcher-1", "demo")

    def claim(self) -> dict[str, Any]:
        return self.kubernetes.objects[f"{_CLAIMS}/{claim_name(self.data.content)}"]

    def created(self, collection: str) -> list[str]:
        return [
            path
            for method, path in self.kubernetes.calls
            if method == "POST" and path == collection
        ]


@pytest.fixture
def fixture(tmp_path: Path) -> Fixture:
    return Fixture(tmp_path)


def test_a_successful_copy_makes_the_claim_attachable_and_removes_the_job(
    fixture: Fixture,
) -> None:
    fixture.kubernetes.job_outcomes = ["Complete"]
    volume = fixture.fill()

    claim = fixture.claim()
    assert volume.model_dump() == {
        "claim_name": "hw-project-data-aaaaaaaaaaaaaaaa",
        "claim_uid": claim["metadata"]["uid"],
        "content_sha256": "a" * 64,
        "mount_path": "/data",
        "objects": 2,
    }
    assert claim["metadata"]["labels"] == {
        "heartwood.platform/session": _SESSION,
        "heartwood.platform/project": "demo",
        "heartwood.platform/role": "project-data",
        "openshell.ai/sandbox-attachable": "true",
        "openshell.ai/sandbox-attachable-workspace": "research",
    }
    assert claim["metadata"]["annotations"] == {"heartwood.platform/content-sha256": "a" * 64}
    assert claim["spec"]["storageClassName"] == "heartwood-project-data"
    # The plan with its URLs and the job are gone once the copy ended.
    assert not fixture.kubernetes.kind("Job", _NAMESPACE)
    assert not fixture.kubernetes.kind("Secret", _NAMESPACE)
    job_deletions = [d for d in fixture.kubernetes.deletions if d[0].startswith(_JOBS)]
    assert job_deletions
    assert all(policy == "Foreground" for _, policy in job_deletions)
    (request,) = fixture.data.requests
    assert request.headers["Authorization"] == "Bearer control-data-token"
    assert json.loads(request.content) == {"subject": "researcher-1", "project": "demo"}


def test_the_copy_job_runs_restricted_without_credentials(fixture: Fixture) -> None:
    fixture.kubernetes.job_outcomes = ["Complete"]
    fixture.fill()

    (job,) = [item for item in fixture.kubernetes.posted if item["kind"] == "Job"]
    secrets = [item for item in fixture.kubernetes.posted if item["kind"] == "Secret"]
    spec = job["spec"]
    pod = spec["template"]["spec"]
    (container,) = pod["containers"]
    assert spec["backoffLimit"] == 0
    assert 299 <= spec["activeDeadlineSeconds"] <= 301
    assert spec["ttlSecondsAfterFinished"] == 600
    assert spec["template"]["metadata"]["labels"]["heartwood.platform/role"] == "project-data-copy"
    assert pod["automountServiceAccountToken"] is False
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    assert container["image"] == _IMAGE
    assert container["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
        "readOnlyRootFilesystem": True,
    }
    assert "env" not in container
    assert {volume["name"]: set(volume) - {"name"} for volume in pod["volumes"]} == {
        "plan": {"secret"},
        "ca": {"configMap"},
        "data": {"persistentVolumeClaim"},
    }
    (secret,) = secrets
    (owner,) = secret["metadata"]["ownerReferences"]
    assert (owner["kind"], owner["name"]) == ("Job", job["metadata"]["name"])
    plan = json.loads(base64.b64decode(secret["data"]["plan.json"]))
    assert [item["path"] for item in plan["objects"]] == [
        "cohort/participants.csv",
        "cohort/README.md",
    ]
    policy = fixture.kubernetes.objects[
        f"/apis/networking.k8s.io/v1/namespaces/{_NAMESPACE}/networkpolicies/"
        "hw-project-data-copy-egress"
    ]
    assert policy["spec"]["podSelector"] == {
        "matchLabels": {"heartwood.platform/role": "project-data-copy"}
    }
    assert policy["spec"]["policyTypes"] == ["Egress"]
    store = policy["spec"]["egress"][1]
    assert store["ports"] == [{"protocol": "TCP", "port": 7070}]
    assert store["to"][0]["podSelector"] == {
        "matchLabels": {"app.kubernetes.io/component": "versitygw"}
    }


def test_an_unchanged_catalog_reuses_the_filled_claim(fixture: Fixture) -> None:
    fixture.kubernetes.job_outcomes = ["Complete"]
    first = fixture.fill()
    second = fixture.fill()
    assert first == second
    assert len(fixture.created(_JOBS)) == 1


def test_a_failed_copy_is_run_again_with_new_urls(fixture: Fixture) -> None:
    fixture.kubernetes.job_outcomes = ["Failed", "Complete"]
    volume = fixture.fill()
    assert volume is not None
    assert len(fixture.created(_JOBS)) == 2
    assert len(fixture.data.requests) == 2


def test_three_failed_copies_fail_the_launch_and_leave_the_claim_unattachable(
    fixture: Fixture,
) -> None:
    fixture.kubernetes.job_outcomes = ["Failed", "Failed", "Failed", "Complete"]
    with pytest.raises(SessionError, match="could not be copied"):
        fixture.fill()
    assert len(fixture.created(_JOBS)) == 3
    assert "openshell.ai/sandbox-attachable" not in fixture.claim()["metadata"]["labels"]
    assert not fixture.kubernetes.kind("Secret", _NAMESPACE)
    # The next launch completes the volume.
    assert fixture.fill() is not None
    assert fixture.claim()["metadata"]["labels"]["openshell.ai/sandbox-attachable"] == "true"


def test_a_catalog_change_during_the_copy_fails_the_launch(fixture: Fixture) -> None:
    fixture.kubernetes.job_outcomes = ["Failed"]
    original = fixture.data._handle

    def changing(request: httpx.Request) -> httpx.Response:
        if fixture.data.requests:
            fixture.data.content = "c" * 64
        return original(request)

    fixture.data._handle = changing  # type: ignore[method-assign]
    fixture.project_data = ProjectData(
        Kubernetes(fixture.kubernetes.client()),
        fixture.settings,
        httpx.Client(transport=httpx.MockTransport(changing)),
        sleep=lambda _: None,
    )
    with pytest.raises(SessionError, match="catalog changed"):
        fixture.fill()


def test_a_new_catalog_replaces_the_claim_of_earlier_content(fixture: Fixture) -> None:
    fixture.kubernetes.job_outcomes = ["Complete", "Complete"]
    earlier = fixture.fill()
    fixture.data.content = "d" * 64
    current = fixture.fill()
    assert earlier.claim_name != current.claim_name
    claims = fixture.kubernetes.kind("PersistentVolumeClaim", _NAMESPACE)
    assert list(claims) == [current.claim_name]


def test_a_project_without_catalog_objects_gets_no_volume(fixture: Fixture) -> None:
    fixture.data.objects = []
    assert fixture.fill() is None
    assert not fixture.kubernetes.kind("PersistentVolumeClaim", _NAMESPACE)


def test_a_plan_for_another_project_is_refused(fixture: Fixture) -> None:
    fixture.data.project = "other"
    with pytest.raises(SessionError, match="another project"):
        fixture.fill()
    assert not fixture.kubernetes.kind("PersistentVolumeClaim", _NAMESPACE)


def test_an_unavailable_data_service_fails_the_launch(fixture: Fixture) -> None:
    fixture.project_data = ProjectData(
        Kubernetes(fixture.kubernetes.client()),
        fixture.settings,
        httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(503))),
    )
    with pytest.raises(httpx.HTTPStatusError):
        fixture.fill()


def test_a_claim_of_other_content_under_the_name_is_refused(fixture: Fixture) -> None:
    fixture.kubernetes.job_outcomes = ["Complete"]
    fixture.fill()
    fixture.claim()["metadata"]["annotations"]["heartwood.platform/content-sha256"] = "f" * 64
    with pytest.raises(SessionError, match="other content"):
        fixture.fill()


def test_an_invalid_plan_fails_the_launch_with_a_session_error(fixture: Fixture) -> None:
    fixture.project_data = ProjectData(
        Kubernetes(fixture.kubernetes.client()),
        fixture.settings,
        httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={}))),
    )
    with pytest.raises(SessionError, match="invalid plan"):
        fixture.fill()
