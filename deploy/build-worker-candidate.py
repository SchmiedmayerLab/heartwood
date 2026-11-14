# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Build maintained dependency wheels and a gateway for native worker qualification."""

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


def _source(
    source: dict[str, Any],
    staging: Path,
    *,
    cache: Path | None = None,
    limit: int = _MAX_ARCHIVE,
) -> Path:
    """Extract only digest-verified upstream bytes into an isolated build directory."""
    cached = cache / (source["sha256"] + ".tar.gz") if cache is not None else None
    if cached is not None and cached.exists():
        archive = _verified(cached, source["sha256"])
    else:
        with urllib.request.urlopen(source["url"], timeout=60) as response:
            archive = response.read(limit + 1)
        if len(archive) > limit or _digest(archive) != source["sha256"]:
            raise ValueError("Candidate source digest mismatch")
        if cached is not None:
            # Concurrent builders may cache identical verified content.
            with TemporaryDirectory(dir=cache) as temporary:
                download = Path(temporary) / "archive"
                download.write_bytes(archive)
                download.replace(cached)
    if len(archive) > limit:
        raise ValueError("Candidate source exceeds its size limit")
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
        bundle.extractall(staging, filter="data")
    checkout = staging / str(source["directory"])
    if not checkout.is_dir():
        raise ValueError("Candidate archive root missing")
    return checkout


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
    if manifest["schema_version"] != 1 or manifest["purpose"] != "maintained-dependencies":
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
            checkout = _source(source, staging / name)
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
        "purpose": manifest["purpose"],
        "manifest_sha256": _digest(manifest_bytes),
        "python": manifest["python"],
        "uv": manifest["uv"],
        "wheels": {path.name: _digest(path.read_bytes()) for path in wheels},
    }
    if report["wheels"] != manifest["wheels_sha256"]:
        raise ValueError("Built dependency wheels differ from their reviewed digests")
    (output / "build-record.json").write_text(json.dumps(report, indent=2) + "\n")


def bootstrap(output: Path, uv: str) -> None:
    """Build missing dependencies or verify an existing release-owned wheel directory."""
    manifest, manifest_bytes, _ = _configuration(uv)
    if output.is_symlink():
        raise ValueError("Runtime dependency directory must not be a symlink")
    if not output.exists():
        build(output, uv)
        return
    expected = manifest["wheels_sha256"]
    actual = {path.name: _digest(path.read_bytes()) for path in output.glob("*.whl")}
    if actual != expected:
        raise ValueError(
            "Runtime wheel digests differ from the source manifest; use a clean checkout"
        )
    record = json.loads((output / "build-record.json").read_text())
    if record["manifest_sha256"] != _digest(manifest_bytes) or record["wheels"] != expected:
        raise ValueError("Runtime dependency provenance differs from the source manifest")


