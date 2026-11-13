# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Build pinned, patched dependency wheels for native worker qualification only."""

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
import zipfile
from email.parser import Parser
from pathlib import Path
from tempfile import TemporaryDirectory

_CONFIG = Path(__file__).resolve().parents[1] / "images/worker/candidate"
_MAX_ARCHIVE = 50 * 1024 * 1024


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _verified(path: Path, expected: str) -> bytes:
    data = path.read_bytes()
    if _digest(data) != expected:
        raise ValueError(f"Candidate input digest mismatch: {path.name}")
    return data


def _wheel_identity(path: Path) -> tuple[str, str]:
    with zipfile.ZipFile(path) as wheel:
        entries = [name for name in wheel.namelist() if name.endswith(".dist-info/METADATA")]
        if len(entries) != 1:
            raise ValueError("Candidate wheel metadata is missing or ambiguous")
        metadata = Parser().parsestr(wheel.read(entries[0]).decode("utf-8"), headersonly=True)
        name, version = metadata.get_all("Name", []), metadata.get_all("Version", [])
        if len(name) != 1 or len(version) != 1:
            raise ValueError("Candidate wheel identity is missing or ambiguous")
        license_path = entries[0].removesuffix("METADATA") + "licenses/LICENSE"
        if license_path not in wheel.namelist() or not wheel.read(license_path):
            raise ValueError("Candidate wheel must preserve its upstream license")
    return name[0], version[0]


def build(output: Path, uv: str) -> None:
    """Build new artifacts without changing installed dependencies or source checkouts."""
    manifest_bytes = (_CONFIG / "sources.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    if manifest["schema_version"] != 1 or manifest["purpose"] != "qualification-only":
        raise ValueError("Unsupported worker candidate manifest")
    version = subprocess.check_output([uv, "--version"], text=True).split()[1]
    if version != manifest["uv"]:
        raise ValueError(f"Worker candidate requires uv {manifest['uv']}")
    constraints = _CONFIG / "build-requirements.txt"
    _verified(constraints, manifest["build_requirements_sha256"])
    for source in manifest["sources"].values():
        for patch in source["patches"]:
            _verified(_CONFIG / patch["path"], patch["sha256"])
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    with TemporaryDirectory(prefix="heartwood-worker-build-") as temporary:
        staging = Path(temporary)
        env = {**os.environ, "SOURCE_DATE_EPOCH": str(manifest["source_date_epoch"])}
        # Applying a patch must not discover the caller's surrounding repository.
        env["GIT_CEILING_DIRECTORIES"] = str(staging)
        for name, source in manifest["sources"].items():
            with urllib.request.urlopen(source["url"], timeout=60) as response:
                archive = response.read(_MAX_ARCHIVE + 1)
            if len(archive) > _MAX_ARCHIVE or _digest(archive) != source["sha256"]:
                raise ValueError(f"Candidate source digest mismatch: {name}")
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
                bundle.extractall(staging, filter="data")
            checkout = staging / source["directory"]
            if not checkout.is_dir():
                raise ValueError(f"Candidate archive root missing: {name}")
            for patch in source["patches"]:
                path = str(_CONFIG / patch["path"])
                subprocess.run(["git", "apply", "--check", path], cwd=checkout, env=env, check=True)
                subprocess.run(["git", "apply", path], cwd=checkout, env=env, check=True)
            for package in source["packages"]:
                project = checkout / package["path"]
                if package["original_version"] is not None:
                    metadata = project / "pyproject.toml"
                    old = f'version = "{package["original_version"]}"'
                    content = metadata.read_text()
                    if content.count(old) != 1:
                        raise ValueError("Candidate package version does not match the source")
                    metadata.write_text(content.replace(old, f'version = "{package["version"]}"'))
                    if project != checkout:
                        shutil.copyfile(checkout / "LICENSE", project / "LICENSE")
                build_env = {**env, "SETUPTOOLS_SCM_PRETEND_VERSION": package["version"]}
                subprocess.run(
                    [
                        uv,
                        "build",
                        "--no-config",
                        "--no-sources",
                        "--wheel",
                        "--python",
                        manifest["python"],
                        "--build-constraints",
                        str(constraints),
                        "--require-build-hashes",
                        "--out-dir",
                        str(output),
                        str(project),
                    ],
                    cwd=staging,
                    env=build_env,
                    check=True,
                )
    wheels = sorted(output.glob("*.whl"))
    expected = sum(len(source["packages"]) for source in manifest["sources"].values())
    if len(wheels) != expected:
        raise ValueError("Candidate wheel set is incomplete")
    identities = [_wheel_identity(path) for path in wheels]
    expected_identities = {
        (package["name"], package["version"])
        for source in manifest["sources"].values()
        for package in source["packages"]
    }
    if len(set(identities)) != len(wheels) or set(identities) != expected_identities:
        raise ValueError("Candidate wheel identities do not match the manifest")
    report = {
        "purpose": "qualification-only",
        "manifest_sha256": _digest(manifest_bytes),
        "python": manifest["python"],
        "uv": version,
        "wheels": {path.name: _digest(path.read_bytes()) for path in wheels},
    }
    (output / "build-record.json").write_text(json.dumps(report, indent=2) + "\n")


def main() -> None:
    """Parse the artifact destination and build with the pinned tool version."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New wheel output directory")
    parser.add_argument("--uv", default="uv", help="Path to the pinned uv executable")
    args = parser.parse_args()
    build(args.output, str(Path(args.uv).resolve()) if "/" in args.uv else args.uv)


if __name__ == "__main__":
    main()
