# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""The job service's clients: the TES executor, the object store and the control service."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from heartwood_platform.common.signing import Credentials, presign
from heartwood_platform.jobs.__main__ import Settings
from heartwood_platform.jobs.objects import ObjectNotFoundError, ProjectBucket, StoreObjects
from heartwood_platform.jobs.platform import ControlContexts, NoSessionError, NotAMemberError
from heartwood_platform.jobs.tes import HttpTesExecutor, TaskNotFoundError, TesTask

_NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


@pytest.fixture
def ca_file(tmp_path: Path) -> Path:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test CA")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(_NOW - timedelta(days=1))
        .not_valid_after(_NOW + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    path = tmp_path / "ca.crt"
    path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    return path


def test_the_executor_client_speaks_tes_with_a_basic_credential(ca_file: Path) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.raw_path.decode()
        if request.method == "POST" and path == "/ga4gh/tes/v1/tasks":
            return httpx.Response(200, json={"id": "abc"})
        if path.startswith("/ga4gh/tes/v1/tasks/missing"):
            return httpx.Response(404, json={"message": "task not found"})
        if path.startswith("/ga4gh/tes/v1/tasks/abc?"):
            return httpx.Response(200, json={"id": "abc", "state": "RUNNING", "extra": 1})
        if path.startswith("/ga4gh/tes/v1/tasks?"):
            return httpx.Response(200, json={"tasks": [{"id": "abc"}], "next_page_token": "n"})
        return httpx.Response(200, json={})

    executor = HttpTesExecutor(
        "https://funnel.example.test/ga4gh/tes",
        ca_file,
        "jobsvc",
        "secret",
        transport=httpx.MockTransport(handle),
    )
    task = TesTask.model_validate(
        {
            "name": "t",
            "resources": {"cpu_cores": 1, "ram_gb": 1},
            "executors": [{"image": "i", "command": ["true"]}],
        }
    )
    assert executor.create(task) == "abc"
    assert executor.get("abc").state == "RUNNING"
    assert executor.list().next_page_token == "n"
    executor.cancel("abc")
    with pytest.raises(TaskNotFoundError):
        executor.get("missing")
    # Funnel needs the colon of :cancel encoded.
    assert requests[3].url.raw_path == b"/ga4gh/tes/v1/tasks/abc%3Acancel"
    expected = "Basic " + base64.b64encode(b"jobsvc:secret").decode()
    assert {request.headers["Authorization"] for request in requests} == {expected}
    body = json.loads(requests[0].content)
    assert "description" not in body
    assert body["resources"] == {
        "cpu_cores": 1,
        "ram_gb": 1.0,
        "backend_parameters": {},
        "backend_parameters_strict": False,
    }


def test_the_executor_client_refuses_plain_http(ca_file: Path) -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        HttpTesExecutor("http://funnel.example.test", ca_file, "jobsvc", "secret")


def test_presigned_urls_name_only_a_store_that_serves_https() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        StoreObjects("http://versitygw.example.test:7070", "us-east-1", {}, httpx.Client())


def _store(handle: httpx.MockTransport) -> StoreObjects:
    return StoreObjects(
        "https://versitygw.example.test:7070",
        "us-east-1",
        {"demo": ProjectBucket("hw-project-demo", Credentials("jobs-demo", "secret"))},
        httpx.Client(transport=handle),
        clock=lambda: _NOW,
    )


def test_inputs_are_existing_project_objects_read_through_short_urls() -> None:
    heads: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        heads.append(request.url.path)
        found = request.url.path == "/hw-project-demo/datasets/cohort/data.csv"
        return httpx.Response(200 if found else 404)

    store = _store(httpx.MockTransport(handle))
    url = store.input_url("demo", "cohort.data.csv", 900)
    parsed = urlsplit(url)
    assert parsed.path == "/hw-project-demo/datasets/cohort/data.csv"
    query = parse_qs(parsed.query)
    assert query["X-Amz-Expires"] == ["900"]
    assert query["X-Amz-Credential"][0].startswith("jobs-demo/")
    assert url == presign(
        method="GET",
        endpoint="https://versitygw.example.test:7070",
        path="/hw-project-demo/datasets/cohort/data.csv",
        credentials=Credentials("jobs-demo", "secret"),
        region="us-east-1",
        expires=900,
        now=_NOW,
    )
    # A results object is never an input: identifiers name objects below datasets/.
    for missing in ("cohort.other.csv", "results.count.txt", "../x", "nodot", "s3://bucket/key"):
        with pytest.raises(ObjectNotFoundError):
            store.input_url("demo", missing, 900)
    with pytest.raises(ObjectNotFoundError):
        store.input_url("other", "cohort.data.csv", 900)
    assert heads == [
        "/hw-project-demo/datasets/cohort/data.csv",
        "/hw-project-demo/datasets/cohort/other.csv",
        "/hw-project-demo/datasets/results/count.txt",
    ]


def test_outputs_go_below_the_results_prefix_of_the_operation() -> None:
    store = _store(httpx.MockTransport(lambda _: httpx.Response(500)))
    url = store.output_url("demo", "op-1", "count.txt", 1200)
    assert urlsplit(url).path == "/hw-project-demo/results/op-1/count.txt"
    assert parse_qs(urlsplit(url).query)["X-Amz-Expires"] == ["1200"]
    assert store.output_location("demo", "op-1") == "s3://hw-project-demo/results/op-1/"


def test_launch_contexts_come_from_the_control_service_with_the_service_token(
    ca_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[httpx.Request] = []
    context = {"project_context_id": "session-1"}

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        subject = json.loads(request.content)["subject"]
        status = {"alice": 200, "bob": 403, "carol": 404}[subject]
        return httpx.Response(status, json=context if status == 200 else {"error": "x"})

    contexts = ControlContexts("https://control.example.test", ca_file, lambda: "service-token")
    monkeypatch.setattr(
        contexts,
        "_client",
        httpx.Client(
            base_url="https://control.example.test", transport=httpx.MockTransport(handle)
        ),
    )
    assert contexts.context("alice", "demo") == "session-1"
    with pytest.raises(NotAMemberError):
        contexts.context("bob", "demo")
    with pytest.raises(NoSessionError):
        contexts.context("carol", "demo")
    assert (seen[0].method, seen[0].url.path) == ("POST", "/v1/projects/demo/launch-context")
    assert all("alice" not in str(request.url) for request in seen)
    assert seen[0].headers["Authorization"] == "Bearer service-token"


def _settings(**changes: object) -> dict[str, object]:
    project = {
        "id": "demo",
        "executor": {
            "url": "https://funnel-server.hw-jobs-demo.svc:8000",
            "ca_file": "/ca.crt",
            "user": "jobsvc",
            "password_file": "/password",
        },
        "store": {"bucket": "hw-project-demo", "access_key_file": "/a", "secret_key_file": "/s"},
        "max_cpu_cores": 1,
        "max_ram_gb": 1,
        "max_disk_gb": 1,
        "max_runtime_seconds": 3600,
    }
    return {
        "database": "/jobs.sqlite3",
        "audience": "heartwood-jobs",
        "issuer": "https://keycloak/realms/heartwood",
        "keys_url": "https://keycloak/certs",
        "keys_ca_file": "/ca.crt",
        "tls": {"certificate_file": "/tls.crt", "key_file": "/tls.key"},
        "control": {"url": "https://control:8443", "ca_file": "/ca.crt", "token_file": "/t"},
        "store": {"endpoint": "https://versitygw:7070", "ca_file": "/ca.crt"},
        "images": ["registry.example.test/task@sha256:" + "a" * 64],
        "cpu_profile": "synthetic-batch-small",
        "gpu_profile": "synthetic-batch-gpu",
        "drs_host": "heartwood-data",
        "projects": [project],
        **changes,
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"images": ["registry.example.test/task:latest"]},
        {"reconcile_seconds": 300},
        {"start_deadline_seconds": 7 * 24 * 3600},
        # Presigned URLs never travel in plain HTTP.
        {"store": {"endpoint": "http://versitygw:7070", "ca_file": "/ca.crt"}},
    ],
)
def test_the_configuration_refuses_tags_and_bounds_that_urls_cannot_cover(
    changes: dict[str, object],
) -> None:
    Settings.model_validate(_settings())
    with pytest.raises(ValueError, match=r"\S"):
        Settings.model_validate(_settings(**changes))
