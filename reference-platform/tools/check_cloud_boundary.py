# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Keep cloud-specific code and platform code out of the application packages.

The check fails when an application package under `packages/` depends on or imports a
cloud infrastructure SDK, names a cloud infrastructure host, metadata address, GKE
annotation or storage class in its sources, or imports reference platform code.
It also fails when the platform services depend on a Heartwood package other than
`heartwood-schemas`. Exceptions are listed in `ALLOWLIST`, each with its reason; an
exception that no longer matches anything fails the check, so the list stays current.
Research platform adapters and model provider names in model routing are outside its
scope: it matches SDK names, hosts and cluster settings, not provider names.

Usage: `python3 reference-platform/tools/check_cloud_boundary.py [--root PATH]`.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
import tomllib
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

# Cloud infrastructure SDK distributions (PyPI names, normalized) and npm packages.
CLOUD_DISTRIBUTIONS = (
    "aioboto3",
    "aiobotocore",
    "adlfs",
    "azure-*",
    "boto3",
    "botocore",
    "gcloud-aio-*",
    "gcsfs",
    "google-api-core",
    "google-api-python-client",
    "google-auth",
    "google-auth-*",
    "google-cloud-*",
    "s3fs",
)
CLOUD_NPM_PACKAGES = ("@aws-sdk/*", "@azure/*", "@google-cloud/*", "aws-sdk", "googleapis")
# Top-level Python modules those distributions provide.
CLOUD_MODULES = (
    "aioboto3",
    "aiobotocore",
    "adlfs",
    "azure",
    "boto3",
    "botocore",
    "gcloud.aio",
    "gcsfs",
    "google.api_core",
    "google.auth",
    "google.cloud",
    "google.oauth2",
    "googleapiclient",
    "s3fs",
)
PLATFORM_DISTRIBUTION = "heartwood-reference-platform"
PLATFORM_MODULE = "heartwood_platform"
# The only application package the platform services may depend on.
SHARED_DISTRIBUTION = "heartwood-schemas"
SHARED_MODULE = "heartwood.schemas"

# Cloud infrastructure identifiers, matched case-insensitively in package sources.
CLOUD_IDENTIFIERS = re.compile(
    r"""
    (?<![a-z0-9.-])(?:[a-z0-9-]+\.)*(?:googleapis\.com|amazonaws\.com|azure\.com|azure\.net)\b
    | (?<![a-z0-9.-])(?:[a-z0-9-]+\.)+(?:windows\.net|azurecr\.io)\b
    | metadata\.google\.internal
    | 169\.254\.169\.254
    | 169\.254\.170\.2\b
    | fd00:ec2:[0-9a-f:/]*
    | cloud\.google\.com/[a-z0-9./_-]+
    | (?<![a-z0-9.-])[a-z0-9.-]*\.gke\.io\b(?:/[a-z0-9./_-]*)?
    | (?<![a-z0-9.-])[a-z0-9.-]*\.csi\.aws\.com
    | \b(?:standard|premium|balanced)-rwo\b
    | \bpd-(?:standard|ssd|balanced|extreme)\b
    | \bhyperdisk-(?:balanced|extreme|throughput|ml)\b
    """,
    re.IGNORECASE | re.VERBOSE,
)
IMPORT_FUNCTIONS = frozenset({"import_module", "__import__"})


@dataclass(frozen=True)
class Allowance:
    """One accepted finding: a denylisted name or identifier in matching files."""

    path: str
    entry: str
    reason: str


ALLOWLIST = (
    Allowance(
        "packages/gateway/pyproject.toml",
        "botocore",
        "S3 protocol client for any S3-compatible records service",
    ),
    Allowance(
        "packages/gateway/src/*",
        "botocore",
        "S3 protocol client for any S3-compatible records service",
    ),
    Allowance(
        "packages/gateway/src/heartwood/gateway/_mcp_policy.py",
        "fd00:ec2::/32",
        "Refuses research service destinations in a cloud metadata address range",
    ),
)


@dataclass(frozen=True)
class Finding:
    """One boundary violation at a repository-relative location."""

    path: str
    line: int
    entry: str
    message: str

    def __str__(self) -> str:
        """Return the finding as `path:line: message`."""
        return f"{self.path}:{self.line}: {self.message}"


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirement_name(requirement: str) -> str:
    match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", requirement)
    return _normalize(match.group(1)) if match else ""


def _line_of(text: str, needle: str) -> int:
    for number, line in enumerate(text.splitlines(), start=1):
        if needle.lower() in line.lower():
            return number
    return 1


def _declared_requirements(metadata: dict[str, Any]) -> list[str]:
    project = metadata.get("project", {})
    lists = [project.get("dependencies", []), *project.get("optional-dependencies", {}).values()]
    lists.extend(metadata.get("dependency-groups", {}).values())
    # Dependency groups can also include other groups as tables; those add no names.
    return [item for items in lists for item in items if isinstance(item, str)]


def _python_dependencies(path: Path) -> list[tuple[str, int]]:
    text = path.read_text(encoding="utf-8")
    names = {_requirement_name(item) for item in _declared_requirements(tomllib.loads(text))}
    return [(name, _line_of(text, name)) for name in sorted(names - {""})]


