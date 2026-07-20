# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Implementation-grounding tests for the public documentation."""

from __future__ import annotations

import json
import re
import struct
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from heartwood.gateway import diagnostic_catalog


def test_documentation_navigation_uses_progressive_disclosure() -> None:
    site = tomllib.loads(_read("zensical.toml"))
    navigation = site["project"]["nav"]
    top_level = [next(iter(section)) for section in navigation]

    assert top_level == [
        "Home",
        "Get Started",
        "Work With Heartwood",
        "Models",
        "Platforms",
        "Operate Heartwood",
        "How Heartwood Works",
        "Reference",
        "Contribute",
    ]
    assert site["project"]["docs_dir"] == "documentation"
    assert site["project"]["extra"]["version"] == {
        "provider": "mike",
        "alias": True,
    }
    for relative_path in _nav_paths(navigation):
        assert (_repo_root() / "documentation" / relative_path).is_file(), relative_path


def test_documentation_has_one_canonical_source_tree() -> None:
    assert not (_repo_root() / "docs").exists()
    assert not (_repo_root() / "design").exists()
    assert not (_repo_root() / "deploy" / "stage_documentation.py").exists()
    assert (_repo_root() / "documentation" / "index.md").is_file()


def test_home_and_readme_present_a_clear_first_use_path() -> None:
    home = _read("documentation/index.md")
    readme = _read("README.md")

    for heading in (
        "## What You Can Do",
        "## Start With Your Environment",
        "## Choose Where the Model Runs",
        "## Understand the Boundary",
        "## Find an Answer",
    ):
        assert heading in home
    assert home.index("## Start With Your Environment") < home.index(
        "## Choose Where the Model Runs"
    )
    for heading in (
        "## What Heartwood Provides",
        "## Quick Start",
        "## Choose a Setup",
        "## Responsible Use",
        "## Contributing",
        "## License",
    ):
        assert heading in readme
    assert "Schmiedmayer Lab at Stanford University" in readme
    assert "model weights or credentials" in readme
    assert "heartwood --interface web" in readme


def test_public_documentation_contains_no_planning_or_process_artifacts() -> None:
    forbidden_organization = "bio" + "design"
    forbidden_phrases = (
        "chain of thought",
        "implementation plan",
        "validation diary",
        "testing diary",
        "this pass replaces",
        "carried over",
    )

    for path in _canonical_documentation_paths():
        content = path.read_text(encoding="utf-8")
        lowered = content.lower()
        assert forbidden_organization not in lowered, path
        for phrase in forbidden_phrases:
            assert phrase not in lowered, f"{phrase!r} found in {path}"
        assert "## Future" not in content
        assert "## Planned" not in content
        assert "TODO" not in content
        assert "TBD" not in content
        assert re.search(r"github\.com/[^\s)]+/issues/\d+", content) is None


def test_documentation_describes_current_product_boundaries() -> None:
    product = _read("documentation/architecture/index.md")
    architecture = _read("documentation/architecture/system.md")
    sessions = _read("documentation/architecture/sessions-audit.md")
    security = _read("documentation/operate/security.md")

    assert "does not fork the OpenHands agent loop" in product
    assert "process current directory" in architecture
    assert "one decision" in sessions
    assert "One Heartwood process should write a given session at a time" in architecture
    assert "does not confer institutional approval" in security


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


def test_model_guides_cover_simple_and_advanced_routes() -> None:
    overview = _read("documentation/models/index.md")
    connections = _read("documentation/models/connections.md")
    choices = _read("documentation/models/choose-managed.md")
    runtime = _read("documentation/models/run-with-heartwood.md")
    offline = _read("documentation/models/offline.md")
    combined = "\n".join((overview, connections, choices, runtime, offline))

    for phrase in (
        "Research environment",
        "OpenAI",
        "Anthropic",
        "Other compatible service",
        "Run with Heartwood",
        "Heartwood-managed model",
        "Other Hugging Face model",
        "heartwood models inspect",
        "heartwood models import",
        "license",
        "download size",
        "context window",
    ):
        assert phrase.lower() in combined.lower()
    assert "no model weights" in combined.lower()
    assert "on this computer" not in combined.lower()
    assert "local model" not in combined.lower()
    assert "not yet supported" in choices.lower()
    assert "github.com/SchmiedmayerLab/heartwood/issues" in choices


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
    assert "heartwood --interface web" in terra
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
    assert "approval_controls" in combined
    assert "--workspace" not in combined
    for cell in cells:
        if cell["cell_type"] == "code":
            assert cell["execution_count"] is None
            assert cell["outputs"] == []


def test_web_documentation_uses_generated_desktop_screenshots() -> None:
    browser_guide = _read("documentation/use/browser.md")
    package = json.loads(_read("packages/webui/package.json"))
    screenshot_script = _read("packages/webui/scripts/smoke-reference-analysis.cjs")
    assets = _repo_root() / "documentation" / "assets" / "screenshots"

    assert "assets/screenshots/browser-conversation.png" in _read("README.md")
    assert "../assets/screenshots/browser-conversation.png" in browser_guide
    assert "../assets/screenshots/browser-action-review.png" in browser_guide
    assert package["scripts"]["screenshots:docs"].endswith("../../documentation/assets/screenshots")
    assert '"browser-conversation.png"' in screenshot_script
    assert '"browser-action-review.png"' in screenshot_script
    assert "captureApproval: true" in screenshot_script
    for filename in ("browser-conversation.png", "browser-action-review.png"):
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


def test_native_installer_defaults_to_current_directory_and_confines_state() -> None:
    installer = _read("deploy/install.sh")

    assert 'root="${PWD}"' in installer
    assert 'installer_state="${root}/.installer"' in installer
    assert 'export HOME="${installer_state}/home"' in installer
    assert 'export TMPDIR="${installer_state}/tmp"' in installer
    assert 'export UV_CACHE_DIR="${installer_state}/cache/uv"' in installer
    assert 'export MAMBA_ROOT_PREFIX="${installer_state}/cache/mamba"' in installer
    assert "export HEARTWOOD_PLATFORM=carina" in installer
    assert 'installer_release="__HEARTWOOD_RELEASE_VERSION__"' in installer
    assert "--minimum-free-gib N" in installer
    assert "--version VERSION" not in installer
    assert "releases/latest/download" not in installer


def test_platform_extension_guide_uses_the_shared_application_contract() -> None:
    guide = _read("documentation/operate/platform-integration.md")

    for path in (
        "images/platforms.toml",
        "images/Dockerfile",
        "docker-bake.hcl",
        "PlatformCapabilities",
        "SessionGateway",
    ):
        assert path in guide
    assert "Do not add a platform-specific agent loop" in guide
    assert "model weights and credentials" in guide
    assert "synthetic" in guide.lower()


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


def _canonical_documentation_paths() -> tuple[Path, ...]:
    return (
        _repo_root() / "README.md",
        *tuple((_repo_root() / "documentation").rglob("*.md")),
    )


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]
