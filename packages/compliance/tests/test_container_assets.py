# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Static tests for the generic image and Compose smoke-test contract."""

from __future__ import annotations

from pathlib import Path


def test_generic_image_contains_runtime_surface_packages() -> None:
    dockerfile = (_repo_root() / "images" / "generic" / "Dockerfile").read_text(encoding="utf-8")

    assert "uv sync --locked --no-dev --all-extras" in dockerfile
    assert "COPY packages ./packages" in dockerfile
    assert "COPY fixtures ./fixtures" in dockerfile
    assert "COPY skills ./skills" in dockerfile
    assert "COPY images ./images" in dockerfile
    assert "USER heartwood" in dockerfile
    assert 'PATH="/opt/heartwood/.venv/bin:${PATH}"' in dockerfile
    assert 'CMD ["heartwood", "--help"]' in dockerfile


def test_compose_smoke_runtime_disables_network() -> None:
    compose = (_repo_root() / "images" / "generic" / "compose.yaml").read_text(encoding="utf-8")

    assert "network_mode: none" in compose
    assert "bash images/generic/scripts/offline_stack_smoke.sh" in compose


def test_offline_stack_smoke_runs_local_model_and_cli() -> None:
    script = (_repo_root() / "images" / "generic" / "scripts" / "offline_stack_smoke.sh").read_text(
        encoding="utf-8"
    )

    assert "local_model_stub.py" in script
    assert "--local-model" in script
    assert "--target-id decision-synthetic-model-call" in script
    assert 'grep -q "model=heartwood-local-demo status=ok"' in script
    assert "reviewer packet" in script


def test_container_image_workflow_publishes_ghcr_tags() -> None:
    workflow = (_repo_root() / ".github" / "workflows" / "container-image.yml").read_text(
        encoding="utf-8"
    )

    assert "packages: write" in workflow
    assert "ghcr.io/${GITHUB_REPOSITORY,,}" in workflow
    assert ":dev-main" in workflow
    assert ":main" in workflow
    assert "${{ github.sha }}" in workflow


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]
