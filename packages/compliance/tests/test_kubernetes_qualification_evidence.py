# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Kubernetes qualification evidence passes only for a complete run with its inputs."""

from __future__ import annotations

import ast
import base64
import copy
import hashlib
import hmac
import http.server
import importlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

_ROOT = Path(__file__).resolve().parents[3]
_COMMIT = "1" * 40
_KIND = "synthetic-kubernetes"
_RUNNER = "deploy/qualify-kubernetes-worker.py"
_DOCKERFILE = "images/worker/candidate/kubernetes-controller.Dockerfile"
_ISSUER = "https://keycloak.heartwood-platform.svc.cluster.local:8443/realms/heartwood"
_RESEARCHER_KEY = b"synthetic-researcher-id-key-0123456789"


@pytest.fixture
def evidence(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.syspath_prepend(str(_ROOT / "deploy"))
    return importlib.import_module("qualification_evidence")


def _manifest(evidence: ModuleType) -> dict[str, Any]:
    image = {
        "revision": _COMMIT,
        "repo_digests": [],
        "build_records": {"sdk": {"wheels": {}}, "application": {"commit": _COMMIT}},
    }
    return {
        "qualification_kind": _KIND,
        "source": {"commit": _COMMIT, "tree_clean": True},
        "image_sources": {"worker": image, "runtime": copy.deepcopy(image)},
        "qualification_sources_unchanged": True,
        "returncode": 0,
        "session_removed": True,
        "cases": [
            {"id": identifier, "outcome": "passed", "seconds": 1.5}
            for identifier in sorted(evidence.EXPECTED_CASES[_KIND])
        ],
    }


def _session_kept(manifest: dict[str, Any]) -> None:
    manifest["session_removed"] = False


def _case_missing(manifest: dict[str, Any]) -> None:
    manifest["cases"].pop()


def _case_failed(manifest: dict[str, Any]) -> None:
    manifest["cases"][0]["outcome"] = "failed"


def _other_image(manifest: dict[str, Any]) -> None:
    manifest["image_sources"]["worker"]["revision"] = "2" * 40


def _sources_changed(manifest: dict[str, Any]) -> None:
    manifest["qualification_sources_unchanged"] = False


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (_session_kept, "the session namespace was not removed"),
        (_case_missing, "the executed cases differ from the expected set"),
        (_case_failed, "not every case passed"),
        (_other_image, "the worker image was not built from the source commit"),
        (_sources_changed, "qualification sources changed during the run"),
    ],
)
def test_only_a_complete_kubernetes_run_qualifies_its_commit(
    evidence: ModuleType, change: Callable[[dict[str, Any]], None], reason: str
) -> None:
    manifest = _manifest(evidence)

    assert evidence.failures(manifest) == []
    change(manifest)
    assert evidence.failures(manifest) == [reason]


def test_every_replaced_docker_value_names_a_platform_requirement(evidence: ModuleType) -> None:
    for entry in evidence.KUBERNETES_PLATFORM_REQUIREMENTS:
        assert set(entry) == {"docker_evidence", "kubernetes_evidence", "platform_requirement"}
        assert entry["docker_evidence"]
        assert entry["platform_requirement"]
    assert all(evidence.KUBERNETES_CASES_NOT_RUN.values())


