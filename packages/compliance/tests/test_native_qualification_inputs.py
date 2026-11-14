# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Reject replaced native artifacts before granting a controller Docker access."""

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "changed",
    [
        "manifest",
        "builder",
        "binary",
        "packaging",
        "missing-cli",
        "records-manifest",
        "records-builder",
        "records-binary",
        "records-license",
        "records-source",
        "records-missing-file",
        "records-architecture",
        "records-path",
    ],
)
def test_substituted_native_artifacts_are_rejected_before_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    spec = importlib.util.spec_from_file_location(
        "native_qualification",
        Path(__file__).resolve().parents[3] / "deploy/qualify-native-worker.py",
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = tmp_path / "repository"
    manifest_path = root / "images/worker/candidate/gateway.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(json.dumps({"source": {"revision": "reviewed"}, "version": "1+test"}))
    builder = root / "deploy/build-worker-candidate.py"
    builder.parent.mkdir()
    builder.write_text("synthetic reviewed builder")
    artifacts = tmp_path / "artifacts"
    binaries = artifacts / "bin"
    binaries.mkdir(parents=True)
    for name in ("openshell", "openshell-gateway"):
        (binaries / name).write_text("synthetic verified binary")
    packaging = artifacts / "deploy/docker"
    prebuilt = packaging / ".build/prebuilt-binaries/amd64/openshell-gateway"
    prebuilt.parent.mkdir(parents=True)
    prebuilt.write_bytes((binaries / "openshell-gateway").read_bytes())
    dockerfile = packaging / "Dockerfile.gateway"
    dockerfile.write_text("synthetic reviewed Dockerfile")

    def digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    binaries_sha256 = {path.name: digest(path) for path in binaries.iterdir()}
    record = {
        "manifest_sha256": digest(manifest_path),
        "builder_sha256": digest(builder),
        "source_revision": "reviewed",
        "version": "1+test",
        "architecture": "amd64",
        "binaries": binaries_sha256,
        "dockerfile_sha256": digest(dockerfile),
    }
    records = tmp_path / "records"
    (records / "bin").mkdir(parents=True)
    files = (
        "bin/minio",
        "bin/mc",
        "LICENSE.minio",
        "LICENSE.mc",
        "minio-source.tar.gz",
        "mc-source.tar.gz",
    )
    for name in files:
        (records / name).write_text("synthetic verified records artifact")
    records_manifest_path = manifest_path.with_name("records.json")
    records_manifest = {"builder": "go@sha256:reviewed", "sources": {"minio": "reviewed"}}
    records_manifest_path.write_text(json.dumps(records_manifest))
    records_builder = builder.with_name("build-records-fixture.py")
    records_builder.write_text("synthetic reviewed records builder")
    records_build = {
        "purpose": "qualification-only",
        "architecture": "amd64",
        "manifest_sha256": digest(records_manifest_path),
        "builder_sha256": digest(records_builder),
        "builder_image": records_manifest["builder"],
        "sources": records_manifest["sources"],
        "files": {name: digest(records / name) for name in files},
    }
    if changed == "missing-cli":
        del binaries_sha256["openshell"]
    elif changed == "records-missing-file":
        del records_build["files"]["bin/minio"]
    elif changed == "records-architecture":
        records_build["architecture"] = "arm64"
    elif changed == "records-path":
        records_build["files"]["../outside"] = "substituted"
    else:
        target = {
            "manifest": manifest_path,
            "builder": builder,
            "binary": binaries / "openshell",
            "packaging": prebuilt,
            "records-manifest": records_manifest_path,
            "records-builder": records_builder,
            "records-binary": records / "bin/minio",
            "records-license": records / "LICENSE.mc",
            "records-source": records / "mc-source.tar.gz",
        }[changed]
        target.write_text("{}" if changed.endswith("manifest") else "substituted content")
    (artifacts / "gateway-build-record.json").write_text(json.dumps(record))
    (records / "records-build-record.json").write_text(json.dumps(records_build))
    monkeypatch.setattr(module, "ROOT", root)
    monkeypatch.setattr(
        module.subprocess, "check_output", lambda *_args, **_kwargs: pytest.fail("contacted Docker")
    )
    monkeypatch.setattr(
        module.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("started a container")
    )
    with pytest.raises(ValueError, match=r"manifest|digest|packaging|artifacts"):
        module.qualify("worker", "runtime", artifacts, records, tmp_path / "output")
    assert not (tmp_path / "output").exists()
