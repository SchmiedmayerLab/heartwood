# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Native qualification evidence states and proves what it qualified."""

from __future__ import annotations

import copy
import hashlib
import importlib
import importlib.util
import json
from collections.abc import Callable
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from xml.etree import ElementTree

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_COMMIT = "1" * 40
_OTHER_COMMIT = "2" * 40
_KIND = "synthetic-native"


@pytest.fixture
def evidence(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.syspath_prepend(str(_ROOT / "deploy"))
    return importlib.import_module("qualification_evidence")


def _write_report(path: Path, outcomes: dict[str, str | None]) -> None:
    suite = ElementTree.Element("testsuite", name="pytest")
    for identifier, outcome in outcomes.items():
        classname, name = identifier.split("::")
        case = ElementTree.SubElement(suite, "testcase", classname=classname, name=name, time="1.5")
        if outcome is not None:
            ElementTree.SubElement(case, outcome)
    ElementTree.ElementTree(suite).write(path)


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
        "network_retained": False,
        "cases": [
            {"id": identifier, "outcome": "passed", "seconds": 1.5}
            for identifier in sorted(evidence.EXPECTED_CASES[_KIND])
        ],
    }


def test_report_cases_keep_their_identity_and_outcome(evidence: ModuleType, tmp_path: Path) -> None:
    report = tmp_path / "junit.xml"
    _write_report(
        report,
        {"suite::passed": None, "suite::failed[a]": "failure", "suite::broken": "error"}
        | {"suite::skipped": "skipped"},
    )

    assert evidence.cases(report) == [
        {"id": "suite::passed", "outcome": "passed", "seconds": 1.5},
        {"id": "suite::failed[a]", "outcome": "failed", "seconds": 1.5},
        {"id": "suite::broken", "outcome": "error", "seconds": 1.5},
        {"id": "suite::skipped", "outcome": "skipped", "seconds": 1.5},
    ]
    assert evidence.cases(tmp_path / "absent.xml") == []


def test_image_source_comes_from_image_metadata(evidence: ModuleType) -> None:
    inspected: dict[str, Any] = {
        "Id": "sha256:image",
        "RepoDigests": ["registry.example/heartwood@sha256:pushed"],
        "Config": {"Labels": {"org.opencontainers.image.revision": _COMMIT}},
    }

    assert evidence.image_source(inspected, {"application": {}}) == {
        "revision": _COMMIT,
        "repo_digests": ["registry.example/heartwood@sha256:pushed"],
        "build_records": {"application": {}},
    }
    inspected["Config"]["Labels"] = None
    assert evidence.image_source(inspected, {})["revision"] is None


def _other_label(manifest: dict[str, Any]) -> None:
    manifest["image_sources"]["worker"]["revision"] = _OTHER_COMMIT


def _other_build_record(manifest: dict[str, Any]) -> None:
    manifest["image_sources"]["runtime"]["build_records"]["application"]["commit"] = _OTHER_COMMIT


def _source_moved_after_image_build(manifest: dict[str, Any]) -> None:
    manifest["source"]["commit"] = _OTHER_COMMIT


def _reduced_selection(manifest: dict[str, Any]) -> None:
    del manifest["cases"][0]


def _added_case(manifest: dict[str, Any]) -> None:
    manifest["cases"].append({"id": "suite::unreviewed", "outcome": "passed", "seconds": 0.0})


def _repeated_case(manifest: dict[str, Any]) -> None:
    manifest["cases"][0] = manifest["cases"][1]


