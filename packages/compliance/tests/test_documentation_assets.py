# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Executable contract tests for the public documentation."""

from __future__ import annotations

import json
import re
import struct
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from heartwood.gateway import diagnostic_catalog


def test_documentation_navigation_resolves_to_canonical_sources() -> None:
    site = tomllib.loads(_read("zensical.toml"))
    navigation = site["project"]["nav"]
    paths = tuple(_nav_paths(navigation))

    assert site["project"]["docs_dir"] == "documentation"
    assert site["project"]["extra"]["version"] == {
        "provider": "mike",
        "alias": True,
    }
    assert paths[0] == "index.md"
    assert len(paths) == len(set(paths))
    for relative_path in paths:
        assert (_repo_root() / "documentation" / relative_path).is_file(), relative_path


def test_documentation_has_one_canonical_source_tree() -> None:
    assert not (_repo_root() / "docs").exists()
    assert not (_repo_root() / "design").exists()
    assert not (_repo_root() / "deploy" / "stage_documentation.py").exists()
    assert (_repo_root() / "documentation" / "index.md").is_file()


def test_home_and_readme_expose_an_executable_first_use_path() -> None:
    home = _read("documentation/index.md")
    readme = _read("README.md")
    version = _declared_version()

    assert "[Start Your First Project](start/index.md)" in home
    assert "[Choose a Platform](platforms/index.md)" in home
    assert "[Actions and Audit History](use/actions-audit.md)" in home
    assert f"ghcr.io/schmiedmayerlab/heartwood:{version}" in readme
    assert '-v "$PWD:/workspace"' in readme
    assert "heartwood --interface web" in readme
    assert "--host-loopback-publication" in readme
    assert "http://127.0.0.1:8767/" in readme


def test_first_use_and_interface_guides_share_one_project_contract() -> None:
    guides = "\n".join(
        _read(path)
        for path in (
            "documentation/start/index.md",
            "documentation/start/project.md",
            "documentation/use/index.md",
            "documentation/use/terminal.md",
            "documentation/use/browser.md",
            "documentation/use/notebooks.md",
        )
    )
    assert "current directory" in guides
    assert ".heartwood/" in guides
    assert "heartwood --interface web" in guides
    assert "NotebookSession" in guides
    assert "--workspace" not in guides
    assert "HEARTWOOD_WORKSPACE" not in guides
    assert "heartwood launch" not in guides
    assert "heartwood serve" not in guides


def test_platform_guides_use_current_release_artifacts_and_commands() -> None:
    version = _declared_version()
    containers = _read("documentation/platforms/containers.md")
    terra = _read("documentation/platforms/terra.md")
    carina = _read("documentation/platforms/carina.md")
    combined = "\n".join((containers, terra, carina))

    assert f"heartwood:{version}" in containers
    assert f"heartwood:{version}-terra" in terra
    assert f"heartwood:{version}-terra-gpu-nvidia" in terra
    assert f"releases/download/{version}/heartwood-installer" in carina
    assert "heartwood --interface web" not in terra
    assert "Python 3 (Heartwood)" in terra
    assert "heartwood runtime start --partition dev" in carina
    assert "heartwood launch" not in combined
    assert "heartwood serve" not in combined
    assert "--workspace" not in combined


def test_terra_notebook_is_output_free_and_uses_the_shared_project() -> None:
    notebook = json.loads(_read("documentation/assets/examples/terra-heartwood.ipynb"))
    cells = notebook["cells"]
    combined = "".join("".join(cell["source"]) for cell in cells)

    assert notebook["nbformat"] == 4
    assert "Path.cwd()" in combined
    assert "NotebookSession(session_id=" in combined
    assert "startup_plan" in combined
    assert "project_readiness" in combined
    assert "platform_capabilities" in combined
    assert "pending_approval" in combined
    assert "session.approve(group_id=" in combined
    assert "heartwood --interface web" not in combined
    assert "--workspace" not in combined
    for cell in cells:
        if cell["cell_type"] == "code":
            assert cell["execution_count"] is None
            assert cell["outputs"] == []


