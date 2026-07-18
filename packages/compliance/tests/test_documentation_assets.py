# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Structural and implementation-grounding tests for public documentation."""

from __future__ import annotations

import importlib.util
import json
import re
import struct
import tomllib
from pathlib import Path
from types import ModuleType

import pytest


def test_documentation_navigation_uses_progressive_disclosure() -> None:
    site = tomllib.loads(_read("zensical.toml"))
    navigation = site["project"]["nav"]
    top_level = [next(iter(section)) for section in navigation]

    assert top_level == [
        "Home",
        "Get Started",
        "Using Heartwood",
        "Models",
        "Environments",
        "For Operators",
        "Architecture",
        "Reference",
        "Project",
    ]
    serialized = json.dumps(navigation)
    for path in (
        "docs/getting-started.md",
        "docs/installation.md",
        "docs/using-heartwood.md",
        "docs/model-connections.md",
        "docs/terra-jupyter-demo.md",
        "docs/carina-cli.md",
        "docs/cli-reference.md",
        "ACRONYMS.md",
    ):
        assert path in serialized
    assert "docs/README.md" not in serialized
    assert site["project"]["extra"]["version"] == {
        "provider": "mike",
        "alias": True,
    }


def test_home_and_readme_present_a_clear_first_use_path() -> None:
    home = _read("documentation/index.md")
    readme = _read("README.md")

    for heading in (
        "## Start with Your Environment",
        "## Learn the Core Workflow",
        "## Choose How You Interact",
        "## Choose Where the Model Runs",
        "## Understand the Boundary",
        "## Find an Answer",
    ):
        assert heading in home
    assert home.index("## Start with Your Environment") < home.index(
        "## Choose Where the Model Runs"
    )
    for heading in (
        "## What Heartwood Provides",
        "## Quick Start",
        "## Choose a Setup",
        "## Responsible Use",
        "## Contribute",
        "## License",
    ):
        assert heading in readme
    assert "```bash" in readme
    assert "Schmiedmayer Lab at Stanford University" in readme
    assert "model weights or credentials" in readme
    assert "https://schmiedmayerlab.github.io/heartwood/preview/" in readme


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
        assert re.search(r"completed in \d+(?:\.\d+)? seconds", lowered) is None


def test_documentation_describes_current_product_boundaries() -> None:
    overview = _read("design/01-overview.md")
    architecture = _read("design/03-architecture.md")
    support = _read("docs/platform-support.md")
    interfaces = _read("docs/web-interface.md")

    assert "does not discover, authorize, or validate a real biomedical dataset" in overview
    assert "one allow or reject decision" in overview
    assert "does not fork the OpenHands agent loop" in architecture
    assert "Browser storage is never the source of truth" in architecture
    assert "does not enforce an interprocess lock" in architecture
    assert "One process may write a session at a time" in support
    assert "Bundled data detection and biomedical workflows use synthetic fixtures" in support
    assert "Stanford Carina has no documented authenticated Heartwood browser route" in support
    assert "| Skill inspection and management | Yes | Yes | No |" in interfaces
    assert "The notebook does not start a downloaded model" in interfaces
    assert "not included in the published generic native archive" in interfaces


def test_model_guides_match_supported_connections_and_runtimes() -> None:
    guide = _read("docs/model-connections.md")
    local = _read("docs/getting-started-offline.md")

    for source in (
        "**On this device**",
        "**OpenAI**",
        "**Anthropic**",
        "**Stanford AI API Gateway**",
    ):
        assert source in guide
    assert "Heartwood discovers available models from the selected service" in guide
    assert "Other Hugging Face model" in guide
    assert "user-supplied URL does not widen Terra or Carina policy" in guide
    assert "does not accept provider tokens as command-line arguments" in guide
    assert "HEARTWOOD_CUSTOM_MODEL_API_KEY" in guide
    assert "HEARTWOOD_MODEL_CONNECTIONS" not in guide

    assert "images contain inference software but no model weights" in local
    assert "Generic native installation | External local service" in local
    assert "public Hugging Face repository" in local
    assert "private or gated snapshots" in local
    assert "Downloading prepares the files; it does not leave a server running" in local
    assert "--network none" in local
    assert '-v "$PWD:/workspace"' in local
    assert "has no general command for importing externally transferred model files" in local
    assert "direct download command starts the transfer immediately" in local


