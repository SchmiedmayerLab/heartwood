# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Static tests for runnable documentation assets."""

from __future__ import annotations

import json
from pathlib import Path


def test_documentation_index_separates_current_design_and_delivery_work() -> None:
    index = _read("docs/README.md")
    readme = _read("README.md")
    development = _read("design/08-development.md")

    for heading in (
        "## Current Operational Documentation",
        "## Design And Rationale",
        "## Delivery Roadmap",
        "## Status Terms",
    ):
        assert heading in index
    for term in (
        "Implemented",
        "CI-validated",
        "Live-validated",
        "Institution-approved",
        "Release-ready",
        "Planned",
    ):
        assert term in index
    assert "[Documentation](docs/README.md)" in readme
    assert "[Platform Support](docs/platform-support.md)" in readme
    assert "[documentation index](../docs/README.md)" in development


def test_platform_support_distinguishes_ci_from_live_validation() -> None:
    support = _read("docs/platform-support.md")

    assert "## Support Matrix" in support
    assert "Generic Linux or Jupyter environment | Implemented" in support
    assert "Terra Jupyter | Implemented platform-derived image" in support
    assert "All of Us or AnVIL through Terra | Design target only" in support
    assert "Seven Bridges or Velsera | Design target only" in support
    assert "DNAnexus or UK Biobank Research Analysis Platform | Design target only" in support
    assert "Real Terra workspace validation remains required" in support
    assert "does not establish a business associate agreement" in support
    assert "synthetic OMOP data-source fixture" in support
    assert "not a complete Terra runtime adapter" in support
    assert "concurrent independent processes writing the same session" in support


def test_delivery_roadmap_is_ordered_actionable_and_unambiguous() -> None:
    roadmap = _read("design/09-implementation-plan.md")
    ordered_headings = (
        "## Non-Negotiable Requirements",
        "## Current Baseline",
        "## Material Readiness Gaps",
        "## Priority 1 — Release-Candidate Runtime Contract",
        "## Priority 2 — Researcher Web Experience",
        "## Priority 3 — Live Terra Acceptance",
        "## Priority 4 — Terra And OMOP Reference Workflow",
        "## Priority 5 — Assurance And Stable Release Governance",
        "## Priority 6 — Conditional Expansion",
        "## Start Conditions For Deferred Capabilities",
        "## Cross-Cutting Acceptance Rules",
    )

    assert roadmap.startswith("<!--")
    assert "# 09 — Delivery Roadmap" in roadmap
    assert [roadmap.index(heading) for heading in ordered_headings] == sorted(
        roadmap.index(heading) for heading in ordered_headings
    )
    assert roadmap.count("**Objective:**") == 6
    assert roadmap.count("### Deliverables") == 6
    assert roadmap.count("### Exit Criteria") == 6
    assert "- [ ]" in roadmap
    assert "- [x]" not in roadmap
    assert "LocalFilesystemDataSourceAdapter.synthetic_omop()" in roadmap
    assert "OpenHands dependency-upgrade gate" in roadmap
    assert "## Phase" not in roadmap
    assert "This pass" not in roadmap


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
    roadmap = _read("design/09-implementation-plan.md")
    readme = _read("README.md")

    assert "browser storage is never the source of truth" in architecture
    assert "the shell does not encode a fixed cohort" in architecture
    assert "The browser does not synthesize action outcomes" in architecture
    assert "Compliance evidence packages remain maintainer or reviewer tooling" in architecture
    assert "defines the presentation contract" in architecture
    assert "gateway-owned session metadata" in architecture
    assert "## Priority 2 — Researcher Web Experience" in roadmap
    assert "Represent absent evidence as unknown or unconfigured" in roadmap
    assert "no model capability claim without benchmark evidence" in roadmap
    assert "Do not place compliance evidence packages" in roadmap
    assert "gateway-owned session lifecycle" in readme
    assert "Boundary and workflow labels require typed gateway evidence" in readme


