# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Running image observations and the release check against the signed publication record."""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest
import yaml
from test_publish_images import _COMMIT, _record

_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def observed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Feed the collector a synthetic live pod response for each requested selector."""
    monkeypatch.syspath_prepend(str(_ROOT / "reference-platform/tools"))
    import qualification_images as images

    record = _record()
    session = "ab1234"
    pods = {}
    blobs = {}

    def blob(value: dict) -> str:
        raw = json.dumps(value).encode()
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        blobs[digest] = raw
        return digest

    for name, (namespace, selector, container) in images.workloads(session).items():
        item = record["images"][name]
        media = "application/vnd.oci.image.manifest.v1+json"
        child = blob({"schemaVersion": 2, "mediaType": media, "config": {"digest": item["digest"]}})
        item["digest"] = blob(
            {
                "schemaVersion": 2,
                "mediaType": "application/vnd.oci.image.index.v1+json",
                "manifests": [
                    {
                        "mediaType": media,
                        "platform": {"os": "linux", "architecture": arch},
                        "digest": digest,
                    }
                    for arch, digest in [("amd64", child), ("arm64", "sha256:" + "a" * 64)]
                ],
            }
        )
        ref = f"{item['repository']}@{item['digest']}"
        pod = {
            "metadata": {"namespace": namespace, "name": name, "uid": f"uid-{name}"},
            "spec": {"nodeName": "node", "containers": [{"name": container, "image": ref}]},
            "status": {
                "containerStatuses": [
                    {
                        "name": container,
                        "imageID": "docker-pullable://" + item["repository"] + "@" + child,
                        "state": {"running": {}},
                    }
                ]
            },
        }
        pods[(namespace, selector)] = {"items": [pod]}
    record_path = tmp_path / "published-images.json"
    record_path.write_text(json.dumps(record))
    evidence_path = tmp_path / "observed-images.json"

    def response(command: list[str], *, timeout: int) -> bytes:
        assert timeout > 0
        if command[:4] == ["docker", "exec", "node", "crictl"]:
            root = json.loads(blobs[command[-1].split("@")[1]])
            child = json.loads(blobs[root["manifests"][0]["digest"]])
            return json.dumps({"status": {"id": child["config"]["digest"]}}).encode()
        if command[0] == "docker":
            assert command[1:-1] == ["exec", "node", "ctr", "-n", "k8s.io", "content", "get"]
            return blobs[command[-1]]
        if command[4] == "node":
            return b'{"status":{"nodeInfo":{"architecture":"amd64","operatingSystem":"linux"}}}'
        assert command[:5] == ["kubectl", "--context", "kind-test", "get", "pods"]
        return json.dumps(pods[(command[6], command[8])]).encode()

    monkeypatch.setattr(images.subprocess, "check_output", response)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "qualification_images.py",
            "observe",
            "--stage",
            "hosted",
            "--run",
            "17",
            "--attempt",
            "2",
            "--context",
            "kind-test",
            "--session",
            session,
            "--commit",
            _COMMIT,
            "--record",
            str(record_path),
            "--evidence",
            str(evidence_path),
        ],
    )
    images.main()
    return record_path, evidence_path


@pytest.mark.parametrize(
    "mutation",
    [
        "none",
        "missing-role",
        "wrong-role",
        "other-platform",
        "manifest-bytes",
        "wrong-config",
        "wrong-repo",
        "stage",
        "run",
        "attempt",
        "missing-job",
        "failed-job",
        "skipped-job",
        "old-job",
        "other-run",
        "unsigned-receipt",
        "unsigned-record",
        "wrong-id",
        "partial-receipt",
        "wrong-archive-digest",
        "drill-record",
        "wrong-commit",
        "other-workflow",
    ],
)
def test_release_rechecks_observations(
    mutation: str, observed: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import release_image_evidence as release

    record, observation = observed
    monkeypatch.chdir(tmp_path)
    repo = "example-owner/heartwood"
    for key, value in {
        "GITHUB_REPOSITORY": repo,
        "GITHUB_SHA": _COMMIT,
        "QUALIFICATION_RUN": "17",
    }.items():
        monkeypatch.setenv(key, value)
    stages = dict(zip(release.STAGES, ["11", "12", "13"], strict=True))
    receipt = {"commit": _COMMIT, "run": "17", "attempt": "2", "stages": stages.copy()}
    if mutation == "partial-receipt":
        del receipt["stages"]["platform"]

    if mutation in {"none", "drill-record"}:
        drill = tmp_path / "test-results/release-evidence/published" / record.name
        drill.parent.mkdir(parents=True)
        drill.write_bytes(record.read_bytes() if mutation == "none" else b"{}")

    def gh(command: list[str]) -> bytes:
        assert command[0] == "gh"
        args = command[1:]
        if args[:2] == ["run", "download"]:
            assert args[2:6] == ["17", "--repo", repo, "--name"]
            out = Path(args[-1])
            if args[6] == f"stage-receipt-2-{_COMMIT}":
                (out / "stage-receipt.json").write_text(json.dumps(receipt))
            else:
                assert args[6] == f"published-images-{_COMMIT}"
                (out / record.name).write_bytes(record.read_bytes())
            return b""
        if args[:2] == ["attestation", "verify"]:
            assert args[3:] == [
                "--repo",
                repo,
                "--signer-workflow",
                repo + "/" + release.WORKFLOW,
                "--source-ref",
                "refs/heads/main",
                "--source-digest",
                _COMMIT,
            ]
            if (mutation == "unsigned-record" and args[2].endswith(record.name)) or (
                mutation == "unsigned-receipt" and args[2].endswith("stage-receipt.json")
            ):
                raise subprocess.CalledProcessError(1, command)
            return b""
        assert args[0] == "api"
        path = args[-1].removeprefix(f"repos/{repo}/actions/")
        if path == "runs/17":
            data = {
                "id": 17,
                "path": "other" if mutation == "other-workflow" else release.WORKFLOW,
                "head_sha": _COMMIT,
                "head_branch": "main",
                "head_repository": {"full_name": repo},
                "event": "push",
                "conclusion": "success",
                "run_attempt": 2,
            }
        elif path == "runs/17/attempts/2/jobs":
            jobs = []
            for stage in stages:
                if stage == "worker" and mutation == "missing-job":
                    continue
                jobs.append(
                    {
                        "name": "Kubernetes Qualification / "
                        + f"Kubernetes qualification of the {stage} stage",
                        "conclusion": {"failed-job": "failure", "skipped-job": "skipped"}.get(
                            mutation, "success"
                        ),
                        "head_sha": _COMMIT,
                        "run_id": 17,
                        "run_attempt": 1 if mutation == "old-job" else 2,
                    }
                )
            return json.dumps([{"jobs": jobs}]).encode()
        else:
            _, number, *suffix = path.split("/")
            stage = next(stage for stage, value in stages.items() if value == number)
            if suffix:
                assert suffix == ["zip"]
                evidence = json.loads(observation.read_bytes())
                evidence["producer"]["stage"] = stage
                if stage == "worker":
                    row = evidence["observations"]["runtime"]
                    if mutation == "missing-role":
                        del evidence["observations"]["funnel"]
                    elif mutation == "wrong-role":
                        row["image"] = evidence["observations"]["worker"]["image"]
                    elif mutation == "other-platform":
                        row["image_id"] = "sha256:" + "a" * 64
                    elif mutation == "wrong-config":
                        row["config_id"] = "sha256:" + "a" * 64
                    elif mutation == "manifest-bytes":
                        row["manifests"][row["image"].split("@")[1]] += " "
                    elif mutation == "wrong-repo":
                        row["image_id"] = "other@" + row["image_id"].split("@")[1]
                    elif mutation == "wrong-commit":
                        evidence["commit"] = "0" * 40
                    elif mutation in {"stage", "run", "attempt"}:
                        evidence["producer"][mutation] = "other"
                buffer = io.BytesIO()
                with zipfile.ZipFile(buffer, "w") as archive:
                    archive.writestr(zipfile.ZipInfo("observed-images.json"), json.dumps(evidence))
                return buffer.getvalue()
            data = {
                "id": 0 if mutation == "wrong-id" else int(number),
                "expired": False,
                "digest": "sha256:"
                + hashlib.sha256(
                    b""
                    if mutation == "wrong-archive-digest"
                    else gh([*command[:-1], args[-1] + "/zip"])
                ).hexdigest(),
                "name": f"observed-platform-images-{stage}-2-{_COMMIT}",
                "workflow_run": {"id": 18 if mutation == "other-run" else 17, "head_sha": _COMMIT},
            }
        return json.dumps(data).encode()

    monkeypatch.setattr(subprocess, "check_output", gh)
    if mutation == "none":
        release.main()
        files = (tmp_path / "test-results/release-evidence/images").iterdir()
        assert {p.name for p in files} == {
            "platform.json",
            "hosted.json",
            "worker.json",
            "published-images.json",
            "stage-receipt.json",
        }
    else:
        with pytest.raises((ValueError, subprocess.CalledProcessError)):
            release.main()


def test_runtime_accepts_config_of_verified_child(observed: tuple[Path, Path]) -> None:
    from oci_identity import check_runtime

    row = json.loads(observed[1].read_bytes())["observations"]["worker"]
    child = row["image_id"].split("@")[1]
    config = json.loads(row["manifests"][child])["config"]["digest"]
    for identity in (row["image"], child, "containerd://" + config):
        check_runtime(row["image"], identity, "amd64", row["manifests"], row["config_id"])


@pytest.mark.parametrize("statuses", [[], [{"name": "server", "state": {"waiting": {}}}]])
def test_pending_or_missing_container_is_not_an_observation(
    statuses: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(_ROOT / "reference-platform/tools"))
    import qualification_images as images

    with pytest.raises(ValueError, match="expected one running"):
        images.observation(
            [
                {
                    "metadata": {},
                    "spec": {"containers": [{"name": "server"}]},
                    "status": {"containerStatuses": statuses},
                }
            ],
            "server",
        )


def test_signed_receipt_uses_upload_outputs_in_stage_order(tmp_path: Path) -> None:
    workflows = _ROOT / ".github/workflows"
    native = yaml.safe_load((workflows / "native-qualification.yml").read_text())
    kube = yaml.safe_load((workflows / "kubernetes-qualification.yml").read_text())
    cluster = kube["jobs"]["cluster"]
    stages = ("platform", "hosted", "worker")
    assert cluster["outputs"] == {
        stage: "${{ steps.receipt.outputs." + stage + " }}" for stage in stages
    }
    # PyYAML reads the unquoted workflow key "on" as True.
    assert kube[True]["workflow_call"]["outputs"]["observations"]["value"] == ":".join(
        "${{ jobs.cluster.outputs." + stage + " }}" for stage in stages
    )
    producer = next(step for step in cluster["steps"] if step.get("id") == "receipt")
    assert producer["env"] == {"ARTIFACT_ID": "${{ steps.observed.outputs.artifact-id }}"}
    signer = native["jobs"]["stage-receipt"]
    assert signer["needs"] == "kubernetes-qualification"
    assemble, attest, upload = signer["steps"]
    assert assemble["env"]["IDS"] == "${{ needs.kubernetes-qualification.outputs.observations }}"
    assert attest["with"]["subject-path"] == upload["with"]["path"] == "stage-receipt.json"
    for ids in ("11:12:13", "11::13"):
        result = subprocess.run(
            ["bash", "-e", "-c", assemble["run"]],
            cwd=tmp_path,
            env={
                "PATH": os.environ["PATH"],
                "IDS": ids,
                "GITHUB_SHA": _COMMIT,
                "GITHUB_RUN_ID": "17",
                "GITHUB_RUN_ATTEMPT": "2",
            },
            check=False,
        )
        assert (result.returncode == 0) == (ids == "11:12:13")
    assert json.loads((tmp_path / "stage-receipt.json").read_bytes()) == {
        "stages": dict(zip(stages, ("11", "12", "13"), strict=True)),
        "commit": _COMMIT,
        "run": "17",
        "attempt": "2",
    }


def test_single_manifest_uses_its_own_config(observed: tuple[Path, Path]) -> None:
    from oci_identity import check_runtime

    row = json.loads(observed[1].read_bytes())["observations"]["worker"]
    leaf = row["image_id"].removeprefix("docker-pullable://")
    for identity in (leaf, row["config_id"]):
        check_runtime(leaf, identity, "amd64", row["manifests"], row["config_id"])
    with pytest.raises(ValueError, match="outside"):
        check_runtime(leaf, row["image"], "amd64", row["manifests"], row["config_id"])


@pytest.mark.parametrize("architecture", ["amd64", "arm64"])
def test_inventory_collects_each_nodes_images_or_refuses_the_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, architecture: str
) -> None:
    monkeypatch.syspath_prepend(str(_ROOT / "reference-platform/tools"))
    import oci_identity

    references = {
        role: "registry.test/" + role + "@sha256:" + "1" * 64
        for role in ("worker", "replacement_worker", "supervisor", "sandbox")
    }
    source = tmp_path / "images.json"
    source.write_text(json.dumps({"image_references": references}))
    target = tmp_path / "inventory.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "oci_identity",
            "--images",
            str(source),
            "--output",
            str(target),
            "--context",
            "kind-test",
        ],
    )
    nodes = [
        {
            "metadata": {"name": node},
            "status": {"nodeInfo": {"architecture": arch, "operatingSystem": "linux"}},
        }
        for node, arch in [("first", "amd64"), ("second", architecture)]
    ]
    monkeypatch.setattr(subprocess, "check_output", lambda _: json.dumps({"items": nodes}).encode())
    pulls = []
    monkeypatch.setattr(subprocess, "run", lambda args, **_kw: pulls.append(args))
    monkeypatch.setattr(
        oci_identity, "node_identity", lambda node, ref, arch: {"source": [node, ref, arch]}
    )
    if architecture == "arm64":
        with pytest.raises(ValueError, match="Linux amd64"):
            oci_identity.main()
        assert not target.exists()
        assert not pulls
        return
    oci_identity.main()
    expected = {}
    for node in ("first", "second"):
        images = {}
        for role, ref in references.items():
            assert ["docker", "exec", node, "crictl", "pull", ref] in pulls
            images[ref] = {
                "role": "worker" if role == "replacement_worker" else role,
                "source": [node, ref, "amd64"],
            }
        expected[node] = {"platform": "linux/amd64", "images": images}
    assert json.loads(target.read_bytes()) == expected