def test_web_documentation_uses_generated_theme_aware_desktop_screenshots() -> None:
    browser_guide = _read("documentation/use/browser.md")
    readme = _read("README.md")
    screenshot_documents = "\n".join(
        (
            readme,
            browser_guide,
            _read("documentation/use/actions-audit.md"),
            _read("documentation/use/specialists.md"),
        )
    )
    package = json.loads(_read("packages/webui/package.json"))
    screenshot_script = _read("packages/webui/scripts/smoke-reference-analysis.cjs")
    assets = _repo_root() / "documentation" / "assets" / "screenshots"

    readme_pictures = re.findall(r"<picture>.*?</picture>", readme, flags=re.DOTALL)
    assert readme_pictures
    for picture in readme_pictures:
        assert '<source media="(prefers-color-scheme: dark)"' in picture
        assert "-dark.png" in picture
        assert "-light.png" in picture
    assert screenshot_documents.count("{ .theme-screenshot-light }") == (
        screenshot_documents.count("{ .theme-screenshot-dark }")
    )
    stylesheet = _read("documentation/stylesheets/extra.css")
    assert '[data-md-color-scheme="slate"] .md-typeset .theme-screenshot-light' in stylesheet
    assert '[data-md-color-scheme="slate"] .md-typeset .theme-screenshot-dark' in stylesheet
    assert package["scripts"]["screenshots:docs"].endswith("../../documentation/assets/screenshots")
    assert 'for (const theme of ["light", "dark"])' in screenshot_script
    assert "page.emulateMedia({ colorScheme: theme })" in screenshot_script
    assert "captureApproval: true" in screenshot_script
    for basename in (
        "browser-action-review",
        "browser-action-settings",
        "browser-changes",
        "browser-conversation",
        "browser-files",
        "browser-specialists",
    ):
        assert f'"{basename}.png"' in screenshot_script
        for theme in ("light", "dark"):
            filename = f"{basename}-{theme}.png"
            assert filename in screenshot_documents
            screenshot = assets / filename
            assert screenshot.stat().st_size > 1_000
            width, height = _png_dimensions(screenshot)
            assert width >= 1280
            assert height >= 800
            assert (assets / f"{filename}.license").is_file()


def test_diagnostic_routes_resolve_into_public_documentation() -> None:
    for diagnostic in diagnostic_catalog():
        route, _, anchor = diagnostic.documentation_path.partition("#")
        source = _source_for_route(route)
        assert source.is_file(), diagnostic.documentation_path
        if anchor:
            anchors = {
                _heading_slug(match.group(1))
                for match in re.finditer(
                    r"^#{1,6}\s+(.+?)\s*$",
                    source.read_text(encoding="utf-8"),
                    flags=re.MULTILINE,
                )
            }
            assert anchor in anchors, diagnostic.documentation_path


def test_documentation_builds_directly_from_canonical_sources() -> None:
    validation = _read(".github/workflows/documentation.yml")
    publication = _read(".github/workflows/publish-documentation.yml")
    smoke = _read("deploy/tests/versioned_documentation_smoke.sh")
    combined = "\n".join((validation, publication, smoke))

    assert "stage_documentation" not in combined
    assert "zensical build --clean --strict" in validation
    assert "zensical build --clean --strict" in publication
    assert 'source_path="${repository_root}/documentation/index.md"' in smoke


def test_readme_links_to_published_documentation_channels() -> None:
    readme = _read("README.md")

    assert "https://schmiedmayerlab.github.io/heartwood/" in readme
    assert "https://schmiedmayerlab.github.io/heartwood/preview/" in readme
    assert re.search(r"\]\(documentation/[^)]+\.md\)", readme) is None


def _nav_paths(value: object) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for nested in value.values():
            yield from _nav_paths(nested)
    elif isinstance(value, Sequence):
        for nested in value:
            yield from _nav_paths(nested)


def _source_for_route(route: str) -> Path:
    relative = route.strip("/")
    documentation = _repo_root() / "documentation"
    if not relative:
        return documentation / "index.md"
    index = documentation / relative / "index.md"
    return index if index.is_file() else documentation / f"{relative}.md"


def _heading_slug(heading: str) -> str:
    plain = re.sub(r"[`*_]", "", heading).lower()
    plain = re.sub(r"[^a-z0-9\s-]", "", plain)
    return re.sub(r"[\s-]+", "-", plain).strip("-")


def _png_dimensions(path: Path) -> tuple[int, int]:
    header = path.read_bytes()[:24]
    assert header[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", header[16:24])


def _read(path: str) -> str:
    return (_repo_root() / path).read_text(encoding="utf-8")


def _declared_version() -> str:
    metadata = tomllib.loads(_read("VERSION.toml"))
    version = metadata.get("version")
    assert isinstance(version, str)
    return version


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]
