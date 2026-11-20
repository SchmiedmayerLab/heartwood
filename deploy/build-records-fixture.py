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
import re
import shutil
import subprocess
import tarfile
import urllib.request
import zlib
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "images/worker/candidate/records.json"
MAX_ARCHIVE = 50 * 1024 * 1024


def digest(path: Path) -> str:
    """Identify retained source and binary inputs."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_digest(root: Path) -> str:
    """Identify an extracted tree by its paths, kinds, executable bits, contents and links.

    This matches `_tree_digest` in `build-worker-candidate.py`.
    """
    entries: list[tuple[bytes, bytes, bytes]] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        for child in directory.iterdir():
            path = os.fsencode(child.relative_to(root).as_posix())
            if child.is_symlink():
                # The raw target, since `Path.readlink` normalizes it.
                entries.append((path, b"symlink", os.fsencode(os.readlink(child))))  # noqa: PTH115
            elif child.is_dir():
                entries.append((path, b"directory", b""))
                pending.append(child)
            elif child.is_file():
                kind = b"executable" if child.lstat().st_mode & 0o100 else b"file"
                entries.append((path, kind, digest(child).encode()))
            else:
                raise ValueError("Records fixture source contains a special file")
    # NUL cannot occur in a path or link target, so each record parses unambiguously.
    return hashlib.sha256(
        b"".join(b"\0".join(entry) + b"\0" for entry in sorted(entries))
    ).hexdigest()


def source_checkout(source: dict[str, Any], staging: Path, cache: Path) -> tuple[Path, bytes]:
    """Bound downloads and cached bytes, then verify the extracted tree before use.

    GitHub generates revision archives on request and does not keep their bytes stable,
    so the reviewed digest covers the extracted content instead of the archive.
    """
    expected = source["tree_sha256"]
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("Records fixture source digest must be a SHA-256 hex digest")
    path = cache / (expected + ".tar.gz")
    if path.exists():
        with path.open("rb") as stream:
            content = stream.read(MAX_ARCHIVE + 1)
    else:
        with urllib.request.urlopen(source["url"], timeout=60) as response:
            content = response.read(MAX_ARCHIVE + 1)
    if len(content) > MAX_ARCHIVE:
        raise ValueError("Records fixture source exceeds its size limit")
    staging.mkdir(parents=True)
    try:
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as bundle:
            bundle.extractall(staging, filter="data")
    except (tarfile.TarError, EOFError, zlib.error) as error:
        # The data filter also refuses paths and links that leave the staging directory.
        raise ValueError("Records fixture source is unsafe or unreadable") from error
    # The root directory's name follows the repository name, which can change.
    roots = list(staging.iterdir())
    if (
        len(roots) != 1
        or roots[0].is_symlink()
        or not roots[0].is_dir()
        or tree_digest(roots[0]) != expected
    ):
        raise ValueError("Records fixture source digest mismatch")
    checkout = roots[0]
    if not path.exists():
        with TemporaryDirectory(dir=cache) as temporary:
            staged = Path(temporary) / "download"
            staged.write_bytes(content)
            staged.replace(path)
    return checkout, content


def build(output: Path, cache: Path) -> None:
    """Compile unchanged sources in an unprivileged, resource-bounded Linux container."""
    manifest_hash = digest(MANIFEST)
    builder_hash = digest(Path(__file__))
    manifest = json.loads(MANIFEST.read_text())
    if (
        manifest["schema_version"] != 2
        or manifest["purpose"] != "qualification-only"
        or set(manifest["sources"]) != {"versitygw"}
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
            checkout, archive = source_checkout(source, staging / name, cache / "archives")
            for notice in source["notices"]:
                shutil.copyfile(checkout / notice, output / f"{notice}.{name}")
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
                    "sh",
                    manifest["builder"],
                    "-c",
                    # A restored module cache is not rehashed by a build; verify it first.
                    'go mod verify && exec go "$@"',
                    "go",
                    "build",
                    "-mod=readonly",
                    "-trimpath",
                    "-buildvcs=false",
                    "-p=2",
                    "-ldflags=-s -w",
                    "-o",
                    f"/output/{name}",
                    source["package"],
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
