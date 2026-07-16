# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Static tests for runnable documentation assets."""

from __future__ import annotations

import importlib.util
import json
import re
import tomllib
from pathlib import Path
from types import ModuleType

import pytest


def test_documentation_index_uses_progressive_disclosure_and_project_tracking() -> None:
    index = _read("docs/README.md")
    readme = _read("README.md")
    development = _read("design/08-development.md")
    site = tomllib.loads(_read("zensical.toml"))

    for heading in (
        "## Start Here",
        "## Models",
        "## Platforms",
        "## Deployment",
        "## How Heartwood Works",
        "## Reference",
        "## Documentation Status",
        "## Project Planning",
        "## Published Documentation",
    ):
        assert heading in index
    for term in (
        "Implemented",
        "CI-validated",
        "Live-validated",
        "Institution-approved",
        "Release-ready",
    ):
        assert term in index
    assert "[Documentation Guide](docs/README.md)" in readme
    assert "[Get Started](docs/getting-started.md)" in readme
    assert "[Work with the Agent](docs/using-heartwood.md)" in readme
    assert "[Platform Support and Validation](docs/platform-support.md)" in readme
    assert "[documentation index](../docs/README.md)" in development
    assert "https://github.com/SchmiedmayerLab/heartwood/issues" in index
    assert "https://github.com/orgs/SchmiedmayerLab/projects/2" in index
    assert "does not serve as a backlog, implementation diary, or decision transcript" in index
    superfences = site["project"]["markdown_extensions"]["pymdownx"]["superfences"]
    assert superfences["custom_fences"] == [
        {
            "name": "mermaid",
            "class": "mermaid",
            "format": "pymdownx.superfences.fence_code_format",
        }
    ]
    assert site["project"]["extra"]["version"] == {
        "provider": "mike",
        "alias": True,
    }


def test_platform_support_distinguishes_ci_from_live_validation() -> None:
    support = _read("docs/platform-support.md")

    assert "## Current Status" in support
    assert "## Validation Evidence" in support
    assert "Generic Linux or Jupyter environment | Generic release container" in support
    assert "Terra Jupyter | Terra-derived release image" in support
    assert "Design target only" not in support
    assert "Real Terra workspace validation remains required" in support
    assert "does not establish a business associate agreement" in support
    assert "synthetic fixtures" in support
    assert "does not infer authorization for workspace data" in support
    assert "concurrent independent processes writing the same session" in support


def test_planned_work_is_owned_by_github_tracking() -> None:
    assert not (_repo_root() / "design" / "09-implementation-plan.md").exists()
    for path in (
        "README.md",
        "AGENTS.md",
        "docs/README.md",
        "design/01-overview.md",
        "design/02-platforms.md",
        "design/03-architecture.md",
        "design/07-testing-eval.md",
        "design/08-development.md",
    ):
        content = _read(path)
        assert "09-implementation-plan" not in content
        assert "Delivery Roadmap" not in content
    project_url = "https://github.com/orgs/SchmiedmayerLab/projects/2"
    for path in ("README.md", "AGENTS.md", "docs/README.md", "design/08-development.md"):
        assert project_url in _read(path)
    for documentation_path in _canonical_documentation_paths():
        content = documentation_path.read_text(encoding="utf-8")
        assert "Delivery Roadmap" not in content
        assert "## Future " not in content
        assert "in the roadmap" not in content.lower()
        assert re.search(r"github\.com/[^\s)]+/issues/\d+", content) is None
        assert "TODO" not in content
        assert "TBD" not in content


def test_architecture_requires_upstream_reuse() -> None:
    architecture = _read("design/03-architecture.md")
    skills = _read("design/04-skills.md")

    assert "## Upstream Reuse Rule" in architecture
    assert "must not fork or independently reproduce" in architecture
    assert "Provider-specific request construction" in architecture
    assert (
        "Terminal and file actions execute only through the OpenHands conversation" in architecture
    )
    assert "Stronger isolation must use a supported OpenHands remote workspace" in architecture
    assert (
        "Automatic user, public-marketplace, and project-workspace Skill loading is disabled"
        in skills
    )
    assert "audited deployment assertion rather than a network interceptor" in architecture


