# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Kubernetes qualification evidence passes only for a complete run with its inputs."""

from __future__ import annotations

import ast
import copy
import importlib
import os
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_COMMIT = "1" * 40
_KIND = "synthetic-kubernetes"
_RUNNER = "deploy/qualify-kubernetes-worker.py"
_DOCKERFILE = "images/worker/candidate/kubernetes-controller.Dockerfile"


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
    steps = {step["name"]: step for step in cluster["jobs"]["cluster"]["steps"]}

    # Without the candidate build's artifacts in the same run the stage cannot start.
    assert set(cluster["on"]) == {"workflow_call"}
    assert cluster["permissions"] == {"contents": "read"}
    for name in ("worker-dependency-candidate", "worker-gateway-candidate"):
        assert any(
            step.get("with", {}).get("name") == name + "-${{ github.sha }}"
            for step in steps.values()
        )
    assert '--commit "${GITHUB_SHA}"' in steps["Verify the evidence for this commit"]["run"]
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
