# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Stage canonical repository documentation for the static documentation site."""

from __future__ import annotations

import argparse
import hashlib
import shutil
import tempfile
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
_DOCUMENTATION_FILES = (
    "carina-cli.md",
    "cli-reference.md",
    "container-images.md",
    "deployment.md",
    "getting-started-offline.md",
    "getting-started.md",
    "installation.md",
    "model-connections.md",
    "platform-images.md",
    "platform-support.md",
    "platforms.md",
    "project-state.md",
    "releases.md",
    "terra-jupyter-demo.ipynb",
    "terra-jupyter-demo.md",
    "troubleshooting.md",
    "using-heartwood.md",
    "web-interface.md",
)
_DOCUMENTATION_ASSETS = (
    "web-action-review.png",
    "web-action-review.png.license",
    "web-reference-analysis.png",
    "web-reference-analysis.png.license",
)
_CANONICAL_DIRECTORIES = ("docs", "design", "documentation")
_CANONICAL_FILES = (
    "README.md",
    "AGENTS.md",
    "VERSION.toml",
    *_REFERENCE_FILES,
)
_LEGACY_STAGE_MARKER = ".heartwood-documentation-stage"
_LEGACY_STAGE_MARKER_CONTENT = "heartwood.documentation-stage.v1\n"
_STAGE_MARKER_SUFFIX = ".heartwood-documentation-stage"
_STAGE_MARKER_VERSION = "heartwood.documentation-stage.v2"
_LEGACY_STAGE_ENTRIES = {
    "README.md",
    "design",
    "docs",
    "index.md",
    "stylesheets",
    *_REFERENCE_FILES,
}


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
    output_root.parent.mkdir(parents=True, exist_ok=True)
    temporary_root = Path(tempfile.mkdtemp(prefix=f".{output_root.name}.", dir=output_root.parent))
    try:
        site_home = (source_root / "documentation" / "index.md").read_text(encoding="utf-8")
        repository_base = f"https://github.com/SchmiedmayerLab/heartwood/tree/{version}"
        contributing = (source_root / "CONTRIBUTING.md").read_text(encoding="utf-8")
        contributing = contributing.replace(
            "](AGENTS.md)",
            f"]({repository_base}/AGENTS.md)",
        )
        contributing = contributing.replace(
            "](documentation/index.md)",
            "](index.md)",
        )

        (temporary_root / "index.md").write_text(
            site_home.replace("](../", "]("),
            encoding="utf-8",
        )
        for filename in _REFERENCE_FILES:
            shutil.copy2(source_root / filename, temporary_root / filename)
        (temporary_root / "CONTRIBUTING.md").write_text(contributing, encoding="utf-8")
        documentation_root = temporary_root / "docs"
        documentation_root.mkdir()
        for filename in _DOCUMENTATION_FILES:
            shutil.copy2(source_root / "docs" / filename, documentation_root / filename)
        asset_root = documentation_root / "assets"
        asset_root.mkdir()
        for filename in _DOCUMENTATION_ASSETS:
            shutil.copy2(source_root / "docs" / "assets" / filename, asset_root / filename)

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

        if output_root.exists():
            _assert_replaceable_output(source_root, output_root)
            shutil.rmtree(output_root)
        temporary_root.replace(output_root)
        _stage_marker(output_root).write_text(
            _stage_marker_content(output_root),
            encoding="utf-8",
        )
    finally:
        shutil.rmtree(temporary_root, ignore_errors=True)


def _assert_replaceable_output(source_root: Path, output_root: Path) -> None:
    try:
        marker_content = _stage_marker(output_root).read_text(encoding="utf-8")
    except OSError:
        marker_content = None
    if marker_content == _stage_marker_content(output_root):
        return
    try:
        legacy_marker_content = (output_root / _LEGACY_STAGE_MARKER).read_text(encoding="utf-8")
    except OSError:
        legacy_marker_content = None
    if legacy_marker_content == _LEGACY_STAGE_MARKER_CONTENT:
        return
    legacy_default = source_root / "build" / "documentation"
    if output_root == legacy_default and _looks_like_legacy_stage(output_root):
        return
    raise ValueError("documentation output already exists without a valid Heartwood staging marker")


def _stage_marker(output_root: Path) -> Path:
    return output_root.parent / f".{output_root.name}{_STAGE_MARKER_SUFFIX}"


def _stage_marker_content(output_root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(path for path in output_root.rglob("*") if path.is_file()):
        digest.update(path.relative_to(output_root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as staged_file:
            for chunk in iter(lambda: staged_file.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return f"{_STAGE_MARKER_VERSION}\ntree-sha256={digest.hexdigest()}\n"


def _looks_like_legacy_stage(output_root: Path) -> bool:
    try:
        index = (output_root / "index.md").read_text(encoding="utf-8")
        entries = {entry.name for entry in output_root.iterdir()}
    except OSError:
        return False
    return (
        entries == _LEGACY_STAGE_ENTRIES
        and index == '--8<-- "README.md"\n'
        and all(
            (output_root / directory).is_dir() for directory in ("docs", "design", "stylesheets")
        )
    )


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