def test_platform_guides_are_task_oriented_and_versioned() -> None:
    terra = _read("docs/terra-jupyter-demo.md")
    carina = _read("docs/carina-cli.md")
    version = _declared_version()

    assert f"ghcr.io/schmiedmayerlab/heartwood:{version}-terra" in terra
    assert f"ghcr.io/schmiedmayerlab/heartwood:{version}-terra-gpu-nvidia" in terra
    for command in (
        "heartwood detect",
        "heartwood doctor",
        "heartwood serve",
        "heartwood launch --web",
    ):
        assert command in terra
    assert "/home/jupyter/heartwood-demo" in terra
    assert "complete authenticated Jupyter proxy URL" in terra
    assert "does not reuse a token held by a terminal or browser process" in terra
    assert "real Terra workspace validation" not in terra
    assert "manifest media type" not in terra

    assert f"releases/download/{version}/heartwood-installer" in carina
    assert "module load micromamba/2.3.3" in carina
    assert "heartwood-installation heartwood-demo" in carina
    assert "heartwood launch --dry-run" in carina
    assert "`dev`, `normal`, and `long`" in carina
    assert "login nodes are for setup and job submission" in carina
    assert "The Carina interface is currently the terminal" in carina
    for obsolete in ("HEARTWOOD_ROOT", "--model-root", "PROJECT_STORAGE="):
        assert obsolete not in carina


def test_terra_notebook_uses_the_shared_project_contract() -> None:
    notebook = json.loads(_read("docs/terra-jupyter-demo.ipynb"))
    cells = notebook["cells"]
    sources = ["".join(cell["source"]) for cell in cells]
    combined = "\n".join(sources)

    assert notebook["nbformat"] == 4
    assert "Analyze Synthetic Data with Heartwood on Terra" in sources[0]
    assert f"{_declared_version()}-terra`" in combined
    assert "contains no model weights" in combined
    assert "NotebookSession" in combined
    assert "from getpass import getpass" in combined
    assert "allowed_credentials" in combined
    assert "os.environ[credential_name] = getpass" in combined
    assert "has_authenticated_jupyter_proxy" in combined
    assert "jupyter_proxy_url(port=8767)" in combined
    assert "project_root = Path.cwd().resolve()" in combined
    assert "readiness = session.project_readiness()" in combined
    assert 'session.discover_models("local", refresh=True)' in combined
    assert "session.run(prompt)" in combined
    assert "session.approve" in combined
    assert "session.audit_export()" in combined
    assert "Review every member of the pending OpenHands action set" in combined
    assert "pull-request integration" not in combined
    assert "--workspace" not in combined
    assert "HEARTWOOD_WORKSPACE" not in combined
    for cell in cells:
        if cell["cell_type"] == "code":
            assert cell["execution_count"] is None
            assert cell["outputs"] == []


def test_web_documentation_uses_current_desktop_system_screenshots() -> None:
    web_interface = _read("docs/web-interface.md")
    workflow = _read("docs/using-heartwood.md")
    package = json.loads(_read("packages/webui/package.json"))
    screenshot_script = _read("packages/webui/scripts/smoke-reference-analysis.cjs")
    model_stub = _read("images/generic/scripts/local_model_stub.py")
    assets = _repo_root() / "docs" / "assets"

    assert "assets/web-reference-analysis.png" in web_interface
    assert "assets/web-action-review.png" in workflow
    assert package["scripts"]["screenshots:docs"].endswith("../../docs/assets")
    assert '"web-action-review.png"' in screenshot_script
    assert "captureApproval: true" in screenshot_script
    assert 'HEARTWOOD_TOOL_PYTHON: "python"' in screenshot_script
    assert '"$HEARTWOOD_RUNTIME_ROOT"/skills/verified' in model_stub
    assert "call-heartwood-reference-analysis-read" in model_stub
    for filename in ("web-action-review.png", "web-reference-analysis.png"):
        screenshot = assets / filename
        assert screenshot.stat().st_size > 1_000
        width, height = _png_dimensions(screenshot)
        assert width >= 1280
        assert height >= 800
        assert (assets / f"{filename}.license").is_file()
    assert not (assets / "web-notebook-viewport.png").exists()