def test_web_experience_remains_a_gateway_projection() -> None:
    architecture = _read("design/03-architecture.md")
    testing = _read("design/07-testing-eval.md")
    readme = _read("README.md")

    assert "browser storage is never the source of truth" in architecture
    assert "the shell does not encode a fixed cohort" in architecture
    assert "The browser does not synthesize action outcomes" in architecture
    assert "Compliance evidence packages remain maintainer or reviewer tooling" in architecture
    assert "defines the presentation contract" in architecture
    assert "gateway-owned session metadata" in architecture
    assert "Unknown or unconfigured state is explicit" in architecture
    assert "Endpoint validation or artifact integrity is not capability evidence" in testing
    assert "terminal, browser, or notebook" in readme
    assert "keeps its setup and conversation history with that project" in readme


def test_documentation_site_stages_only_canonical_public_sources(tmp_path: Path) -> None:
    stager = _documentation_stager()
    destination = tmp_path / "documentation"

    stager.stage_documentation(_repo_root(), destination)

    assert (destination / ".heartwood-documentation-stage").read_text(
        encoding="utf-8"
    ) == "heartwood.documentation-stage.v1\n"
    assert (destination / "index.md").read_text(encoding="utf-8") == '--8<-- "README.md"\n'
    assert (destination / "docs" / "README.md").is_file()
    for filename in (
        "getting-started.md",
        "platforms.md",
        "deployment.md",
        "project-state.md",
    ):
        assert (destination / "docs" / filename).is_file()
    assert (destination / "docs" / "assets" / "web-reference-analysis.png").is_file()
    for filename in (
        "ACRONYMS.md",
        "CONTRIBUTING.md",
        "CONTRIBUTORS.md",
        "LICENSE",
        "NOTICE",
    ):
        assert (destination / filename).is_file()
    assert (destination / "stylesheets" / "extra.css").is_file()
    for index in range(1, 9):
        assert tuple((destination / "design").glob(f"{index:02d}-*.md"))
    assert not tuple((destination / "design").glob("09-*.md"))

    readme = (destination / "README.md").read_text(encoding="utf-8")
    container = (destination / "docs" / "container-images.md").read_text(encoding="utf-8")
    carina = (destination / "docs" / "carina-cli.md").read_text(encoding="utf-8")
    contributing = (destination / "CONTRIBUTING.md").read_text(encoding="utf-8")
    version = stager.declared_version(_repo_root())
    assert f"ghcr.io/schmiedmayerlab/heartwood:{version}" not in readme
    assert f"releases/download/{version}/heartwood-installer" not in readme
    assert f"ghcr.io/schmiedmayerlab/heartwood:{version}" in container
    assert f"releases/download/{version}/heartwood-installer" in carina
    assert f"https://github.com/SchmiedmayerLab/heartwood/tree/{version}/AGENTS.md" in contributing

    (destination / "transient.txt").write_text("remove me\n", encoding="utf-8")
    stager.stage_documentation(_repo_root(), destination)
    assert not (destination / "transient.txt").exists()


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


def test_documentation_stager_requires_exact_legacy_output_shape(tmp_path: Path) -> None:
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


def test_web_interface_documentation_uses_synthetic_system_screenshots() -> None:
    web_interface = _read("docs/web-interface.md")
    terra = _read("docs/terra-jupyter-demo.md")
    package = json.loads(_read("packages/webui/package.json"))
    assets = _repo_root() / "docs" / "assets"

    assert (
        "same conversations, model selection, action review, Skills, and audit history"
        in web_interface
    )
    assert "Allow all once" in web_interface
    assert "documentation screenshots contain synthetic data only" in web_interface.lower()
    assert "responsive layout used by the automated notebook-viewport test" in terra
    assert package["scripts"]["screenshots:docs"].endswith("../../docs/assets")
    for filename in ("web-reference-analysis.png", "web-notebook-viewport.png"):
        screenshot = assets / filename
        assert screenshot.stat().st_size > 1_000
        assert (assets / f"{filename}.license").is_file()


def test_project_markdown_contains_no_process_artifacts() -> None:
    forbidden_parts = (
        ("evaluated,", "not", "chosen"),
        ("this", "pass", "replaces"),
        ("historical", "command"),
        ("carried", "over"),
        ("co", "dex"),
        ("phase", "0"),
    )

    for path in _project_markdown_paths():
        content = path.read_text(encoding="utf-8").lower()
        for parts in forbidden_parts:
            phrase = " ".join(parts) if parts != ("co", "dex") else "".join(parts)
            assert phrase not in content, f"{phrase!r} found in {path.relative_to(_repo_root())}"


