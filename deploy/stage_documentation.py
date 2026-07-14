# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Stage canonical repository documentation for the static documentation site."""

from __future__ import annotations

import argparse
import shutil
import tomllib
from pathlib import Path

_DESIGN_FILES = tuple(
    f"{index:02d}-{name}.md"
    for index, name in (
        (1, "overview"),
        (2, "platforms"),
        (3, "architecture"),
        (4, "skills"),
        (5, "security-compliance"),
        (6, "observability-audit"),
        (7, "testing-eval"),
        (8, "development"),
    )
)
_REFERENCE_FILES = (
    "ACRONYMS.md",
    "CONTRIBUTING.md",
    "CONTRIBUTORS.md",
    "LICENSE",
    "NOTICE",
)
_CANONICAL_DIRECTORIES = ("docs", "design", "documentation")
_CANONICAL_FILES = (
    "README.md",
    "AGENTS.md",
    "VERSION.toml",
    *_REFERENCE_FILES,
)
_REPOSITORY_DIRECTORIES = (
    "evals",
    "fixtures",
    "images",
    "packages/audit",
    "packages/cli",
    "packages/compliance",
    "packages/core-adapter",
    "packages/gateway",
    "packages/model-policy",
    "packages/notebook",
    "packages/skills",
    "packages/webui",
    "skills",
)


def declared_version(source_root: Path) -> str:
    """Return the canonical repository version."""
    metadata = tomllib.loads((source_root / "VERSION.toml").read_text(encoding="utf-8"))
    version = metadata.get("version")
    if not isinstance(version, str) or not version:
        msg = "VERSION.toml must declare a non-empty string version"
        raise ValueError(msg)
    return version


def stage_documentation(source_root: Path, output_root: Path) -> None:
    """Create a deterministic site source tree from canonical project documents."""
    source_root = source_root.resolve()
    output_root = output_root.resolve()

    if output_root == source_root or output_root in source_root.parents:
        msg = "documentation output must not replace the repository root or an ancestor"
        raise ValueError(msg)
    protected_directories = tuple(
        (source_root / relative_path).resolve() for relative_path in _CANONICAL_DIRECTORIES
    )
    protected_files = tuple(
        (source_root / relative_path).resolve() for relative_path in _CANONICAL_FILES
    )
    if output_root in protected_files or any(
        output_root == directory or output_root.is_relative_to(directory)
        for directory in protected_directories
    ):
        msg = "documentation output must not replace canonical repository sources"
        raise ValueError(msg)

    version = declared_version(source_root)
    temporary_root = output_root.with_name(f".{output_root.name}.staging")

    shutil.rmtree(temporary_root, ignore_errors=True)
    temporary_root.mkdir(parents=True)

    readme = (source_root / "README.md").read_text(encoding="utf-8")
    repository_base = f"https://github.com/SchmiedmayerLab/heartwood/tree/{version}"
    for relative_path in (*_REPOSITORY_DIRECTORIES, "AGENTS.md"):
        readme = readme.replace(f"]({relative_path})", f"]({repository_base}/{relative_path})")

    contributing = (source_root / "CONTRIBUTING.md").read_text(encoding="utf-8")
    contributing = contributing.replace(
        "](AGENTS.md)",
        f"]({repository_base}/AGENTS.md)",
    )

    (temporary_root / "README.md").write_text(readme, encoding="utf-8")
    (temporary_root / "index.md").write_text('--8<-- "README.md"\n', encoding="utf-8")
    for filename in _REFERENCE_FILES:
        shutil.copy2(source_root / filename, temporary_root / filename)
    (temporary_root / "CONTRIBUTING.md").write_text(contributing, encoding="utf-8")
    shutil.copytree(source_root / "docs", temporary_root / "docs")

    design_root = temporary_root / "design"
    design_root.mkdir()
    for filename in _DESIGN_FILES:
        shutil.copy2(source_root / "design" / filename, design_root / filename)

    stylesheets = temporary_root / "stylesheets"
    stylesheets.mkdir()
    shutil.copy2(
        source_root / "documentation" / "stylesheets" / "extra.css",
        stylesheets / "extra.css",
    )

    shutil.rmtree(output_root, ignore_errors=True)
    temporary_root.replace(output_root)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path.cwd())
    parser.add_argument("--output-root", type=Path, default=Path("build/documentation"))
    return parser


def main() -> None:
    """Stage documentation from command-line arguments."""
    arguments = _parser().parse_args()
    stage_documentation(arguments.source_root, arguments.output_root)


if __name__ == "__main__":
    main()