def test_documentation_site_stages_only_canonical_public_sources(tmp_path: Path) -> None:
    stager = _documentation_stager()
    destination = tmp_path / "documentation"

    stager.stage_documentation(_repo_root(), destination)

    marker = destination.parent / ".documentation.heartwood-documentation-stage"
    marker_content = marker.read_text(encoding="utf-8")
    assert marker_content == stager._stage_marker_content(destination)
    assert marker_content.startswith("heartwood.documentation-stage.v2\ntree-sha256=")
    assert not (destination / ".heartwood-documentation-stage").exists()
    expected_home = _read("documentation/index.md").replace("](../", "](")
    assert (destination / "index.md").read_text(encoding="utf-8") == expected_home
    assert not (destination / "README.md").exists()
    assert not (destination / "docs" / "README.md").exists()
    for filename in (
        "getting-started.md",
        "installation.md",
        "using-heartwood.md",
        "model-connections.md",
        "terra-jupyter-demo.md",
        "carina-cli.md",
        "cli-reference.md",
    ):
        assert (destination / "docs" / filename).is_file()
    for filename in (
        "ACRONYMS.md",
        "CONTRIBUTING.md",
        "CONTRIBUTORS.md",
        "LICENSE",
        "NOTICE",
    ):
        assert (destination / filename).is_file()
    assert (destination / "stylesheets" / "extra.css").is_file()
    staged_documentation = {
        path.relative_to(destination / "docs").as_posix()
        for path in (destination / "docs").rglob("*")
        if path.is_file()
    }
    expected_documentation = set(stager._DOCUMENTATION_FILES)
    expected_documentation.update(f"assets/{name}" for name in stager._DOCUMENTATION_ASSETS)
    assert staged_documentation == expected_documentation
    for index in range(1, 9):
        assert tuple((destination / "design").glob(f"{index:02d}-*.md"))
    assert not tuple((destination / "design").glob("09-*.md"))

    contributing = (destination / "CONTRIBUTING.md").read_text(encoding="utf-8")
    version = stager.declared_version(_repo_root())
    assert f"https://github.com/SchmiedmayerLab/heartwood/tree/{version}/AGENTS.md" in contributing
    assert "](../" not in (destination / "index.md").read_text(encoding="utf-8")

    stager.stage_documentation(_repo_root(), destination)
    assert marker.is_file()

    staged_index = destination / "index.md"
    staged_index.write_text("repurposed output\n", encoding="utf-8")
    with pytest.raises(ValueError, match="without a valid Heartwood staging marker"):
        stager.stage_documentation(_repo_root(), destination)
    assert staged_index.read_text(encoding="utf-8") == "repurposed output\n"

    extra_destination = tmp_path / "documentation-extra"
    stager.stage_documentation(_repo_root(), extra_destination)
    transient = extra_destination / "transient.txt"
    transient.write_text("preserve me\n", encoding="utf-8")
    with pytest.raises(ValueError, match="without a valid Heartwood staging marker"):
        stager.stage_documentation(_repo_root(), extra_destination)
    assert transient.read_text(encoding="utf-8") == "preserve me\n"


@pytest.mark.parametrize(
    "relative_path",
    [
        ".",
        "docs",
        "docs/generated-site",
        "design",
        "documentation/stylesheets",
        "README.md",
    ],
)
def test_documentation_stager_rejects_destructive_output_paths(relative_path: str) -> None:
    stager = _documentation_stager()
    output = (_repo_root() / relative_path).resolve()

    with pytest.raises(ValueError, match="must not replace"):
        stager.stage_documentation(_repo_root(), output)


def test_documentation_stager_rejects_repository_ancestor() -> None:
    stager = _documentation_stager()

    with pytest.raises(ValueError, match="must not replace"):
        stager.stage_documentation(_repo_root(), _repo_root().parent)


def test_documentation_stager_preserves_unmarked_existing_output(tmp_path: Path) -> None:
    stager = _documentation_stager()
    destination = tmp_path / "unrelated"
    destination.mkdir()
    sentinel = destination / "keep.txt"
    sentinel.write_text("research data\n", encoding="utf-8")

    with pytest.raises(ValueError, match="without a valid Heartwood staging marker"):
        stager.stage_documentation(_repo_root(), destination)

    assert sentinel.read_text(encoding="utf-8") == "research data\n"


def test_documentation_stager_recognizes_only_the_exact_legacy_shape(tmp_path: Path) -> None:
    stager = _documentation_stager()
    destination = tmp_path / "legacy"
    destination.mkdir()
    for directory in ("design", "docs", "stylesheets"):
        (destination / directory).mkdir()
    for filename in (
        "ACRONYMS.md",
        "CONTRIBUTING.md",
        "CONTRIBUTORS.md",
        "LICENSE",
        "NOTICE",
        "README.md",
    ):
        (destination / filename).write_text("synthetic\n", encoding="utf-8")
    (destination / "index.md").write_text('--8<-- "README.md"\n', encoding="utf-8")

    assert stager._looks_like_legacy_stage(destination)
    (destination / "research-data.csv").write_text("synthetic\n", encoding="utf-8")
    assert not stager._looks_like_legacy_stage(destination)


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
    guide = _read("docs/platform-images.md")

    for path in (
        "images/platforms.toml",
        "images/platform/Dockerfile",
        "images/platform/scripts/verify_registry_manifest.py",
        "docker-bake.hcl",
    ):
        assert path in guide
    assert "shared Heartwood payload" in guide
    assert "Do not add a platform-specific agent loop" in guide
    assert "model weights and credentials out of image layers" in guide
    assert "synthetic data" in guide.lower()


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


def _documentation_stager() -> ModuleType:
    path = _repo_root() / "deploy" / "stage_documentation.py"
    spec = importlib.util.spec_from_file_location("stage_documentation", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _canonical_documentation_paths() -> tuple[Path, ...]:
    return (
        _repo_root() / "README.md",
        *tuple((_repo_root() / "documentation").glob("*.md")),
        *tuple((_repo_root() / "docs").glob("*.md")),
        *tuple((_repo_root() / "design").glob("*.md")),
    )


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]
