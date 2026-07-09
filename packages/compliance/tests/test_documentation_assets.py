# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Static tests for runnable documentation assets."""

from __future__ import annotations

import json
from pathlib import Path


def test_terra_jupyter_demo_notebook_covers_runtime_surfaces() -> None:
    notebook = json.loads(
        (_repo_root() / "docs" / "terra-jupyter-demo.ipynb").read_text(encoding="utf-8")
    )
    cells = notebook["cells"]
    sources = ["".join(cell["source"]) for cell in cells]
    combined = "\n".join(sources)

    assert notebook["nbformat"] == 4
    assert "Terra-Style Jupyter Demo" in sources[0]
    assert "NotebookSession" in combined
    assert "jupyter_proxy_url(port=8767)" in combined
    assert "offline_stack_smoke.sh" in combined
    assert "start_web_ui.sh" in combined
    assert "heartwood --workspace /home/jupyter/heartwood-workspace/sessions" in combined
    assert "session.detect()" in combined
    assert 'session.run("run the synthetic workflow")' in combined
    assert "session.audit_export()" in combined
    assert "build_widget_spec" in combined
    assert "synthetic" in combined
    assert "live Terra validation pass still needs to confirm" in combined
    for cell in cells:
        if cell["cell_type"] == "code":
            assert cell["execution_count"] is None
            assert cell["outputs"] == []


def test_terra_runbook_tracks_platform_image_contract() -> None:
    runbook = (_repo_root() / "docs" / "terra-jupyter-demo.md").read_text(encoding="utf-8")

    assert "Terra Jupyter Notebook base image" in runbook
    assert "DataBiosphere/terra-docker" in runbook
    assert "ghcr.io/schmiedmayerlab/heartwood:edge-terra-smoke" in runbook
    assert "edge-terra" in runbook
    assert "edge-terra-smoke-ci" in runbook
    assert "Terra-compatible notebook base" in runbook
    assert "publish automatically from `main`" in runbook
    assert "cd /opt/heartwood && bash images/generic/scripts/offline_stack_smoke.sh" in runbook
    assert "run.approval_controls" in runbook
    assert "approval.decision" in runbook
    assert "run.approvals" not in runbook
    assert "approval.status" not in runbook
    assert "web UI chat interaction" in runbook
    assert "custom image digest" in runbook
    assert "real Terra workspace is still required" in runbook
    assert "configure the Cloud Environment to use the selected image directly" not in runbook


def test_platform_image_extension_guide_defines_mechanism() -> None:
    guide = (_repo_root() / "docs" / "platform-images.md").read_text(encoding="utf-8")
    readme = (_repo_root() / "README.md").read_text(encoding="utf-8")
    container_docs = (_repo_root() / "docs" / "container-images.md").read_text(encoding="utf-8")

    assert "Platform Image Extension Guide" in guide
    assert "images/platforms.toml" in guide
    assert "images/platform/Dockerfile" in guide
    assert "docker-bake.hcl" in guide
    assert ".github/workflows/container-smoke.yml" in guide
    assert ".github/workflows/container-image.yml" in guide
    assert "Add Or Adapt A Platform Image" in guide
    assert "Keep `--set <target>.platform=<architecture>`" in guide
    assert "use the Docker driver when the build depends on a locally tagged base image" in guide
    assert "local-only CI load targets without attestations" in guide
    assert (
        "Docker's local image exporter does not load manifest lists or attested image indexes"
        in guide
    )
    assert "Required Live Evidence" in guide
    assert "custom image digest" in guide
    assert "Synthetic data only" in guide
    assert "Platform Image Extension Guide](docs/platform-images.md)" in readme
    assert "Platform Image Extension Guide](platform-images.md)" in container_docs


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]