def _skipped_case(manifest: dict[str, Any]) -> None:
    manifest["cases"][0]["outcome"] = "skipped"


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (_other_label, "the worker image was not built from the source commit"),
        (_other_build_record, "the runtime image was not built from the source commit"),
        (_source_moved_after_image_build, "the worker image was not built from the source commit"),
        (
            lambda manifest: manifest["source"].update(tree_clean=False),
            "the source tree differs from its commit",
        ),
        (_reduced_selection, "the executed cases differ from the expected set"),
        (_added_case, "the executed cases differ from the expected set"),
        (_repeated_case, "the executed cases differ from the expected set"),
        (_skipped_case, "not every case passed"),
        (lambda manifest: manifest.update(returncode=1), "the test command failed"),
        (
            lambda manifest: manifest.update(network_retained=True),
            "the qualification network retained attached resources",
        ),
        (
            lambda manifest: manifest.update(qualification_sources_unchanged=False),
            "qualification sources changed during the run",
        ),
    ],
)
def test_only_an_exact_complete_run_qualifies_its_commit(
    evidence: ModuleType, change: Callable[[dict[str, Any]], None], reason: str
) -> None:
    manifest = _manifest(evidence)
    assert evidence.failures(manifest) == []

    change(manifest)

    assert reason in evidence.failures(manifest)


def _package(evidence: ModuleType, directory: Path, manifest: dict[str, Any]) -> None:
    case = directory / "test_case[a]"
    (case / "private").mkdir(parents=True)
    (case / "evidence.json").write_text("{}\n")
    (case / "private" / "credential").write_text("generated")
    (directory / "requested-model-profile.json").write_text("{}\n")
    _write_report(
        directory / "junit.xml",
        dict.fromkeys(sorted(evidence.EXPECTED_CASES[_KIND])),
    )
    manifest = manifest | {"failures": evidence.failures(manifest), "passed": True}
    (directory / "qualification.json").write_text(json.dumps(manifest))
    evidence.write_checksums(directory)


def test_checksums_cover_only_retained_evidence(evidence: ModuleType, tmp_path: Path) -> None:
    _package(evidence, tmp_path, _manifest(evidence))

    listed = {line[66:]: line[:64] for line in (tmp_path / "SHA256SUMS").read_text().splitlines()}

    assert set(listed) == {"junit.xml", "qualification.json", "test_case[a]/evidence.json"}
    assert listed["test_case[a]/evidence.json"] == hashlib.sha256(b"{}\n").hexdigest()
    assert evidence.verify(tmp_path, _COMMIT) == []


def _rewrite_manifest(directory: Path, change: Callable[[dict[str, Any]], None]) -> None:
    path = directory / "qualification.json"
    manifest = json.loads(path.read_text())
    change(manifest)
    path.write_text(json.dumps(manifest))