def _local_imports(path: Path) -> set[str]:
    """Name the repository files a qualification module imports."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        names = (
            [alias.name for alias in node.names]
            if isinstance(node, ast.Import)
            else [node.module]
            if isinstance(node, ast.ImportFrom) and node.module
            else []
        )
        for name in names:
            for candidate in (
                _ROOT / "deploy/tests" / f"{name}.py",
                _ROOT / "deploy" / f"{name}.py",
                _ROOT / (name.replace(".", "/") + ".py"),
            ):
                if candidate.is_file() and not name.startswith("heartwood"):
                    found.add(candidate.relative_to(_ROOT).as_posix())
    return found


def test_controller_image_and_source_list_hold_everything_the_cases_import() -> None:
    dockerfile = (_ROOT / _DOCKERFILE).read_text()
    copied: set[str] = set()
    for line in dockerfile.splitlines():
        words = [word for word in line.split() if not word.startswith("--")]
        if words[:1] == ["COPY"] and "--from=" not in line:
            for source in words[1:-1]:
                copied.update(path.relative_to(_ROOT).as_posix() for path in _ROOT.glob(source))
    listed = set(re.findall(r'^    "([^"]+)",$', (_ROOT / _RUNNER).read_text(), re.MULTILINE))
    pending = {"deploy/tests/test_worker_kubernetes_integration.py"}
    needed: set[str] = set()
    while pending:
        path = pending.pop()
        needed.add(path)
        pending |= _local_imports(_ROOT / path) - needed
    assert "deploy/tests/worker_native_fixture.py" in needed
    assert "deploy/tests/test_worker_lifecycle_integration.py" in needed
    # The run hashes every module of the cases, and the controller image contains each.
    tls_fixture = "packages/audit/tests/tls_fixture.py"
    assert "COPY --from=records-tls-fixture /tls_fixture.py" in dockerfile
    assert needed <= copied | {tls_fixture}
    assert needed <= listed
    assert {_RUNNER, _DOCKERFILE, "deploy/qualification_evidence.py"} <= listed
    assert all((_ROOT / name).is_file() for name in listed)


def _workflow(name: str) -> dict[str, Any]:
    yaml = importlib.import_module("yaml")
    workflow: dict[str, Any] = yaml.load(
        (_ROOT / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader
    )
    return workflow


def _selected(workflow: str, changed: str) -> bool:
    selection = _workflow(workflow)["jobs"]["scope"]["steps"][1]["run"]
    comparison = '"$(git diff --name-only --no-renames HEAD^1 HEAD)"'
    completed = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", selection.replace(comparison, '"${CHANGED}"')],
        env={
            "EVENT_NAME": "pull_request",
            "CHANGED": changed,
            "GITHUB_OUTPUT": "/dev/stdout",
            "PATH": os.environ["PATH"],
        },
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout == "required=true\n"


def test_cluster_stage_runs_after_the_candidate_build_it_downloads() -> None:
    cluster = _workflow("kubernetes-qualification.yml")
    jobs = cluster["jobs"]
    steps = {
        name: {step["name"]: step for step in jobs[name]["steps"]} for name in ("images", "cluster")
    }

    # Without the candidate build's artifacts in the same run the stage cannot start.
    assert set(cluster["on"]) == {"workflow_call"}
    assert cluster["permissions"] == {"contents": "read"}
    for job, name in (
        ("images", "worker-dependency-candidate"),
        ("cluster", "worker-gateway-candidate"),
        ("cluster", "kubernetes-qualification-images"),
    ):
        assert any(
            step.get("with", {}).get("name") == name + "-${{ github.sha }}"
            for step in steps[job].values()
        )
    verify = steps["cluster"]["Verify the evidence for this commit"]
    assert '--commit "${GITHUB_SHA}"' in verify["run"]
    # The check of the stage keeps its name and requires every cluster job.
    assert jobs["qualification"]["name"] == "Kubernetes qualification"
    assert jobs["qualification"]["needs"] == ["scope", "images", "cluster"]
    assert jobs["qualification"]["if"] == "${{ !cancelled() }}"
    for caller in ("pull-request-validation.yml", "native-qualification.yml"):
        job = _workflow(caller)["jobs"]["kubernetes-qualification"]
        assert job["needs"] == "native-qualification"
        assert job["uses"] == "./.github/workflows/kubernetes-qualification.yml"
    # Every change that selects the cluster stage also selects the candidate build.
    for changed in (
        ".github/workflows/kubernetes-qualification.yml",
        ".github/workflows/worker-candidate.yml",
        ".github/workflows/pull-request-validation.yml",
        "packages/gateway/changed.py",
        "packages/schemas/changed.py",
        "deploy/changed.py",
        "images/changed",
        "reference-platform/changed",
    ):
        assert _selected("kubernetes-qualification.yml", changed)
        assert _selected("worker-candidate.yml", changed)
    assert not _selected("kubernetes-qualification.yml", "documentation/index.md")


def test_each_cluster_test_module_runs_in_exactly_one_stage() -> None:
    stages = {
        entry["stage"]: entry["tests"].split()
        for entry in _workflow("kubernetes-qualification.yml")["jobs"]["cluster"]["strategy"][
            "matrix"
        ]["include"]
    }
    modules = {path.name for path in (_ROOT / "reference-platform/tests/cluster").glob("test_*.py")}
    prefix = "../tests/cluster/"
    platform = stages["platform"]
    ignored = {item.removeprefix("--ignore=" + prefix) for item in platform[1:]}
    named = [
        item.removeprefix(prefix) for stage in ("hosted", "end-to-end") for item in stages[stage]
    ]

    assert set(stages) == {"platform", "hosted", "end-to-end", "worker"}
    # The platform stage runs every module that another stage does not.
    assert platform[0] == "../tests/cluster"
    assert all(item.startswith("--ignore=" + prefix) for item in platform[1:])
    assert ignored == set(named)
    assert set(named) <= modules
    assert len(named) == len(set(named))
    assert stages["worker"] == []
    # The chart tests run the stage's modules, and only the worker stage qualifies workers.
    steps = {
        step["name"]: step
        for step in _workflow("kubernetes-qualification.yml")["jobs"]["cluster"]["steps"]
    }
    chart = steps["Test the installed chart"]
    assert chart["if"] == "matrix.stage != 'worker'"
    assert chart["env"] == {"TESTS": "${{ matrix.tests }}"}
    assert '"${tests[@]}"' in chart["run"]
    for name in (
        "Qualify managed workers on the Kubernetes driver",
        "Verify the evidence for this commit",
    ):
        assert steps[name]["if"] == "matrix.stage == 'worker'"


@pytest.mark.parametrize(
    ("required", "results", "passes"),
    [
        ("false", {"scope": "success", "images": "skipped", "cluster": "skipped"}, True),
        ("true", {"scope": "success", "images": "success", "cluster": "success"}, True),
        ("true", {"scope": "success", "images": "success", "cluster": "failure"}, False),
        ("true", {"scope": "success", "images": "failure", "cluster": "skipped"}, False),
        ("", {"scope": "failure", "images": "skipped", "cluster": "skipped"}, False),
    ],
)
def test_the_stage_check_passes_only_when_the_stage_passed_or_was_not_required(
    required: str, results: dict[str, str], passes: bool
) -> None:
    (step,) = _workflow("kubernetes-qualification.yml")["jobs"]["qualification"]["steps"]
    completed = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", step["run"]],
        env={
            "REQUIRED": required,
            "RESULTS": json.dumps({name: {"result": value} for name, value in results.items()}),
            "PATH": os.environ["PATH"],
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert (completed.returncode == 0) is passes


def test_the_launch_token_is_sent_again_when_it_reaches_the_pod_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(_ROOT / "deploy"))
    spec = importlib.util.spec_from_file_location("kubernetes_runner", _ROOT / _RUNNER)
    assert spec is not None
    assert spec.loader is not None
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    received: list[str] = []
    paths: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_PUT(self) -> None:
            received.append(self.headers["Authorization"])
            paths.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *_arguments: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(runner, "_CONTROL", f"http://127.0.0.1:{server.server_port}")
    sent: list[str] = []

    class Cluster:
        def json(self, *arguments: str) -> dict[str, Any]:
            kind, name, *namespace = arguments
            assert namespace == ["--namespace", "heartwood-platform"]
            return {
                ("configmap", "platform-ca"): {"data": {"ca.crt": _authority()}},
                ("configmap", "heartwood-hub"): {
                    "data": {"settings.json": json.dumps({"issuer": _ISSUER})}
                },
                ("secret", "heartwood-researcher-ids"): {
                    "data": {"key": base64.b64encode(_RESEARCHER_KEY + b"\n").decode()}
                },
            }[(kind, name)]

        def kubectl(self, *arguments: str, stdin: str | None = None) -> str:
            assert stdin is not None
            # The first attempt reaches the pod shortened, as kubectl exec can deliver it.
            delivered = stdin[:-3] if not sent else stdin
            sent.append(delivered)
            script, *values = arguments[arguments.index("-c") + 1 :]
            return subprocess.run(
                [sys.executable, "-I", "-c", script, *values],
                input=delivered,
                capture_output=True,
                text=True,
                check=True,
            ).stdout

    try:
        reply = runner._call(Cluster(), "PUT", "synthetic-token\n")
    finally:
        server.shutdown()
    assert reply == {"status": 200, "body": "{}"}
    assert len(sent) == 2
    # Only the complete token reached the control service.
    assert received == ["Bearer synthetic-token"]
    # The path names the researcher by the identifier the platform derives, not the subject.
    digest = hmac.new(
        _RESEARCHER_KEY, f"{_ISSUER}\0{runner._SUBJECT}".encode(), hashlib.sha256
    ).hexdigest()
    assert paths == [f"/v1/researchers/r{digest[:32]}/projects/demo/launch"]


def _authority() -> str:
    """Return a synthetic CA certificate, which the launch script loads as its trust."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Synthetic Platform CA")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(key, hashes.SHA256())
    )
    return certificate.public_bytes(serialization.Encoding.PEM).decode()
