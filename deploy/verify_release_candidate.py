# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Validate a release version and the check runs for its exact commit."""

from __future__ import annotations

import argparse
import ast
import json
import re
import tomllib
from pathlib import Path

SEMVER = re.compile(
    r"^(?P<major>0|[1-9][0-9]*)\."
    r"(?P<minor>0|[1-9][0-9]*)\."
    r"(?P<patch>0|[1-9][0-9]*)"
    r"(?:-(?P<prerelease>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+(?P<build>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
PYTHON_PRERELEASE = re.compile(
    r"^(?P<label>alpha|a|beta|b|preview|pre|rc|c|dev)(?:\.)?"
    r"(?P<number>0|[1-9][0-9]*)$",
    re.IGNORECASE,
)
PYTHON_PRERELEASE_LABELS = {
    "a": "a",
    "alpha": "a",
    "b": "b",
    "beta": "b",
    "c": "rc",
    "dev": ".dev",
    "pre": "rc",
    "preview": "rc",
    "rc": "rc",
}


def valid_semver(value: str) -> bool:
    """Return whether value is strict Semantic Versioning without a prefix."""
    match = SEMVER.fullmatch(value)
    if match is None:
        return False
    prerelease = match.group("prerelease")
    return prerelease is None or all(
        not (identifier.isdigit() and len(identifier) > 1 and identifier.startswith("0"))
        for identifier in prerelease.split(".")
    )


def is_prerelease(value: str) -> bool:
    """Return whether a valid Semantic Version identifies a prerelease."""
    match = SEMVER.fullmatch(value)
    return valid_semver(value) and match is not None and match.group("prerelease") is not None


def python_package_version(value: str) -> str:
    """Return the canonical PEP 440 equivalent of a supported Semantic Version."""
    match = SEMVER.fullmatch(value)
    if match is None or not valid_semver(value):
        raise ValueError(f"not strict Semantic Versioning: {value}")
    version = ".".join((match.group("major"), match.group("minor"), match.group("patch")))
    prerelease = match.group("prerelease")
    if prerelease is not None:
        prerelease_match = PYTHON_PRERELEASE.fullmatch(prerelease)
        if prerelease_match is None:
            raise ValueError(
                "Python packages support prerelease identifiers alpha.N, beta.N, "
                f"rc.N, preview.N, or dev.N; received {prerelease}"
            )
        label = PYTHON_PRERELEASE_LABELS[prerelease_match.group("label").lower()]
        version += f"{label}{prerelease_match.group('number')}"
    build = match.group("build")
    if build is not None:
        local_identifiers = (
            str(int(identifier)) if identifier.isdigit() else identifier.lower()
            for identifier in re.split(r"[.-]", build)
        )
        version += "+" + ".".join(local_identifiers)
    return version


def _module_version(path: Path) -> str | None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for statement in tree.body:
        if not isinstance(statement, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "__version__"
            for target in statement.targets
        ):
            continue
        value = ast.literal_eval(statement.value)
        if isinstance(value, str):
            return value
    return None


def _check_runs(payload: object) -> list[dict[str, object]]:
    pages: list[object] = payload if isinstance(payload, list) else [payload]
    runs: list[dict[str, object]] = []
    for page in pages:
        if not isinstance(page, dict):
            raise ValueError("check-run payload has an unexpected shape")
        page_runs = page.get("check_runs")
        if not isinstance(page_runs, list):
            raise ValueError("check-run payload has an unexpected shape")
        for run in page_runs:
            if isinstance(run, dict):
                runs.append({str(key): value for key, value in run.items()})
    return runs


def check_status(
    payload: object,
    expected: list[str],
    status_payload: object | None = None,
) -> tuple[list[str], list[str]]:
    """Return incomplete and failed required checks, using the newest run by id."""
    newest: dict[str, dict[str, object]] = {}
    for run in _check_runs(payload):
        name = run.get("name")
        identifier = run.get("id")
        if not isinstance(name, str) or not isinstance(identifier, int):
            continue
        previous_identifier = newest.get(name, {}).get("id")
        if not isinstance(previous_identifier, int) or identifier > previous_identifier:
            newest[name] = run

    contexts: dict[str, str] = {}
    if status_payload is not None:
        if not isinstance(status_payload, dict) or not isinstance(
            status_payload.get("statuses"), list
        ):
            raise ValueError("commit-status payload has an unexpected shape")
        for raw_status in status_payload["statuses"]:
            if not isinstance(raw_status, dict):
                continue
            context = raw_status.get("context")
            state = raw_status.get("state")
            if isinstance(context, str) and isinstance(state, str) and context not in contexts:
                contexts[context] = state

    incomplete: list[str] = []
    failed: list[str] = []
    for name in expected:
        candidate = newest.get(name)
        context_state = contexts.get(name)
        if candidate is None and context_state == "success":
            continue
        if candidate is None and context_state in {"error", "failure"}:
            failed.append(f"{name}: {context_state}")
        elif candidate is None or candidate.get("status") != "completed":
            incomplete.append(name)
        elif candidate.get("conclusion") != "success":
            failed.append(f"{name}: {candidate.get('conclusion', 'unknown')}")
    return incomplete, failed


def source_version_errors(root: Path, version: str) -> list[str]:
    """Return packaged components whose source metadata differs from version."""
    semantic_versions: dict[str, str] = {}
    python_source_versions: dict[str, str] = {}
    python_lock_versions: dict[str, str] = {}
    expected_python_version = python_package_version(version)
    release_metadata = tomllib.loads((root / "VERSION.toml").read_text(encoding="utf-8"))
    declared_version = release_metadata.get("version")
    if isinstance(declared_version, str):
        semantic_versions["VERSION.toml"] = declared_version
    for metadata_path in sorted((root / "packages").glob("*/pyproject.toml")):
        metadata = tomllib.loads(metadata_path.read_text(encoding="utf-8"))
        project = metadata.get("project")
        if isinstance(project, dict) and isinstance(project.get("version"), str):
            python_source_versions[str(metadata_path.relative_to(root))] = project["version"]
    for init_path in sorted((root / "packages").glob("*/src/heartwood/*/__init__.py")):
        module_version = _module_version(init_path)
        if module_version is not None:
            python_source_versions[str(init_path.relative_to(root))] = module_version
    web_path = root / "packages" / "webui" / "package.json"
    web_metadata = json.loads(web_path.read_text(encoding="utf-8"))
    if isinstance(web_metadata, dict) and isinstance(web_metadata.get("version"), str):
        semantic_versions[str(web_path.relative_to(root))] = web_metadata["version"]
    web_lock_path = root / "packages" / "webui" / "package-lock.json"
    web_lock = json.loads(web_lock_path.read_text(encoding="utf-8"))
    if isinstance(web_lock, dict):
        lock_version = web_lock.get("version")
        if isinstance(lock_version, str):
            semantic_versions[str(web_lock_path.relative_to(root))] = lock_version
        lock_packages = web_lock.get("packages")
        if isinstance(lock_packages, dict):
            root_package = lock_packages.get("")
            if isinstance(root_package, dict) and isinstance(root_package.get("version"), str):
                semantic_versions[f"{web_lock_path.relative_to(root)}:root"] = root_package[
                    "version"
                ]
    uv_lock_path = root / "uv.lock"
    uv_lock = tomllib.loads(uv_lock_path.read_text(encoding="utf-8"))
    lock_packages = uv_lock.get("package")
    if isinstance(lock_packages, list):
        for package in lock_packages:
            if not isinstance(package, dict):
                continue
            name = package.get("name")
            package_version = package.get("version")
            if (
                isinstance(name, str)
                and name.startswith("heartwood-")
                and isinstance(package_version, str)
            ):
                python_lock_versions[f"uv.lock:{name}"] = package_version
    if not semantic_versions and not python_source_versions and not python_lock_versions:
        return ["no packaged component versions were found"]
    observed_source_versions = semantic_versions | python_source_versions
    errors = [
        f"{path}: {found}" for path, found in observed_source_versions.items() if found != version
    ]
    errors.extend(
        f"{path}: {found} (expected Python version {expected_python_version})"
        for path, found in python_lock_versions.items()
        if found != expected_python_version
    )
    expected_references = {
        "docs/container-images.md": [f"heartwood:{version}"],
        "docs/carina-cli.md": [
            f"releases/download/{version}/heartwood-installer",
            f"--version {version}",
        ],
        "docs/platform-support.md": [f"Release `{version}`", f"`{version}-terra`"],
        "docs/releases.md": [f"-f version={version}"],
        "docs/terra-jupyter-demo.md": [f"heartwood:{version}-terra"],
    }
    for relative_path, references in expected_references.items():
        content = (root / relative_path).read_text(encoding="utf-8")
        errors.extend(
            f"{relative_path}: missing {reference}"
            for reference in references
            if reference not in content
        )
    return errors


def main() -> int:
    """Run release version and check validation from command-line arguments."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--version-only", action="store_true")
    mode.add_argument("--print-prerelease", action="store_true")
    parser.add_argument("--checks", type=Path)
    parser.add_argument("--statuses", type=Path)
    parser.add_argument("--required-check", action="append", default=[])
    parser.add_argument("--source-root", type=Path)
    args = parser.parse_args()

    if not valid_semver(args.version):
        print(f"release version is not strict Semantic Versioning: {args.version}")
        return 1
    if args.source_root is not None:
        try:
            version_errors = source_version_errors(args.source_root, args.version)
        except (
            OSError,
            SyntaxError,
            UnicodeError,
            ValueError,
            json.JSONDecodeError,
            tomllib.TOMLDecodeError,
        ) as error:
            print(f"unable to evaluate source versions: {error}")
            return 1
        if version_errors:
            print("source versions do not match the release:\n" + "\n".join(version_errors))
            return 1
    if args.version_only:
        print("release version is valid")
        return 0
    if args.print_prerelease:
        print(str(is_prerelease(args.version)).lower())
        return 0
    if args.checks is None or not args.required_check:
        parser.error(
            "--checks and --required-check are required unless a version output mode is used"
        )
    try:
        payload = json.loads(args.checks.read_text(encoding="utf-8"))
        status_payload = (
            json.loads(args.statuses.read_text(encoding="utf-8"))
            if args.statuses is not None
            else None
        )
        incomplete, failed = check_status(
            payload,
            args.required_check,
            status_payload,
        )
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
        print(f"unable to evaluate release checks: {error}")
        return 1
    if failed:
        print("required checks failed:\n" + "\n".join(f"- {name}" for name in failed))
        return 1
    if incomplete:
        print("required checks are incomplete:\n" + "\n".join(f"- {name}" for name in incomplete))
        return 2
    print("all required checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