def test_verification_rejects_changed_or_overstated_evidence(
    evidence: ModuleType, tmp_path: Path
) -> None:
    _package(evidence, tmp_path, _manifest(evidence))
    case_evidence = tmp_path / "test_case[a]" / "evidence.json"

    assert evidence.verify(tmp_path, _OTHER_COMMIT) == [
        "the evidence describes another source commit"
    ]

    case_evidence.write_text('{"changed": true}\n')
    assert evidence.verify(tmp_path) == [
        "test_case[a]/evidence.json is missing or differs from SHA256SUMS"
    ]
    case_evidence.unlink()
    assert evidence.verify(tmp_path) == [
        "test_case[a]/evidence.json is missing or differs from SHA256SUMS"
    ]

    # A consistently re-hashed package still cannot claim more than its own records show.
    _rewrite_manifest(tmp_path, _other_label)
    evidence.write_checksums(tmp_path)
    assert evidence.verify(tmp_path) == ["the worker image was not built from the source commit"]

    _rewrite_manifest(
        tmp_path, lambda manifest: manifest["image_sources"]["worker"].update(revision=_COMMIT)
    )
    _rewrite_manifest(tmp_path, _reduced_selection)
    evidence.write_checksums(tmp_path)
    assert evidence.verify(tmp_path) == [
        "the recorded cases differ from the test report",
        "the executed cases differ from the expected set",
    ]

    (tmp_path / "SHA256SUMS").write_text(f"{hashlib.sha256(b'outside').hexdigest()}  ../outside\n")
    (tmp_path.parent / "outside").write_text("outside")
    assert evidence.verify(tmp_path) == [
        "../outside is missing or differs from SHA256SUMS",
        "SHA256SUMS does not cover the manifest and test report",
    ]


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _reviewed_inputs(tmp_path: Path) -> tuple[Path, Path]:
    candidate = _ROOT / "images/worker/candidate"
    gateway_manifest = json.loads((candidate / "gateway.json").read_text())
    artifacts = tmp_path / "artifacts"
    prebuilt = artifacts / "deploy/docker/.build/prebuilt-binaries/amd64/openshell-gateway"
    prebuilt.parent.mkdir(parents=True)
    (artifacts / "bin").mkdir()
    for path in (artifacts / "bin/openshell", artifacts / "bin/openshell-gateway", prebuilt):
        path.write_text("synthetic binary")
    dockerfile = artifacts / "deploy/docker/Dockerfile.gateway"
    dockerfile.write_text("synthetic Dockerfile")
    (artifacts / "gateway-build-record.json").write_text(
        json.dumps(
            {
                "manifest_sha256": _digest(candidate / "gateway.json"),
                "builder_sha256": _digest(_ROOT / "deploy/build-worker-candidate.py"),
                "source_revision": gateway_manifest["source"]["revision"],
                "version": gateway_manifest["version"],
                "architecture": "amd64",
                "binaries": {
                    name: _digest(artifacts / "bin" / name)
                    for name in ("openshell", "openshell-gateway")
                },
                "dockerfile_sha256": _digest(dockerfile),
            }
        )
    )
    records_manifest = json.loads((candidate / "records.json").read_text())
    records = tmp_path / "records"
    (records / "bin").mkdir(parents=True)
    names = (
        "bin/minio",
        "bin/mc",
        "LICENSE.minio",
        "LICENSE.mc",
        "minio-source.tar.gz",
        "mc-source.tar.gz",
    )
    for name in names:
        (records / name).write_text("synthetic records artifact")
    (records / "records-build-record.json").write_text(
        json.dumps(
            {
                "purpose": "qualification-only",
                "architecture": "amd64",
                "manifest_sha256": _digest(candidate / "records.json"),
                "builder_sha256": _digest(_ROOT / "deploy/build-records-fixture.py"),
                "builder_image": records_manifest["builder"],
                "sources": records_manifest["sources"],
                "files": {name: _digest(records / name) for name in names},
            }
        )
    )
    return artifacts, records


