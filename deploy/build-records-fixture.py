# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Build pinned, synthetic records-test tools without installing a production service."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "images/worker/candidate/records.json"
MAX_ARCHIVE = 50 * 1024 * 1024


def digest(path: Path) -> str:
    """Identify retained source and binary inputs."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_archive(url: str, expected: str, cache: Path) -> bytes:
    """Bound and verify downloads and cached bytes before extraction."""
    path = cache / (expected + ".tar.gz")
    if path.exists():
        with path.open("rb") as stream:
            content = stream.read(MAX_ARCHIVE + 1)
    else:
        with urllib.request.urlopen(url, timeout=60) as response:
            content = response.read(MAX_ARCHIVE + 1)
    if len(content) > MAX_ARCHIVE or hashlib.sha256(content).hexdigest() != expected:
        raise ValueError("Records fixture source digest or size mismatch")
    if not path.exists():
        with TemporaryDirectory(dir=cache) as temporary:
            staged = Path(temporary) / "download"
            staged.write_bytes(content)
            staged.replace(path)
    return content


def build(output: Path, cache: Path) -> None:
    """Compile unchanged sources in an unprivileged, resource-bounded Linux container."""
    manifest_hash = digest(MANIFEST)
    builder_hash = digest(Path(__file__))
    manifest = json.loads(MANIFEST.read_text())
    if (
        manifest["schema_version"] != 1
        or manifest["purpose"] != "qualification-only"
        or set(manifest["sources"]) != {"minio", "mc"}
        or "@sha256:" not in manifest["builder"]
    ):
        raise ValueError("Unsupported records fixture manifest")
    output, cache = output.resolve(), cache.resolve()
    if "," in str(output) or "," in str(cache):
        raise ValueError("Docker paths must not contain commas")
    if output.is_relative_to(cache) or cache.is_relative_to(output):
        raise ValueError("Records output and cache must be separate directories")
    host = subprocess.check_output(
        ["docker", "info", "--format", "{{.OSType}}/{{.Architecture}}"], text=True
    ).strip()
    architecture = {
        "linux/amd64": "amd64",
        "linux/x86_64": "amd64",
        "linux/arm64": "arm64",
        "linux/aarch64": "arm64",
    }.get(host)
    if architecture is None:
        raise ValueError("Records qualification needs a Linux amd64 or arm64 Docker engine")
    output.mkdir(parents=True, exist_ok=False)
    (output / "bin").mkdir()
    for directory in (cache / "archives", cache / "modules", cache / architecture):
        directory.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="records-build-", dir=output) as temporary:
        staging = Path(temporary)
        for name, source in manifest["sources"].items():
            archive = source_archive(source["url"], source["sha256"], cache / "archives")
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
                bundle.extractall(staging, filter="data")
            checkout = staging / source["directory"]
            if not checkout.is_dir():
                raise ValueError("Records source archive root is missing")
            shutil.copyfile(checkout / "LICENSE", output / f"LICENSE.{name}")
            (output / f"{name}-source.tar.gz").write_bytes(archive)
            subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--label",
                    f"heartwood-qualification-owner={staging.name}",
                    "--label",
                    "heartwood-qualification-kind=records-build",
                    "--user",
                    f"{os.getuid()}:{os.getgid()}",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges",
                    "--read-only",
                    "--pids-limit",
                    "512",
                    "--memory",
                    "4g",
                    "--cpus",
                    "4",
                    "--tmpfs",
                    "/tmp:rw,nosuid,nodev,size=1g",
                    "--mount",
                    f"type=bind,src={checkout},dst=/source,readonly",
                    "--mount",
                    f"type=bind,src={cache / 'modules'},dst=/modules",
                    "--mount",
                    f"type=bind,src={cache / architecture},dst=/build-cache",
                    "--mount",
                    f"type=bind,src={output / 'bin'},dst=/output",
                    "--env",
                    "GOMODCACHE=/modules",
                    "--env",
                    "GOCACHE=/build-cache",
                    "--env",
                    "GOTOOLCHAIN=local",
                    "--env",
                    "CGO_ENABLED=0",
                    "--workdir",
                    "/source",
                    "--entrypoint",
                    "go",
                    manifest["builder"],
                    "build",
                    "-mod=readonly",
                    "-trimpath",
                    "-buildvcs=false",
                    "-p=2",
                    "-ldflags=-s -w",
                    "-o",
                    f"/output/{name}",
                    ".",
                ],
                check=True,
            )
    if digest(MANIFEST) != manifest_hash or digest(Path(__file__)) != builder_hash:
        raise ValueError("Records fixture build inputs changed during assembly")
    report = {
        "purpose": "qualification-only",
        "architecture": architecture,
        "manifest_sha256": manifest_hash,
        "builder_sha256": builder_hash,
        "builder_image": manifest["builder"],
        "sources": manifest["sources"],
        "files": {
            str(path.relative_to(output)): digest(path)
            for path in sorted(output.rglob("*"))
            if path.is_file()
        },
        "production_qualified": False,
    }
    (output / "records-build-record.json").write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    args = parser.parse_args()
    build(args.output, args.cache)
