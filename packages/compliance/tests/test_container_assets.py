# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Static tests for the generic image and Compose smoke-test contract."""

from __future__ import annotations

import tomllib
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

    assert "start_local_runtime.sh" in script
    assert "stub-loopback" in script
    assert "--local-model" in script
    assert "--target-id decision-synthetic-model-call" in script
    assert 'grep -q "model=heartwood-local-demo status=ok"' in script
    assert "reviewer packet" in script


def test_local_runtime_profiles_distinguish_stub_from_real_runtime() -> None:
    manifest = tomllib.loads(
        (_repo_root() / "images" / "generic" / "local-runtime" / "profiles.toml").read_text(
            encoding="utf-8"
        )
    )

    assert manifest["default_profile"] == "stub-loopback"
    assert manifest["selected_real_profile"] == "llama-cpp-cpu"
    stub = manifest["profiles"]["stub-loopback"]
    real = manifest["profiles"]["llama-cpp-cpu"]
    gpu = manifest["profiles"]["llama-cpp-cuda"]
    assert stub["status"] == "implemented"
    assert stub["inference_runtime"] is False
    assert stub["quality_claim"] is False
    assert stub["supported_platforms"] == ["linux/amd64", "linux/arm64"]
    assert stub["ships_in_generic_image"] is True
    assert real["status"] == "contract"
    assert real["runtime"] == "llama.cpp through llama-cpp-python server"
    assert real["inference_runtime"] is True
    assert real["model_artifact_required"] is True
    assert real["supported_platforms"] == ["linux/amd64", "linux/arm64"]
    assert real["artifact_format"] == "GGUF"
    assert real["artifact_checksum"].startswith("SHA-256")
    assert real["runtime_resolution"].startswith("Load only")
    assert real["ships_in_generic_image"] is False
    assert gpu["status"] == "deferred"
    assert gpu["base_profile"] == "llama-cpp-cpu"
    assert gpu["supported_platforms"] == ["linux/amd64"]
    assert gpu["ships_in_generic_image"] is False


def test_local_runtime_launcher_keeps_real_profile_behind_explicit_contract() -> None:
    launcher = (
        _repo_root() / "images" / "generic" / "scripts" / "start_local_runtime.sh"
    ).read_text(encoding="utf-8")

    assert "HEARTWOOD_LOCAL_RUNTIME_PROFILE:-stub-loopback" in launcher
    assert "local runtime must bind to loopback" in launcher
    assert "python images/generic/scripts/local_model_stub.py" in launcher
    assert "llama-cpp-cpu" in launcher
    assert "llama_cpp.server" in launcher
    assert "server support" in launcher
    assert "HEARTWOOD_LOCAL_MODEL_PATH" in launcher


def test_container_image_workflow_publishes_ghcr_tags() -> None:
    workflow = (_repo_root() / ".github" / "workflows" / "container-image.yml").read_text(
        encoding="utf-8"
    )

    assert "packages: write" in workflow
    assert "docker/setup-qemu-action@v4" in workflow
    assert "docker/setup-buildx-action@v4" in workflow
    assert "ghcr.io/${GITHUB_REPOSITORY,,}" in workflow
    assert "platforms: linux/amd64,linux/arm64" in workflow
    assert "cache-from: type=gha" in workflow
    assert "cache-to: type=gha,mode=max" in workflow
    assert ":dev-main" in workflow
    assert ":main" in workflow
    assert "${{ github.sha }}" in workflow


def test_container_smoke_workflow_runs_baseline_platform_matrix() -> None:
    workflow = (_repo_root() / ".github" / "workflows" / "container-smoke.yml").read_text(
        encoding="utf-8"
    )

    assert "fail-fast: false" in workflow
    assert "platform: [linux/amd64, linux/arm64]" in workflow
    assert "docker/setup-qemu-action@v4" in workflow
    assert "docker/setup-buildx-action@v4" in workflow
    assert "DOCKER_DEFAULT_PLATFORM: ${{ matrix.platform }}" in workflow
    assert "docker compose -f images/generic/compose.yaml run --rm heartwood" in workflow


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]