@pytest.mark.parametrize(
    ("image_revision", "omitted_cases", "failure"),
    [
        (_COMMIT, 0, None),
        (_OTHER_COMMIT, 0, "the worker image was not built from the source commit"),
        (_COMMIT, 1, "the executed cases differ from the expected set"),
    ],
)
def test_runner_binds_its_result_to_the_images_own_source(
    evidence: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    image_revision: str,
    omitted_cases: int,
    failure: str | None,
) -> None:
    spec = importlib.util.spec_from_file_location(
        "native_qualification", _ROOT / "deploy/qualify-native-worker.py"
    )
    assert spec is not None
    assert spec.loader is not None
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    artifacts, records = _reviewed_inputs(tmp_path)
    output = tmp_path / "output"
    executed = sorted(evidence.EXPECTED_CASES[_KIND])[omitted_cases:]

    def check_output(arguments: list[str], **_options: object) -> str:
        if arguments[0] == "git":
            return _COMMIT + "\n" if "rev-parse" in arguments else ""
        if arguments[1:3] == ["image", "inspect"]:
            # The tag is the same for every revision; only image metadata identifies the source.
            labels = {"org.opencontainers.image.revision": image_revision}
            return json.dumps(
                [{"Id": "sha256:" + arguments[3], "RepoDigests": [], "Config": {"Labels": labels}}]
            )
        if "--entrypoint" in arguments:
            return json.dumps({"sdk": {"wheels": {}}, "application": {"commit": image_revision}})
        if arguments[1] == "info":
            host = {"kernel": "6", "os": "linux", "architecture": "x86_64", "docker_version": "29"}
            return json.dumps(host)
        if arguments[1:3] == ["network", "inspect"]:
            owner = {"heartwood-qualification-owner": arguments[3].removesuffix("-network")}
            return json.dumps([{"Id": "network", "Labels": owner, "Containers": {}}])
        return "0\n"

    def run(arguments: list[str], **_options: object) -> SimpleNamespace:
        if "--iidfile" in arguments:
            Path(arguments[arguments.index("--iidfile") + 1]).write_text("sha256:built")
        for argument in arguments:
            if argument.startswith("--junitxml="):
                _write_report(
                    Path(argument.removeprefix("--junitxml=")),
                    dict.fromkeys(executed),
                )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(runner.subprocess, "check_output", check_output)
    monkeypatch.setattr(runner.subprocess, "run", run)

    if failure is None:
        runner.qualify("heartwood-worker:tag", "heartwood-runtime:tag", artifacts, records, output)
    else:
        with pytest.raises(RuntimeError, match=failure):
            runner.qualify(
                "heartwood-worker:tag", "heartwood-runtime:tag", artifacts, records, output
            )

    manifest = json.loads((output / "qualification.json").read_text())
    assert manifest["source"] == {"commit": _COMMIT, "tree_clean": True}
    assert manifest["image_sources"]["worker"]["revision"] == image_revision
    assert manifest["invocation"]["worker_image"] == "heartwood-worker:tag"
    assert manifest["started_at"] <= manifest["finished_at"]
    assert manifest["started_at"].endswith("+00:00")
    assert [case["id"] for case in manifest["cases"]] == executed
    assert manifest["passed"] is (failure is None)
    assert (failure in evidence.verify(output, _COMMIT)) is (failure is not None)
    if failure is None:
        assert evidence.verify(output, _COMMIT) == []


def test_candidate_workflow_retains_and_attests_the_checksummed_evidence(
    evidence: ModuleType,
) -> None:
    yaml = importlib.import_module("yaml")
    workflows = _ROOT / ".github/workflows"
    candidate = yaml.load((workflows / "worker-candidate.yml").read_text(), Loader=yaml.BaseLoader)
    jobs = candidate["jobs"]
    steps = {step["name"]: step for step in jobs["native"]["steps"]}
    retained = steps["Retain synthetic qualification evidence"]

    assert set(candidate["on"]) == {"workflow_call", "workflow_dispatch"}
    assert set(retained["with"]["path"].split()) == {
        "test-results/native-qualification/" + pattern
        for pattern in (*evidence.RETAINED, "SHA256SUMS")
    }
    assert retained["if"] == "always()"
    assert steps["Attest the evidence checksums"] == {
        "name": "Attest the evidence checksums",
        "if": "github.event_name != 'pull_request'",
        "uses": "actions/attest@v4",
        "with": {"subject-path": "test-results/native-qualification/SHA256SUMS"},
    }
    assert '--commit "${GITHUB_SHA}"' in steps["Verify the evidence for this commit"]["run"]
    # Only an unchanged pull request may skip the builds; every other event qualifies.
    selection = jobs["scope"]["steps"][1]["run"]
    assert selection.startswith('required=true\nif [[ "${EVENT_NAME}" == "pull_request" ]]; then')
    for job in ("gateway", "build", "records"):
        assert jobs[job]["needs"] == "scope"
        assert jobs[job]["if"] == "needs.scope.outputs.required == 'true'"
    assert jobs["native"]["needs"] == ["gateway", "build", "records"]
    assert "if" not in jobs["native"]
    for entrypoint in ("main-validation.yml", "pull-request-validation.yml"):
        validation = yaml.load((workflows / entrypoint).read_text(), Loader=yaml.BaseLoader)
        assert validation["jobs"]["native-qualification"] == {
            "name": "Native Worker Qualification",
            "permissions": jobs["native"]["permissions"],
            "uses": "./.github/workflows/worker-candidate.yml",
        }
        assert "native-qualification" in validation["jobs"]["release-ready"]["needs"]