def _imported_modules(path: Path, data: bytes | None = None) -> list[tuple[str, int]]:
    """Return absolute module names imported statically or by a literal dynamic import."""
    # Parsing bytes honours a PEP 263 coding declaration.
    tree = ast.parse(path.read_bytes() if data is None else data, filename=str(path))
    modules: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.append((node.module, node.lineno))
            modules.extend((f"{node.module}.{alias.name}", node.lineno) for alias in node.names)
        elif isinstance(node, ast.Call) and node.args:
            function = node.func
            name = function.attr if isinstance(function, ast.Attribute) else None
            if isinstance(function, ast.Name):
                name = function.id
            argument = node.args[0]
            if (
                name in IMPORT_FUNCTIONS
                and isinstance(argument, ast.Constant)
                and isinstance(argument.value, str)
            ):
                modules.append((argument.value, node.lineno))
    return modules


def _matching_module(module: str, candidates: tuple[str, ...]) -> str | None:
    for candidate in candidates:
        if module == candidate or module.startswith(f"{candidate}."):
            return candidate
    return None


def _check_package_metadata(root: Path, package: Path) -> list[Finding]:
    findings: list[Finding] = []
    metadata = package / "pyproject.toml"
    if metadata.is_file():
        relative = metadata.relative_to(root).as_posix()
        for name, line in _python_dependencies(metadata):
            if name == PLATFORM_DISTRIBUTION:
                findings.append(Finding(relative, line, name, f"depends on platform code {name}"))
            elif any(fnmatchcase(name, pattern) for pattern in CLOUD_DISTRIBUTIONS):
                findings.append(Finding(relative, line, name, f"depends on cloud SDK {name}"))
    manifest = package / "package.json"
    if manifest.is_file():
        relative = manifest.relative_to(root).as_posix()
        text = manifest.read_text(encoding="utf-8")
        data = json.loads(text)
        for section in ("dependencies", "devDependencies", "optionalDependencies"):
            for name in data.get(section, {}):
                if any(fnmatchcase(name, pattern) for pattern in CLOUD_NPM_PACKAGES):
                    line = _line_of(text, f'"{name}"')
                    findings.append(Finding(relative, line, name, f"depends on cloud SDK {name}"))
    return findings


def _check_source_file(root: Path, path: Path) -> list[Finding]:
    relative = path.relative_to(root).as_posix()
    data = path.read_bytes()
    findings: list[Finding] = []
    if path.suffix in {".py", ".pyi"}:
        try:
            modules = _imported_modules(path, data)
        except (SyntaxError, ValueError) as error:
            line = getattr(error, "lineno", None) or 1
            return [Finding(relative, line, "", f"cannot be parsed: {error}")]
        reported: set[tuple[int, str]] = set()
        for module, line in modules:
            for entry, message in (
                (_matching_module(module, (PLATFORM_MODULE,)), f"imports {module}"),
                (_matching_module(module, CLOUD_MODULES), f"imports cloud SDK {module}"),
            ):
                # `from google.cloud import storage` is reported once, not per name.
                if entry and (line, entry) not in reported:
                    reported.add((line, entry))
                    findings.append(Finding(relative, line, entry, message))
    # Identifiers are ASCII; Latin-1 maps every byte, so no file is skipped as undecodable.
    text = data.decode("latin-1")
    for number, line_text in enumerate(text.splitlines(), start=1):
        for match in CLOUD_IDENTIFIERS.finditer(line_text):
            identifier = match.group(0)
            findings.append(
                Finding(relative, number, identifier, f"names cloud infrastructure {identifier}")
            )
    return findings


def _check_platform_services(root: Path) -> list[Finding]:
    services = root / "reference-platform" / "services"
    findings: list[Finding] = []
    metadata = services / "pyproject.toml"
    if metadata.is_file():
        relative = metadata.relative_to(root).as_posix()
        for name, line in _python_dependencies(metadata):
            if name.startswith("heartwood-") and name != SHARED_DISTRIBUTION:
                message = f"platform services depend on application package {name}"
                findings.append(Finding(relative, line, name, message))
    for path in sorted((services / "src").rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        for module, line in _imported_modules(path):
            if _matching_module(module, ("heartwood",)) and not _matching_module(
                module, (SHARED_MODULE,)
            ):
                message = f"platform services import application package {module}"
                findings.append(Finding(relative, line, module, message))
    return findings


def check(root: Path, allowlist: tuple[Allowance, ...]) -> list[str]:
    """Return every violation and every unused allowance under a repository root."""
    findings: list[Finding] = []
    for package in sorted(path for path in (root / "packages").iterdir() if path.is_dir()):
        findings.extend(_check_package_metadata(root, package))
        source = package / "src"
        for path in sorted(item for item in source.rglob("*") if item.is_file()):
            if "__pycache__" not in path.parts and "node_modules" not in path.parts:
                findings.extend(_check_source_file(root, path))
    findings.extend(_check_platform_services(root))

    used: set[Allowance] = set()
    errors: list[str] = []
    for finding in findings:
        allowance = next(
            (
                item
                for item in allowlist
                if item.entry == finding.entry and fnmatchcase(finding.path, item.path)
            ),
            None,
        )
        if allowance is None:
            errors.append(str(finding))
        else:
            used.add(allowance)
    errors.extend(
        f"unused allowance for {item.entry} in {item.path}"
        for item in allowlist
        if item not in used
    )
    return errors


def main(argv: list[str] | None = None) -> int:
    """Run the check and print each violation."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="repository root (default: the repository containing this file)",
    )
    arguments = parser.parse_args(argv)
    errors = check(arguments.root.resolve(), ALLOWLIST)
    for error in errors:
        print(error, file=sys.stderr)
    if errors:
        print(f"Cloud boundary check failed with {len(errors)} finding(s).", file=sys.stderr)
        return 1
    print("Cloud boundary check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