def _gateway_configuration() -> tuple[dict[str, Any], bytes]:
    manifest_bytes = (_CONFIG / "gateway.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    if manifest["schema_version"] != 1 or manifest["purpose"] != "qualification-only":
        raise ValueError("Unsupported gateway candidate manifest")
    for reference in manifest["images"].values():
        if not re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", reference):
            raise ValueError("Gateway image inputs must use immutable digests")
    for patch in manifest["patches"]:
        _verified(_CONFIG / patch["path"], patch["sha256"])
    return manifest, manifest_bytes


def _gateway_version(checkout: Path, version: str) -> None:
    """Stamp only local workspace package versions, preserving locked dependencies."""
    metadata = checkout / "Cargo.toml"
    lock = checkout / "Cargo.lock"
    content = metadata.read_text()
    locked = lock.read_text()
    packages = tomllib.loads(locked)["package"]
    local = [package for package in packages if package["version"] == "0.0.0"]
    if (
        not local
        or content.count('version = "0.0.0"') != 1
        or any(
            "source" in package or not package["name"].startswith("openshell-") for package in local
        )
        or locked.count('version = "0.0.0"') != len(local)
    ):
        raise ValueError("Gateway workspace versions differ from the pinned source")
    metadata.write_text(content.replace('version = "0.0.0"', f'version = "{version}"'))
    lock.write_text(locked.replace('version = "0.0.0"', f'version = "{version}"'))


def build_gateway(output: Path, cache: Path) -> None:
    """Build a local gateway image without modifying or publishing upstream checkouts."""
    manifest, manifest_bytes = _gateway_configuration()
    builder_digest = _digest(Path(__file__).read_bytes())
    output, cache = output.resolve(), cache.resolve()
    if "," in str(output) or "," in str(cache):
        raise ValueError("Docker build paths must not contain commas")
    if output.is_relative_to(cache) or cache.is_relative_to(output):
        raise ValueError("Gateway output and cache must be separate directories")
    architecture = subprocess.check_output(
        ["docker", "info", "--format", "{{.OSType}}/{{.Architecture}}"], text=True
    ).strip()
    arch = {
        "linux/aarch64": "arm64",
        "linux/arm64": "arm64",
        "linux/x86_64": "amd64",
        "linux/amd64": "amd64",
    }.get(architecture)
    if arch is None:
        raise ValueError("Gateway qualification requires a Linux amd64 or arm64 Docker engine")
    output.mkdir(parents=True, exist_ok=False)
    for directory in (cache / "archives", cache / "cargo", cache / arch / "target"):
        directory.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="gateway-source-", dir=output) as temporary:
        staging = Path(temporary)
        checkout = _source(manifest["source"], staging / "source", cache=cache / "archives")
        env = {**os.environ, "GIT_CEILING_DIRECTORIES": str(staging)}
        for patch in manifest["patches"]:
            path = str(_CONFIG / patch["path"])
            subprocess.run(["git", "apply", "--check", path], cwd=checkout, env=env, check=True)
            subprocess.run(["git", "apply", path], cwd=checkout, env=env, check=True)
        _gateway_version(checkout, manifest["version"])
        cmake = _source(
            manifest["cmake"][arch],
            staging / "cmake",
            cache=cache / "archives",
            limit=128 * 1024 * 1024,
        )
        cli = _source(manifest["cli"][arch], staging / "cli", cache=cache / "archives")
        build = [
            "docker",
            "run",
            "--rm",
            "--label",
            f"heartwood-qualification-owner={staging.name}",
            "--label",
            "heartwood-qualification-kind=gateway-build",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "1024",
            "--memory",
            "4g",
            "--memory-swap",
            "5g",
            "--cpus",
            "4",
            "--mount",
            f"type=bind,src={checkout},dst=/source,readonly",
            "--mount",
            f"type=bind,src={cmake},dst=/cmake,readonly",
            "--mount",
            f"type=bind,src={cache / 'cargo'},dst=/cargo",
            "--mount",
            f"type=bind,src={cache / arch / 'target'},dst=/target",
            "--env",
            "CARGO_HOME=/cargo",
            "--env",
            "CARGO_TARGET_DIR=/target",
            "--env",
            "RUSTUP_TOOLCHAIN=1.95.0",
            "--env",
            "RUSTUP_AUTO_INSTALL=0",
            "--env",
            "GIT_DIR=/nonexistent",
            "--env",
            "PATH=/cmake/bin:/usr/local/cargo/bin:/usr/local/bin:/usr/bin:/bin",
            "--workdir",
            "/source",
            manifest["images"]["rust"],
            "cargo",
            "build",
            "--locked",
            "--release",
            "--package",
            "openshell-gateway",
            "--bin",
            "openshell-gateway",
            "--no-default-features",
            "--features",
            "telemetry,compute-driver-docker,vendored-z3",
            "--jobs",
            "1",
        ]
        (output / "build-command.json").write_text(json.dumps(build, indent=2) + "\n")
        subprocess.run(build, check=True)
        binaries = output / "bin"
        binaries.mkdir()
        shutil.copyfile(
            cache / arch / "target/release/openshell-gateway", binaries / "openshell-gateway"
        )
        shutil.copyfile(cli / "openshell", binaries / "openshell")
        for binary in binaries.iterdir():
            binary.chmod(0o555)
        dockerfile = checkout / "deploy/docker/Dockerfile.gateway"
        packaging = output / "deploy/docker"
        prebuilt = packaging / ".build/prebuilt-binaries" / arch
        prebuilt.mkdir(parents=True)
        shutil.copyfile(dockerfile, packaging / "Dockerfile.gateway")
        shutil.copyfile(binaries / "openshell-gateway", prebuilt / "openshell-gateway")
        (prebuilt / "openshell-gateway").chmod(0o555)
        shutil.copyfile(checkout / "LICENSE", output / "LICENSE.openshell")
        record = {
            "purpose": "qualification-only",
            "manifest_sha256": _digest(manifest_bytes),
            "builder_sha256": builder_digest,
            "architecture": arch,
            "version": manifest["version"],
            "build": build,
            "source_revision": manifest["source"]["revision"],
            "cargo_lock_sha256": _digest((checkout / "Cargo.lock").read_bytes()),
            "dockerfile_sha256": _digest(dockerfile.read_bytes()),
            "binaries": {
                binary.name: _digest(binary.read_bytes()) for binary in binaries.iterdir()
            },
        }
    subprocess.run(
        [
            "docker",
            "buildx",
            "build",
            "--load",
            "--file",
            str(packaging / "Dockerfile.gateway"),
            "--build-arg",
            "GATEWAY_BASE_IMAGE=" + manifest["images"]["gateway_base"],
            "--iidfile",
            str(output / "image-id"),
            str(output),
        ],
        check=True,
    )
    image = (output / "image-id").read_text().strip()
    version = subprocess.check_output(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            image,
            "--version",
        ],
        text=True,
    ).strip()
    if version != "openshell-gateway " + manifest["version"]:
        raise ValueError("Built gateway reports an unexpected version")
    record.update(image_id=image, reported_version=version)
    (output / "gateway-build-record.json").write_text(json.dumps(record, indent=2) + "\n")


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
    parser.add_argument("--output", type=Path, help="New artifact output directory")
    targets = parser.add_mutually_exclusive_group()
    targets.add_argument(
        "--bootstrap", action="store_true", help="Prepare or verify the workspace runtime wheels"
    )
    targets.add_argument(
        "--application", action="store_true", help="Build the committed Heartwood application"
    )
    targets.add_argument("--gateway", action="store_true", help="Build the pinned Linux gateway")
    parser.add_argument("--cache", type=Path, help="Gateway archive and Cargo cache directory")
    parser.add_argument("--uv", default="uv", help="Path to the pinned uv executable")
    args = parser.parse_args()
    if args.bootstrap:
        if args.cache is not None:
            parser.error("--cache is only valid with --gateway")
        bootstrap(args.output or _CONFIG.parents[2] / "vendor/runtime-wheels", args.uv)
        return
    if args.output is None:
        parser.error("--output is required unless --bootstrap is selected")
    if args.gateway:
        if args.cache is None:
            parser.error("--gateway requires --cache")
        build_gateway(args.output, args.cache)
        return
    if args.cache is not None:
        parser.error("--cache is only valid with --gateway")
    builder = build_application if args.application else build
    builder(args.output, str(Path(args.uv).resolve()) if "/" in args.uv else args.uv)


if __name__ == "__main__":
    main()