def test_terra_notebook_uses_the_no_weight_runtime_contract() -> None:
    notebook = json.loads(_read("docs/terra-jupyter-demo.ipynb"))
    cells = notebook["cells"]
    sources = ["".join(cell["source"]) for cell in cells]
    combined = "\n".join(sources)

    assert notebook["nbformat"] == 4
    assert "Analyze Synthetic Data with Heartwood on Terra" in sources[0]
    assert f"{_declared_version()}-terra`" in combined
    assert "contains no model weights" in combined
    assert "edge-terra-coder" not in combined
    assert "edge-terra-smoke" not in combined
    assert "NotebookSession" in combined
    assert "has_authenticated_jupyter_proxy" in combined
    assert "jupyter_proxy_url(port=8767)" in combined
    assert "heartwood serve" in combined
    assert "heartwood launch --web" in combined
    assert "Open Heartwood in a new tab" in combined
    assert "project_root = Path.cwd().resolve()" in combined
    assert "/home/jupyter/heartwood-demo" not in combined
    assert "os.chdir(project_root)" not in combined
    assert "--workspace" not in combined
    assert combined.index("readiness = session.project_readiness()") < combined.index(
        "input_root.mkdir(parents=True, exist_ok=True)"
    )
    assert 'readiness["state"] == "setup-required"' in combined
    assert 'session.discover_models("local", refresh=True)' in combined
    assert "HEARTWOOD_WORKSPACE" not in combined
    assert "session.detect()" in combined
    assert "target-condition cohort" in combined
    assert "session.run(prompt)" in combined
    assert "session.approve" in combined
    assert 'source_participant_count"] == 24' in combined
    assert '"name": "heartwood"' in _read("docs/terra-jupyter-demo.ipynb")
    assert "session.audit_export()" in combined
    assert "Review every member of the pending OpenHands action set" in combined
    for cell in cells:
        if cell["cell_type"] == "code":
            assert cell["execution_count"] is None
            assert cell["outputs"] == []


def test_readme_and_model_guides_define_project_and_no_weight_runtime_paths() -> None:
    readme = _read("README.md")
    container = _read("docs/container-images.md")
    local = _read("docs/getting-started-offline.md")

    for document in (readme, container, local):
        assert "model weights" in document.lower()
    for document in (container, local):
        assert "OpenHands" in document
        assert ".heartwood" in document
    assert "OpenHands" not in readme
    assert ".heartwood" not in readme
    assert "directory where it starts as the project" in readme
    assert "heartwood models download" not in readme
    assert "enter a token for the current Heartwood process" in readme
    assert "model-provider agreements" in readme
    assert "easiest way to use the complete CLI and browser interface" in container
    assert "one project mount" in container.lower()
    assert "Semantic Version tags" in container
    assert "HEARTWOOD_LOCAL_MODEL_PATH" not in container
    assert "deterministic loopback model fixture" in local
    assert "recommended model" in local
    assert "one persistent project mount" in local
    assert "`run_capable_model` option" in local
    assert "`AlwaysConfirm`" in local
    for stale in ("edge-smoke", "edge-providers", "edge-coder-7b", "edge-terra-smoke"):
        assert stale not in readme
        assert stale not in container
        assert stale not in local


def test_model_connection_guide_defines_shared_provider_and_platform_contracts() -> None:
    guide = _read("docs/model-connections.md")

    assert "heartwood.model-connections.v1" in guide
    assert "heartwood models refresh <connection-id>" in guide
    assert "heartwood models connect <connection-id> <model-id>" in guide
    assert "asks the selected service for its available models" in guide
    assert "**OpenAI**" in guide
    assert "**Anthropic**" in guide
    assert "current project's `.heartwood/config.toml`" in guide
    assert "A token entered in the terminal or browser remains only" in guide
    assert "managed research environment" in guide
    assert "absolute HTTPS base URL" in guide
    assert "CLI has no token command-line argument" in guide
    assert "HEARTWOOD_MODEL_CONNECTIONS" not in guide