def test_web_interface_documentation_uses_synthetic_system_screenshots() -> None:
    web_interface = _read("docs/web-interface.md")
    terra = _read("docs/terra-jupyter-demo.md")
    package = json.loads(_read("packages/webui/package.json"))
    assets = _repo_root() / "docs" / "assets"

    assert "production web build, gateway, OpenHands SDK adapter" in web_interface
    assert "not model quality or live Terra behavior" in web_interface
    assert "npm run screenshots:docs" in web_interface
    assert "responsive layout, not live Leonardo proxy" in terra
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
    assert "Terra-Style Jupyter Demo" in sources[0]
    assert "edge-terra`" in combined
    assert "contains no model weights" in combined
    assert "edge-terra-coder" not in combined
    assert "edge-terra-smoke" not in combined
    assert "NotebookSession" in combined
    assert "jupyter_proxy_url(port=8767)" in combined
    assert "start_web_ui.sh" in combined
    assert "session.detect()" in combined
    assert "target-condition cohort" in combined
    assert "session.run(prompt)" in combined
    assert "session.approve" in combined
    assert 'source_participant_count"] == 24' in combined
    assert '"name": "heartwood"' in _read("docs/terra-jupyter-demo.ipynb")
    assert "session.audit_export()" in combined
    assert "Review the printed pending action" in combined
    for cell in cells:
        if cell["cell_type"] == "code":
            assert cell["execution_count"] is None
            assert cell["outputs"] == []


def test_readme_and_model_guides_define_both_runtime_paths() -> None:
    readme = _read("README.md")
    container = _read("docs/container-images.md")
    local = _read("docs/getting-started-offline.md")

    for document in (readme, container, local):
        assert "model weights" in document.lower()
        assert "OpenHands" in document
        assert "policy" in document.lower()
    assert "heartwood models refresh local" in readme
    assert "heartwood models connect local <model-id>" in readme
    assert "heartwood models download" in readme
    assert "environment variables, mounted files, managed identity" in readme
    assert "business associate agreement" in readme
    assert "pushes each result by digest" in container
    assert "persistent `-amd64` and `-arm64` helper tags" in container
    assert "Public commit and moving tags are promotion outputs" in container
    assert "publication jobs run only for the `main` ref" in container
    assert "pull requests continue to run CI validation" in container
    assert "checks the current `main` commit immediately before" in container
    assert "candidate-validation failure leaves the candidate untagged" in container
    assert "promotion failure may leave the validated immutable commit tag" in container
    assert "reachability graph" in container
    assert "HEARTWOOD_LOCAL_MODEL_PATH" in container
    assert "deterministic loopback model fixture" in local
    assert "allowed_model_catalog_endpoints" in local
    assert "mounted capable-model test" in local
    assert "capable_model_e2e.sh" in local
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
    assert "Official OpenAI model-list operation" in guide
    assert "Official Anthropic model-list operation" in guide
    assert "HEARTWOOD_MODEL_CONNECTIONS" in guide
    assert "allowed_model_catalog_endpoints" in guide
    assert "allowed_model_endpoints" in guide
    assert "api_version" in guide
    assert "aws_region_name" in guide
    assert "aws_profile_name" in guide
    assert "The CLI has no token argument" in guide


def test_terra_runbook_tracks_platform_and_model_setup() -> None:
    runbook = _read("docs/terra-jupyter-demo.md")

    assert "ghcr.io/schmiedmayerlab/heartwood:edge-terra" in runbook
    assert "contains no model weights" in runbook
    assert "Terra Jupyter Notebook base image" in runbook
    assert "application/vnd.docker.distribution.manifest.v2+json" in runbook
    assert "Leonardo rejects an Open Container Initiative index" in runbook
    assert "heartwood-workspace/models" in runbook
    assert "models refresh <connection-id>" in runbook
    assert "models connect" in runbook
    assert "models refresh local" in runbook
    assert "HEARTWOOD_MODEL_CONNECTIONS" in runbook
    assert "business associate agreement" in runbook
    assert "Allow once" in runbook
    assert "Reject" in runbook
    assert "WORKSPACE_BUCKET" in runbook
    assert "24 synthetic people" in runbook
    assert "target-condition cohort" in runbook
    assert "custom image and base image digests" in runbook
    assert "real Terra workspace validation remains required" in runbook
    assert "edge-terra-coder" not in runbook
    assert "edge-terra-smoke" not in runbook


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
    assert "Add Or Adapt A Platform Image" in guide
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


def _project_markdown_paths() -> tuple[Path, ...]:
    excluded = {".git", ".venv", ".uv-cache", "coverage", "node_modules"}
    return tuple(
        path for path in _repo_root().rglob("*.md") if not excluded.intersection(path.parts)
    )


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]
