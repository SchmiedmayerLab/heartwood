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
import re
import shutil
import subprocess
import tarfile
import tomllib
import urllib.request
import zipfile
from email.parser import Parser
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

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


def _configuration(uv: str) -> tuple[dict[str, Any], bytes, Path]:
    """Verify the tool and shared build inputs before executing any build."""
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
    return manifest, manifest_bytes, constraints


def _build_wheel(
    project: Path,
    output: Path,
    uv: str,
    manifest: dict[str, Any],
    constraints: Path,
    scm_version: str | None = None,
) -> None:
    env = {**os.environ, "SOURCE_DATE_EPOCH": str(manifest["source_date_epoch"])}
    if scm_version is not None:
        env["SETUPTOOLS_SCM_PRETEND_VERSION"] = scm_version
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
        cwd=project,
        env=env,
        check=True,
    )


def build(output: Path, uv: str) -> None:
    """Build new artifacts without changing installed dependencies or source checkouts."""
    manifest, manifest_bytes, constraints = _configuration(uv)
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
                _build_wheel(project, output, uv, manifest, constraints, package["version"])
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
        "uv": manifest["uv"],
        "wheels": {path.name: _digest(path.read_bytes()) for path in wheels},
    }
    (output / "build-record.json").write_text(json.dumps(report, indent=2) + "\n")


def _application_metadata(project: Path, settings: dict[str, Any]) -> tuple[str, str]:
    metadata = project / "pyproject.toml"
    content = metadata.read_text()
    original = tomllib.loads(content)["project"]
    version = original["version"] + settings["version_suffix"]
    old_version = f'version = "{original["version"]}"'
    if content.count(old_version) != 1:
        raise ValueError("Application version declaration is ambiguous")
    content = content.replace(old_version, f'version = "{version}"')
    replacements = settings["dependency_replacements"]
    names = {item.split("==")[0] for item in replacements}
    for dependency in original.get("dependencies", []):
        if re.split(r"[<>=!~;\[\s]", dependency, maxsplit=1)[0] in names:
            if dependency not in replacements or content.count(f'"{dependency}"') != 1:
                raise ValueError("Application dependency differs from the qualification manifest")
            content = content.replace(f'"{dependency}"', f'"{replacements[dependency]}"')
    metadata.write_text(content)
    return original["name"], version


def build_application(output: Path, uv: str) -> None:
    """Build installable application candidates from the exact committed source tree."""
    manifest, manifest_bytes, constraints = _configuration(uv)
    repository = _CONFIG.parents[2]
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repository, text=True
    ).strip()
    skill_commit = subprocess.check_output(
        ["git", "rev-parse", f"{commit}:vendor/heartwood-skills"], cwd=repository, text=True
    ).strip()
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    with TemporaryDirectory(prefix="heartwood-application-build-") as temporary:
        staging = Path(temporary)
        for checkout, revision, destination in (
            (repository, commit, staging),
            (
                repository / "vendor/heartwood-skills",
                skill_commit,
                staging / "vendor/heartwood-skills",
            ),
        ):
            archive = subprocess.check_output(["git", "archive", revision], cwd=checkout)
            destination.mkdir(parents=True, exist_ok=True)
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
                bundle.extractall(destination, filter="data")
        identities = set()
        for metadata in sorted((staging / "packages").glob("*/pyproject.toml")):
            project = metadata.parent
            identities.add(_application_metadata(project, manifest["application"]))
            shutil.copyfile(staging / "LICENSE.md", project / "LICENSE")
            _build_wheel(project, output, uv, manifest, constraints)
        # The pinned Skill catalog is built unchanged from its own commit.
        project = staging / "vendor/heartwood-skills"
        metadata = tomllib.loads((project / "pyproject.toml").read_text())["project"]
        identities.add((metadata["name"], metadata["version"]))
        _build_wheel(project, output, uv, manifest, constraints)
    wheels = sorted(output.glob("*.whl"))
    actual = [_wheel_identity(path) for path in wheels]
    if len(actual) != len(identities) or set(actual) != identities:
        raise ValueError("Application candidate wheel set does not match its source")
    hashes = {path.name: _digest(path.read_bytes()) for path in wheels}
    requirements = "".join(
        f"{name}=={version} --hash=sha256:{hashes[path.name]}\n"
        for path, (name, version) in zip(wheels, actual, strict=True)
    )
    (output / "application-requirements.txt").write_text(requirements)
    report = {
        "purpose": "qualification-only",
        "commit": commit,
        "skill_commit": skill_commit,
        "manifest_sha256": _digest(manifest_bytes),
        "python": manifest["python"],
        "uv": manifest["uv"],
        "wheels": hashes,
        "requirements_sha256": _digest(requirements.encode()),
    }
    (output / "application-build-record.json").write_text(json.dumps(report, indent=2) + "\n")


def main() -> None:
    """Parse the artifact destination and build with the pinned tool version."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New wheel output directory")
    parser.add_argument(
        "--application", action="store_true", help="Build the committed Heartwood application"
    )
    parser.add_argument("--uv", default="uv", help="Path to the pinned uv executable")
    args = parser.parse_args()
    builder = build_application if args.application else build
    builder(args.output, str(Path(args.uv).resolve()) if "/" in args.uv else args.uv)


if __name__ == "__main__":
    main()