def test_beginner_guides_explain_one_project_and_local_server_lifecycle() -> None:
    getting_started = _read("docs/getting-started.md")
    platforms = _read("docs/platforms.md")
    project = _read("docs/project-state.md")
    local = _read("docs/getting-started-offline.md")
    deployment = _read("docs/deployment.md")

    assert "recommended first-use path" in getting_started
    assert "do not need both a container and a native installation" in getting_started
    assert (
        "Starting Heartwood from a different directory opens a different project" in getting_started
    )
    assert "Platform home directories and container mount points" in platforms
    assert (
        "directory where the Heartwood command, web server, or notebook process starts" in project
    )
    assert "## Understand the Three Parts" in local
    assert "**Model files**" in local
    assert "**The inference server**" in local
    assert "**Heartwood and OpenHands**" in local
    assert "Downloading a model prepares the files but does not leave a server running" in local
    assert "Researchers should not need deployment-specific workspace" in deployment


def test_carina_runbook_uses_project_local_release_workflow() -> None:
    runbook = _read("docs/carina-cli.md")

    assert f"## Step 2: Install Release {_declared_version()}" in runbook
    assert f"--version {_declared_version()}" in runbook
    assert "project's private `.heartwood/` state" in runbook
    assert "heartwood models download qwen25-7b-instruct-vllm" in runbook
    assert "heartwood launch" in runbook
    assert "HEARTWOOD_ROOT" not in runbook
    assert "--model-root" not in runbook


def test_terra_runbook_tracks_platform_and_model_setup() -> None:
    runbook = _read("docs/terra-jupyter-demo.md")

    assert f"ghcr.io/schmiedmayerlab/heartwood:{_declared_version()}-terra" in runbook
    assert "contains no model weights" in runbook
    assert "Terra Jupyter Python base" in runbook
    assert "OpenHands terminal commands retain the operating-system permissions" in runbook
    assert "Terra's environment remains the hard filesystem boundary" in runbook
    assert "application/vnd.docker.distribution.manifest.v2+json" in runbook
    assert "rejects an Open Container Initiative index" in runbook
    assert "current project's `.heartwood/models/`" in runbook
    assert "models refresh <connection-id>" in runbook
    assert "models connect" in runbook
    assert "HEARTWOOD_MODEL_CONNECTIONS" not in runbook
    assert "business associate agreement" in runbook
    assert "Allow all once" in runbook
    assert "Reject all" in runbook
    assert "24 synthetic people" in runbook
    assert "target-condition cohort" in runbook
    assert "Heartwood image and Terra base image digests" in runbook
    assert "real Terra workspace validation" in runbook
    assert "heartwood serve" in runbook
    assert "heartwood launch --web" in runbook
    assert "edge-terra-coder" not in runbook
    assert "edge-terra-smoke" not in runbook


def test_container_examples_preserve_bind_mount_writability() -> None:
    guide = _read("docs/container-images.md")

    assert guide.count("docker run --rm -it \\") == 8
    assert guide.count('--user "$(id -u):$(id -g)" \\') == 8
    assert guide.count("--env HOME=/tmp \\") == 8
    assert "Select exactly one immutable release image" in _read("docs/terra-jupyter-demo.md")


def test_platform_extension_guide_defines_one_shared_mechanism() -> None:
    guide = _read("docs/platform-images.md")

    for path in (
        "images/platforms.toml",
        "images/platform/Dockerfile",
        "images/platform/scripts/verify_registry_manifest.py",
        "docker-bake.hcl",
        ".github/workflows/container-smoke.yml",
        ".github/workflows/container-image.yml",
    ):
        assert path in guide
    assert "Add or Adapt a Platform Image" in guide
    assert "same Heartwood payload" in guide
    assert "Keep model weights and credentials out of every layer" in guide
    assert "Keep `--set <target>.platform=<architecture>`" in guide
    assert "use the Docker driver" in guide
    assert "manifest media type" in guide
    assert "non-platform manifest policy" in guide
    assert "Terra tags must return" in guide
    assert "synthetic data only" in guide.lower()
    assert "Do not promote a platform to supported based only on local CI" in guide


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


def _project_markdown_paths() -> tuple[Path, ...]:
    excluded = {".git", ".venv", ".uv-cache", "coverage", "node_modules"}
    return tuple(
        path for path in _repo_root().rglob("*.md") if not excluded.intersection(path.parts)
    )


def _canonical_documentation_paths() -> tuple[Path, ...]:
    return (
        _repo_root() / "README.md",
        *tuple((_repo_root() / "docs").glob("*.md")),
        *tuple((_repo_root() / "design").glob("*.md")),
    )


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]
