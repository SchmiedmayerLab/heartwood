# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Contract tests for no-weight runtime and platform images."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any, ClassVar, cast

import pytest
from packaging.requirements import Requirement

from heartwood.gateway import verify_model_snapshot


def test_generic_image_packages_one_no_weight_runtime() -> None:
    dockerfile = _read("images/generic/Dockerfile")

    assert dockerfile.startswith("# syntax=docker/dockerfile:")
    assert "FROM node:24-trixie-slim AS webui-build" in dockerfile
    assert "uv sync --locked --no-dev --all-extras" in dockerfile
    assert "USER heartwood" in dockerfile
    assert 'CMD ["heartwood", "--help"]' in dockerfile
    assert "WORKDIR /workspace" in dockerfile
    assert "/workspace" in dockerfile
    assert "LITELLM_LOCAL_MODEL_COST_MAP=True" in dockerfile
    assert "OPENHANDS_SUPPRESS_BANNER=1" in dockerfile
    assert "llama-${LLAMA_CPP_VERSION}-bin-ubuntu-x64.tar.gz" in dockerfile
    assert "llama-${LLAMA_CPP_VERSION}-bin-ubuntu-arm64.tar.gz" in dockerfile
    assert "      jq \\" in dockerfile
    assert "sha256sum --check" in dockerfile
    assert "chown -R heartwood:heartwood /workspace /home/heartwood" in dockerfile
    assert "COPY --chown=heartwood:heartwood" not in dockerfile
    assert dockerfile.index("uv sync --locked --no-dev --all-extras") < dockerfile.index(
        "USER heartwood"
    )
    for line in dockerfile.splitlines():
        if "chown" in line:
            assert "/opt/heartwood" not in line
            assert "/opt/heartwood-vllm" not in line
            assert "/opt/llama.cpp" not in line
    for legacy_setting in (
        "HEARTWOOD_AGENT_BACKEND=",
        "HEARTWOOD_HOME=",
        "HEARTWOOD_WORKSPACE=",
        "HEARTWOOD_MODEL_CACHE=",
        "HEARTWOOD_MODEL_SETTINGS=",
        "HEARTWOOD_ACTION_SETTINGS=",
        "HEARTWOOD_SKILLS_DIR=",
        "HF_HOME=",
    ):
        assert legacy_setting not in dockerfile
    _assert_no_embedded_model_contract(dockerfile)


def test_docker_context_excludes_local_dependencies_and_generated_output() -> None:
    dockerignore = _read(".dockerignore")

    for pattern in (
        ".uv-cache",
        "**/node_modules",
        "**/dist",
        "**/coverage",
        "**/test-results",
    ):
        assert pattern in dockerignore


def test_platform_image_adds_heartwood_without_replacing_terra_runtime() -> None:
    generic = _read("images/generic/Dockerfile")
    platform = _read("images/platform/Dockerfile")
    manifest = _toml("images/platforms.toml")
    terra = manifest["platforms"]["terra"]

    for source in (
        "pyproject.toml uv.lock",
        "packages",
        "fixtures",
        "skills",
        "evals",
        "images",
        "README.md ACRONYMS.md",
        "docs",
        "design",
    ):
        assert source in generic
        assert source in platform
    for runtime_setting in (
        "ARG LLAMA_CPP_VERSION=b9937",
        "LITELLM_LOCAL_MODEL_COST_MAP=True",
        "OPENHANDS_SUPPRESS_BANNER=1",
    ):
        assert runtime_setting in generic
        assert runtime_setting in platform

    assert (
        "FROM --platform=${HEARTWOOD_PLATFORM_BASE_PLATFORM} ${HEARTWOOD_PLATFORM_BASE_IMAGE}"
        in platform
    )
    assert 'PATH="/opt/llama.cpp:${PATH}"' in platform
    assert "      jq \\" in platform
    assert "/opt/heartwood/.venv/bin:${PATH}" not in platform
    assert "ipykernel install" in platform
    assert "heartwood-workspace" not in platform
    assert "heartwood-project" not in platform
    assert "USER ${HEARTWOOD_PLATFORM_USER}" in platform
    assert "WORKDIR ${HEARTWOOD_PLATFORM_HOME}" in platform
    for legacy_setting in (
        "HEARTWOOD_AGENT_BACKEND=",
        "HEARTWOOD_HOME=",
        "HEARTWOOD_WORKSPACE=",
        "HEARTWOOD_MODEL_CACHE=",
        "HEARTWOOD_MODEL_SETTINGS=",
        "HEARTWOOD_ACTION_SETTINGS=",
        "HEARTWOOD_SKILLS_DIR=",
        "HF_HOME=",
    ):
        assert legacy_setting not in platform
    _assert_no_embedded_model_contract(platform)

    assert terra["runtime_target"] == "terra-runtime"
    assert terra["gpu_runtime_target"] == "terra-runtime-gpu-nvidia"
    assert terra["ci_target"] == "terra-ci"
    assert terra["runtime_tag"] == "edge-terra"
    assert terra["gpu_runtime_tag"] == "edge-terra-gpu-nvidia"
    assert terra["commit_runtime_tag"] == "sha-<git-sha>-terra"
    assert terra["commit_gpu_runtime_tag"] == "sha-<git-sha>-terra-gpu-nvidia"
    assert terra["bundles_model_artifact"] is False
    assert terra["supported_platforms"] == ["linux/amd64"]
    assert terra["manifest_media_type"] == "application/vnd.docker.distribution.manifest.v2+json"
    assert terra["config_media_type"] == "application/vnd.docker.container.image.v1+json"
    assert terra["publish_attestations"] is False


def test_openhands_sdk_is_the_only_agent_runtime_dependency() -> None:
    gateway = _toml("packages/gateway/pyproject.toml")
    agent_dependencies = gateway["project"]["optional-dependencies"]["agent"]
    pins = dict(_exact_package_pin(requirement) for requirement in agent_dependencies)

    assert pins == {"openhands-sdk": "1.36.0", "openhands-tools": "1.36.0"}
    assert "openhands-agent-server" not in _read("packages/gateway/pyproject.toml")
    assert "openhands-agent-server" not in _read("uv.lock")


def test_image_catalog_contains_only_explicit_verified_downloads() -> None:
    catalog = _toml("images/generic/local-runtime/model-catalog.toml")
    flavors = _toml("images/generic/image-flavors.toml")

    assert "explicit CLI or web request" in catalog["storage_policy"]
    assert set(catalog["models"]) == {
        "llama-cpp-stories260k-ci",
        "qwen25-7b-instruct-q4_k_m",
        "qwen25-coder-7b-instruct-q4_k_m",
    }
    for model in catalog["models"].values():
        artifact = _toml(model["artifact_manifest"])
        assert artifact["source_revision"] not in {"main", "master", "latest"}
        assert artifact["artifact_size_bytes"] > 0
        assert len(artifact["artifact_sha256"]) == 64
        assert "Not bundled" in artifact["redistribution"]

    assert flavors["flavors"]["runtime"]["bundles_model_artifact"] is False
    assert flavors["flavors"]["runtime_gpu_nvidia"]["bundles_model_artifact"] is False
    assert flavors["platform_flavors"]["terra_runtime"]["bundles_model_artifact"] is False
    assert (
        flavors["platform_flavors"]["terra_runtime_gpu_nvidia"]["bundles_model_artifact"] is False
    )
    assert "No Heartwood image contains model weights" in flavors["model_weight_policy"]


def test_bake_file_has_portable_and_explicit_nvidia_variants() -> None:
    bake = _read("docker-bake.hcl")

    assert _target_names(bake) == {
        "runtime",
        "runtime-gpu-nvidia",
        "_terra_common",
        "terra-runtime",
        "terra-runtime-gpu-nvidia",
        "terra-ci",
    }
    assert 'targets = ["runtime"]' in bake
    assert 'platforms = ["linux/amd64", "linux/arm64"]' in bake
    assert '"${IMAGE_NAME}:${IMAGE_CHANNEL}-gpu-nvidia"' in bake
    assert '"${IMAGE_NAME}:${IMAGE_CHANNEL}-terra-gpu-nvidia"' in bake
    assert bake.count('HEARTWOOD_GPU_RUNTIME = "vllm"') == 2
    assert "type=gha,scope=runtime-gpu-nvidia,mode=min" in bake
    assert "type=gha,scope=terra-runtime-gpu-nvidia,mode=min" in bake
    assert 'output = ["type=registry,oci-mediatypes=false"]' in bake
    assert "HEARTWOOD_BUNDLE_LOCAL_MODEL" not in bake
    assert "coder-7b" not in bake
    assert 'target "smoke"' not in bake
    assert 'target "providers"' not in bake


def test_gpu_runtime_is_isolated_pinned_and_no_weight() -> None:
    generic = _read("images/generic/Dockerfile")
    platform = _read("images/platform/Dockerfile")
    launcher = _read("images/gpu/start_vllm.sh")
    verifier = _read("images/gpu/verify_runtime.sh")
    lock = _read("images/gpu/vllm-requirements.txt")

    for dockerfile in (generic, platform):
        assert "uv venv /opt/heartwood-vllm --python 3.12" in dockerfile
        assert "uv pip sync --require-hashes" in dockerfile
        assert "images/gpu/vllm-requirements.txt" in dockerfile
        assert "HEARTWOOD_GPU_RUNTIME" in dockerfile
        assert "AS gpu-ci-validate" in dockerfile
        assert "RUN /opt/heartwood/images/gpu/verify_runtime.sh" in dockerfile
    assert generic.index("uv venv /opt/heartwood-vllm") < generic.index("COPY packages ./packages")
    assert platform.index("uv venv /opt/heartwood-vllm") < platform.index(
        "COPY packages ./packages"
    )
    assert platform.count("UV_CACHE_DIR=/root/.cache/uv") == 2
    for dockerfile in (generic, platform):
        for line in dockerfile.splitlines():
            if "chown" in line:
                assert "/opt/heartwood" not in line
                assert "/opt/heartwood-vllm" not in line
    assert "vllm-0.10.1.1%2Bcu118" in lock
    assert "certifi-2026.6.17-py3-none-any.whl" in lock
    assert "certifi==2022.12.7" not in lock
    assert "ray==2.55.0" in lock
    assert "setuptools==78.1.1" in lock
    assert "torch-2.7.1%2Bcu118-cp312-cp312-manylinux_2_28_x86_64.whl" in lock
    assert "torchaudio-2.7.1%2Bcu118-cp312-cp312-manylinux_2_28_x86_64.whl" in lock
    assert "torchvision-0.22.1%2Bcu118-cp312-cp312-manylinux_2_28_x86_64.whl" in lock
    assert "xformers-0.0.31-cp39-abi3-manylinux_2_28_x86_64.whl" in lock
    assert "transformers==4.57.6" in lock
    assert "nvidia-cuda-runtime-cu11==11.8.89" in lock
    assert "--extra-index-url https://download.pytorch.org/whl/cu118" not in lock
    assert "nvidia-cuda-runtime-cu13" not in lock
    assert "--hash=sha256:" in lock
    assert 'host="${HEARTWOOD_LOCAL_RUNTIME_HOST:-127.0.0.1}"' in launcher
    assert "--enable-auto-tool-choice" in launcher
    assert 'tool_parser="${HEARTWOOD_VLLM_TOOL_PARSER:-hermes}"' in launcher
    assert "VLLM_USE_FLASHINFER_SAMPLER" in launcher
    assert "huggingface.co" not in launcher
    assert "/opt/heartwood-vllm/bin/python" in verifier
    assert "import torch, vllm" in verifier
    assert 'torch.version.cuda == "11.8"' in verifier
    assert "-name '*.gguf' -o -name '*.safetensors'" in verifier
    assert "-name '*.bin' -size +10M" in verifier
    assert "compressed_tensors/transform/utils/hadamards.safetensors" in verifier
    assert "verify_no_model_artifacts /opt /home" in verifier
    assert "GPU runtime image contains a model artifact" in verifier
    assert os.access(_repo_root() / "images/gpu/verify_runtime.sh", os.X_OK)


@pytest.mark.parametrize("filename", ["small.gguf", "small.safetensors"])
def test_gpu_runtime_verifier_rejects_small_model_artifacts(tmp_path: Path, filename: str) -> None:
    artifact = tmp_path / filename
    artifact.write_bytes(b"synthetic-test-artifact")
    verifier = _repo_root() / "images/gpu/verify_runtime.sh"

    completed = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; verify_no_model_artifacts "$2"',
            "heartwood-gpu-verifier-test",
            str(verifier),
            str(tmp_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 65
    assert str(artifact) in completed.stderr


def test_gpu_runtime_verifier_allows_hash_locked_dependency_tensor(tmp_path: Path) -> None:
    runtime_root = tmp_path / "heartwood-vllm"
    runtime_asset = (
        runtime_root
        / "lib/python3.12/site-packages/compressed_tensors/transform/utils/hadamards.safetensors"
    )
    runtime_asset.parent.mkdir(parents=True)
    runtime_asset.write_bytes(b"synthetic-hadamard-transform")
    verifier = _repo_root() / "images/gpu/verify_runtime.sh"

    completed = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; verify_no_model_artifacts "$2"',
            "heartwood-gpu-verifier-test",
            str(verifier),
            str(runtime_root),
        ],
        check=False,
        capture_output=True,
        env={**os.environ, "HEARTWOOD_VLLM_ROOT": str(runtime_root)},
        text=True,
    )

    assert completed.returncode == 0
    assert completed.stderr == ""


def test_vllm_launcher_enforces_loopback_and_tool_calling(tmp_path: Path) -> None:
    model = tmp_path / "model"
    model.mkdir()
    arguments = tmp_path / "arguments.txt"
    executable = tmp_path / "vllm"
    executable.write_text(
        f"#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" > {arguments}\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    script = _repo_root() / "images/gpu/start_vllm.sh"
    env = {
        **os.environ,
        "HEARTWOOD_LOCAL_MODEL_PATH": str(model),
        "HEARTWOOD_VLLM_EXECUTABLE": str(executable),
        "HEARTWOOD_LOCAL_MODEL_ALIAS": "test-model",
    }

    completed = subprocess.run(["bash", str(script)], env=env, check=False)

    assert completed.returncode == 0
    values = arguments.read_text(encoding="utf-8").splitlines()
    assert values[:2] == ["serve", str(model)]
    assert values[values.index("--host") + 1] == "127.0.0.1"
    assert values[values.index("--served-model-name") + 1] == "test-model"
    assert values[values.index("--tool-call-parser") + 1] == "hermes"

    env["HEARTWOOD_LOCAL_RUNTIME_HOST"] = "0.0.0.0"
    denied = subprocess.run(["bash", str(script)], env=env, check=False)
    assert denied.returncode == 64


def test_carina_native_launch_requires_verified_synthetic_allocation() -> None:
    bootstrap = _read("deploy/carina/bootstrap.sh")
    launch_runtime = _read("packages/cli/src/heartwood/cli/_launch.py")
    environment = _toml("images/generic/image-flavors.toml")
    bootstrap_environment = _read("deploy/carina/environment.yml")

    assert "micromamba create" in bootstrap
    assert "micromamba install" in bootstrap
    assert "module load" in bootstrap
    assert "HEARTWOOD_MODULE_INIT" in bootstrap
    assert "/usr/share/lmod/lmod/init/profile" in bootstrap
    assert '"${root}/bootstrap/conda-meta"' in bootstrap
    assert "images/gpu/vllm-requirements.txt" in bootstrap
    assert '"${root}/vllm/bin/python"' in bootstrap
    assert "import torch" in bootstrap
    assert "import vllm" in bootstrap
    assert "VLLM_USE_FLASHINFER_SAMPLER=0" in bootstrap
    assert "ffmpeg" not in bootstrap_environment
    assert "SLURM_JOB_ID" in launch_runtime
    assert "LOCAL_SCRATCH_JOB" in launch_runtime
    assert "--inside-allocation" in launch_runtime
    assert "HEARTWOOD_PLATFORM=carina" in launch_runtime
    assert "_verify_local_model(selection)" in launch_runtime
    assert "_verify_local_model(selection, model_root=staged_source)" in launch_runtime
    assert 'env.get("LOCAL_SCRATCH_JOB"' in launch_runtime
    assert "allowed_names" in launch_runtime
    assert "result = {name: env[name] for name in allowed_names if name in env}" in launch_runtime
    assert '"OPENAI_API_KEY"' not in launch_runtime
    assert '"--model-source"' in launch_runtime
    assert "127.0.0.1:8765/v1/models" in launch_runtime
    assert '"sinfo", "--noheader", "--format=%P|%G|%a"' in launch_runtime
    assert "_SLURM_EXPORTED_ENVIRONMENT" in launch_runtime
    assert "--export=ALL" not in launch_runtime
    assert environment["flavors"]["runtime_gpu_nvidia"]["public_default"] is False


def test_carina_model_verifier_requires_exact_manifest_coverage(tmp_path: Path) -> None:
    model = tmp_path / "model"
    model.mkdir()
    weights = model / "weights.safetensors"
    weights.write_bytes(b"synthetic-weights")
    digest = hashlib.sha256(weights.read_bytes()).hexdigest()
    (model / "SHA256SUMS").write_text(f"{digest}  weights.safetensors\n", encoding="utf-8")
    verifier_source = _read("packages/gateway/src/heartwood/gateway/_model_snapshots.py")
    assert "file.read(1024 * 1024)" in verifier_source
    assert "os.O_NOFOLLOW" in verifier_source
    assert ".read_bytes()" not in verifier_source

    verify_model_snapshot(model)

    (model / "unlisted.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="unlisted"):
        verify_model_snapshot(model)
    (model / "unlisted.json").unlink()

    weights.unlink()
    weights.symlink_to(model / "missing-weights")
    with pytest.raises(ValueError, match="symbolic link"):
        verify_model_snapshot(model)


def test_native_release_assets_are_verified_before_installation() -> None:
    installer = _read("deploy/install.sh")
    packager = _read("deploy/package-native.sh")
    workflow = _read(".github/workflows/native-release.yml")
    main_workflow = _read(".github/workflows/main-validation.yml")
    release_workflow = _read(".github/workflows/create-release.yml")
    release_images = _read("deploy/promote-release-images.sh")
    smoke = _read("deploy/tests/native_installer_smoke.sh")

    assert "sha256sum --check --strict" in installer
    assert "--bundle" in installer
    assert "--dry-run" in installer
    assert "HEARTWOOD_INSTALL_ROOT" not in installer
    assert "HEARTWOOD_HOME" not in installer
    assert "HEARTWOOD_MODEL_CACHE" not in installer
    assert "exec %q" in installer
    assert "checksum manifest must contain exactly heartwood-native.tar.gz" in installer
    assert "[A-Za-z0-9._+-]{0,127}" in installer
    assert "git archive --format=tar HEAD" in packager
    assert "COPYFILE_DISABLE=1 tar --no-xattrs" in packager
    assert "workflow_call:" in workflow
    assert "uses: ./.github/workflows/native-release.yml" in main_workflow
    assert "name: Release Candidate Ready" in main_workflow
    assert "actions/attest@v4" in release_workflow
    assert "gh release create" in release_workflow
    assert "--draft" in release_workflow
    assert "--draft=false" in release_workflow
    assert "gh release delete" in release_workflow
    assert "existing draft targets a different commit" in release_workflow
    assert "Prepare Editable Release Draft" in release_workflow
    assert "release draft assets differ from the verified candidate" in release_workflow
    assert "--source-root ." in release_workflow
    assert "docker/login-action@v4" in release_workflow
    assert "--required-check 'Release Candidate Ready'" in release_workflow
    assert "environment: release" in release_workflow
    assert "verify_release_candidate.py" in release_workflow
    assert "promote-release-images.sh verify" in release_workflow
    assert "promote-release-images.sh promote" in release_workflow
    assert "observed media type:" in release_images
    assert "Linux platforms:" in release_images
    assert "Build And Verify Native Assets" in workflow
    assert "native_installer_smoke.sh" in workflow
    assert "installer accepted a corrupted checksum" in smoke
    assert "installer accepted an unsafe checksum manifest" in smoke


def test_gpu_publication_builds_only_explicit_main_variants() -> None:
    workflow = _read(".github/workflows/gpu-container-image.yml")
    dependency_review = _read(".github/workflows/dependency-review.yml")
    pull_request_build = workflow.split("  pull-request-build:\n", maxsplit=1)[1].split(
        "\n  build:\n", maxsplit=1
    )[0]
    main_build = workflow.split("  build:\n", maxsplit=1)[1].split("\n  promote:\n", maxsplit=1)[0]

    assert "runtime-gpu-nvidia" in workflow
    assert "terra-runtime-gpu-nvidia" in workflow
    assert "Build GPU candidate ${{ matrix.target }}" in workflow
    assert 'target=gpu-ci-validate"' in pull_request_build
    assert 'output=type=cacheonly"' in pull_request_build
    assert "output=type=docker" not in pull_request_build
    assert "docker/setup-buildx-action@v4" in pull_request_build
    assert "attest=type=sbom,disabled=true" in pull_request_build
    assert "attest=type=provenance,disabled=true" in pull_request_build
    assert "Promote GPU Channel Tags" in workflow
    assert "if: github.ref == 'refs/heads/main'" in workflow
    assert "push-by-digest=true" in workflow
    assert 'BUILDX_NO_DEFAULT_ATTESTATIONS: "1"' in workflow
    assert "--prefer-index=false" in workflow
    assert "application/vnd.docker.distribution.manifest.v2+json" in workflow
    assert "observed media type:" in workflow
    assert "Linux platforms:" in workflow
    assert 'docker pull --platform linux/amd64 "${CANDIDATE}"' in main_build
    assert "--entrypoint /opt/heartwood/images/gpu/verify_runtime.sh" in main_build
    assert "immutable GPU commit tag does not match" in workflow
    assert "refusing to move GPU channel tags from a stale main workflow" in workflow
    assert "promoted ${channel} digest does not match" in workflow
    assert "allow-ghsas: GHSA-w8v5-vhqr-4h9v" in dependency_review
    assert "GHSA-rrmf-rvhw-rf47" in dependency_review
    assert "patched release" in dependency_review


def test_vllm_advisory_exceptions_remain_isolated_to_gpu_dependencies() -> None:
    root = _repo_root()
    lock = root / "images/gpu/vllm-requirements.txt"
    input_file = root / "images/gpu/vllm.in"
    gpu_dependencies = {lock, input_file}
    dependency_files = {
        root / "uv.lock",
        *root.rglob("pyproject.toml"),
        *root.rglob("*requirements*.txt"),
        *root.rglob("*.in"),
    }
    declaration = re.compile(
        r'(?im)(?:^name\s*=\s*["\']|^|["\'\s])(diskcache|torch)(?:["\'\s<>=!~\[])'
    )
    unexpected = [
        path.relative_to(root).as_posix()
        for path in sorted(dependency_files)
        if path not in gpu_dependencies and declaration.search(path.read_text(encoding="utf-8"))
    ]

    assert unexpected == []
    text = lock.read_text(encoding="utf-8")
    assert "diskcache==5.6.3" in text
    assert "torch-2.7.1%2Bcu118-cp312-cp312-manylinux_2_28_x86_64.whl" in text
    assert "torch-2.7.1%2Bcu118-cp312-cp312-manylinux_2_28_x86_64.whl" in input_file.read_text(
        encoding="utf-8"
    )


def test_isolated_smoke_uses_real_openhands_sdk_without_weights() -> None:
    compose = _read("images/generic/compose.yaml")
    smoke = _read("images/generic/scripts/offline_stack_smoke.sh")
    capable = _read("images/generic/scripts/capable_model_e2e.sh")
    model_stub = _read("images/generic/scripts/local_model_stub.py")

    assert "network_mode: none" in compose
    assert "read_only: true" in compose
    assert "cap_drop:" in compose
    assert "- ALL" in compose
    assert "no-new-privileges:true" in compose
    assert "HEARTWOOD_BUNDLE_LOCAL_MODEL" not in compose
    assert "HEARTWOOD_AGENT_BACKEND" not in compose
    assert "image: heartwood-runtime-smoke:local" in compose
    assert "HEARTWOOD_LOCAL_RUNTIME_PROFILE=stub-loopback" in smoke
    assert "HEARTWOOD_SMOKE_PROJECT:-/tmp/heartwood-offline-project" in smoke
    assert "HEARTWOOD_CAPABLE_PROJECT:-/tmp/heartwood-capable-project" in capable
    assert 'workspace = Path.cwd() / ".heartwood" / "sessions"' in smoke
    assert 'cohort_path="${project}/cohort-summary.json"' in capable
    assert "models refresh local" in smoke
    assert "models connect local heartwood-local-runtime" in smoke
    assert "models add inactive-smoke" in smoke
    assert "HEARTWOOD_UNUSED_MODEL_API_KEY" in smoke
    assert "HEARTWOOD_UNUSED_MODEL_API_KEY" in model_stub
    assert 'self.path != "/v1/models"' in model_stub
    assert "models validate local" in smoke
    assert "chat" in smoke
    assert "call-heartwood-reference-analysis" in smoke
    assert "call-heartwood-offline-smoke" in smoke
    assert "cohort-summary.json" in smoke
    assert " allow " in smoke
    assert " reject " in smoke
    assert "Action set approved" in smoke
    assert "Action set denied" in smoke
    assert "audit export" in smoke
    assert "load_skills_from_dir" in smoke
    assert "start_agent_server" not in smoke
    assert "--local-model" not in smoke
    assert "model-call" not in smoke
    assert "command -v jq" in smoke


def test_launch_scripts_are_valid_and_require_explicit_local_artifact() -> None:
    scripts = (
        "images/generic/scripts/capable_model_e2e.sh",
        "images/generic/scripts/offline_stack_smoke.sh",
        "images/generic/scripts/container_persistence_smoke.sh",
        "images/generic/scripts/local_inference_smoke.sh",
        "images/generic/scripts/start_local_runtime.sh",
        "images/platform/scripts/terra_image_smoke.sh",
        "images/platform/scripts/terra_jupyter_contract_smoke.sh",
        "images/platform/scripts/terra_jupyter_launch_smoke.sh",
        "images/platform/scripts/terra_managed_launch_smoke.sh",
        "images/platform/scripts/terra_project_persistence_smoke.sh",
    )
    for script in scripts:
        completed = subprocess.run(
            ["bash", "-n", str(_repo_root() / script)],
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, f"{script}: {completed.stderr}"

    local_runtime = _read("images/generic/scripts/start_local_runtime.sh")
    assert 'model_path="${HEARTWOOD_LOCAL_MODEL_PATH:-}"' in local_runtime
    assert "HEARTWOOD_LOCAL_MODEL_CONTEXT:-32768" in local_runtime
    assert "HEARTWOOD_LOCAL_MODEL_CONTEXT:-32768" in _read(
        "images/generic/scripts/capable_model_e2e.sh"
    )
    assert "HEARTWOOD_LOCAL_MODEL_CONTEXT:-32768" in _read("images/gpu/start_vllm.sh")
    assert '"${script_dir}/local_model_stub.py"' in local_runtime
    assert "runtime_root=" not in local_runtime
    assert "mounted or downloaded GGUF file" in local_runtime
    local_smoke = _read("images/generic/scripts/local_inference_smoke.sh")
    assert "mktemp" in local_smoke
    assert "/tmp/heartwood-llama-smoke.log" not in local_smoke
    assert 'rm -f "${log_file}"' in local_smoke
    assert not (_repo_root() / "images/generic/scripts/start_demo_stack.sh").exists()
    assert not (_repo_root() / "images/generic/scripts/start_web_ui.sh").exists()

    jupyter_smoke = _read("images/generic/scripts/terra_jupyter_demo_smoke.py")
    assert '"chat"' in jupyter_smoke
    assert '"approve"' in jupyter_smoke
    assert '"confirmation.requested"' in jupyter_smoke
    assert '"tool.execution.recorded"' in jupyter_smoke
    assert "os.chdir(PROJECT_ROOT)" in jupyter_smoke
    assert "NotebookSession(session_id=" in jupyter_smoke
    assert '"project/readiness"' in jupyter_smoke

    terra_launch = _read("images/platform/scripts/terra_jupyter_launch_smoke.sh")
    assert "heartwood serve --host 0.0.0.0" in terra_launch
    assert "project/readiness" in terra_launch
    assert "proxy/${gateway_port}/" in terra_launch

    terra_managed_launch = _read("images/platform/scripts/terra_managed_launch_smoke.sh")
    assert "heartwood launch --web" in terra_managed_launch
    assert 'payload.get("platform_id") != "terra"' in terra_managed_launch
    assert 'payload.get("state") != "ready"' in terra_managed_launch
    assert 'payload.get("project_root")' in terra_managed_launch

    terra_persistence = _read("images/platform/scripts/terra_project_persistence_smoke.sh")
    assert '--volume "${state_volume}:/home/jupyter"' in terra_persistence
    assert terra_persistence.count("terra_image_smoke.sh") == 2
    assert "terra-project-persistence replay" in terra_persistence
    assert "test ! -e /home/jupyter/.heartwood" in terra_persistence


def test_publish_workflow_uses_digest_merge_and_clean_public_tags() -> None:
    publish = _read(".github/workflows/container-image.yml")
    smoke = _read(".github/workflows/container-smoke.yml")
    compose = _read("images/generic/compose.yaml")
    offline_guide = _read("docs/getting-started-offline.md")
    capable_model = _read("images/generic/scripts/capable_model_e2e.sh")

    assert "packages: write" in publish
    assert "push-by-digest=true" in publish
    assert publish.count("images/scripts/read_buildx_digest.py") == 2
    assert ".containerimage.descriptor.digest" not in publish
    assert ".terra-runtime" not in publish
    assert "actions/upload-artifact@v7" in publish
    assert "actions/download-artifact@v8" in publish
    assert '"${IMAGE_NAME}:${IMAGE_CHANNEL}"' in publish
    assert '"${IMAGE_NAME}:sha-${GIT_SHA}"' in publish
    assert 'docker pull --platform "${DOCKER_PLATFORM}" "${CANDIDATE_REFERENCE}"' in publish
    assert "Run staged generic OpenHands smoke" in publish
    assert "Run staged generic local inference smoke" in publish
    assert 'docker run --rm --init --platform "${DOCKER_PLATFORM}"' in publish
    assert "--network none --read-only" in publish
    assert '"${IMAGE_NAME}:${IMAGE_CHANNEL}-terra"' in publish
    assert publish.count("uid=10001,gid=10001,mode=0700") == 4
    assert "verify_registry_manifest.py" in publish
    assert "--prefer-index=false" in publish
    assert '--reference "${CANDIDATE_DIGEST}"' in publish
    assert publish.count("^sha256:[0-9a-f]{64}$") == 2
    assert publish.count("if: github.ref == 'refs/heads/main'") == 3
    assert "immutable generic commit tag already exists with a different manifest" in publish
    assert "newly created generic commit tag does not match validated candidate manifest" in publish
    assert "immutable Terra commit tag already exists with a different digest" in publish
    assert "newly created Terra commit tag does not match staged candidate digest" in publish
    assert "promoted generic channel digest does not match the immutable commit tag" in publish
    assert "promoted Terra channel digest does not match the immutable commit tag" in publish
    assert (
        'if inspect_output="$(docker buildx imagetools inspect "${commit_ref}" 2>/dev/null)"; then'
        in publish
    )
    assert 'if commit_digest="$(docker buildx imagetools inspect' not in publish
    assert "refusing to move the generic channel tag from a stale main workflow" in publish
    assert "refusing to move the Terra channel tag from a stale main workflow" in publish
    assert publish.index("Build and stage image by digest") < publish.index(
        "Run staged generic OpenHands smoke"
    )
    assert publish.index("Run staged generic local inference smoke") < publish.index(
        "Upload validated image digest"
    )
    assert publish.index("Upload validated image digest") < publish.index(
        "Create and verify immutable generic commit tag"
    )
    assert publish.index("Create and verify immutable generic commit tag") < publish.index(
        "Promote generic moving tag"
    )
    assert publish.index("Build and stage Terra image by digest") < publish.index(
        "Run staged Terra current-directory persistence smoke"
    )
    assert publish.index("Run staged Terra current-directory persistence smoke") < publish.index(
        "Run staged Terra OpenHands smoke"
    )
    assert publish.index("Run staged Terra managed local-model launch smoke") < publish.index(
        "Run staged Terra local inference smoke"
    )
    assert publish.index("Run staged Terra local inference smoke") < publish.index(
        "Create and verify immutable Terra commit tag"
    )
    assert publish.index("Create and verify immutable Terra commit tag") < publish.index(
        "Promote Terra moving tag"
    )
    for stale_tag in ("edge-smoke", "edge-providers", "coder-7b", '-amd64"', '-arm64"'):
        assert stale_tag not in publish

    assert "Generic OpenHands smoke" in smoke
    assert "platform: linux/amd64" in smoke
    assert "platform: linux/arm64" in smoke
    assert "runner: ubuntu-24.04-arm" in smoke
    assert "runtime runtime-gpu-nvidia" in smoke
    assert "terra-runtime terra-runtime-gpu-nvidia terra-ci" in smoke
    assert "edge-terra-ci" in smoke
    assert "Run Terra current-directory persistence smoke" in smoke
    assert "Run Terra managed local-model launch smoke" in smoke
    assert "terra_managed_launch_smoke.sh" in smoke
    assert "terra_project_persistence_smoke.sh" in smoke
    assert "images/generic/scripts/offline_stack_smoke.sh" in smoke
    assert "HEARTWOOD_SMOKE_PROJECT=/home/jupyter/synthetic-agent-analysis" in smoke
    assert "HEARTWOOD_TERRA_DEMO_PROJECT_ROOT=/home/jupyter/synthetic-notebook-analysis" in smoke
    assert "container_persistence_smoke.sh" in smoke
    assert "Download and verify CI-only model fixture" in smoke
    assert "heartwood models download llama-cpp-stories260k-ci" in smoke
    assert "--volume heartwood-ci-project:/workspace" in smoke
    assert "--volume heartwood-terra-ci-project:/home/jupyter" in smoke
    assert "/workspace/.heartwood/models/llama-cpp-stories260k-ci" in smoke
    assert "/home/jupyter/model-analysis/.heartwood/models/llama-cpp-stories260k-ci" in smoke
    assert smoke.count("local_inference_smoke.sh") == 2
    assert 'f"http://127.0.0.1:{port}/health"' in _read(
        "images/generic/scripts/local_inference_smoke.sh"
    )
    assert "run_capable_model" in smoke
    assert "github.event_name == 'workflow_dispatch'" in smoke
    assert "qwen25-7b-instruct-q4_k_m" in smoke
    assert "capable_model_e2e.sh" in smoke
    assert "--network none --read-only" in smoke
    assert smoke.count("uid=10001,gid=10001,mode=0700") == 2
    assert compose.count("uid=10001,gid=10001,mode=0700") == 2
    assert offline_guide.count("-v heartwood-project:/workspace") == 2
    assert "remains in the current project's `.heartwood/models/` directory" in offline_guide
    assert "not 1 <= len(terminal_executions) <= 3" in capable_model
    assert "not 1 <= len(tool_executions) <= 3" in capable_model
    assert "&& cat cohort-summary.json" in capable_model
    assert 'f"http://127.0.0.1:{port}/health"' in capable_model
    assert "llama.cpp runtime log (last 200 lines)" in capable_model
    assert "cohort_path.is_file()" in capable_model
    assert 'summary["source_participant_count"] != 24' in capable_model
    assert 'summary["participant_count"] != 20' in capable_model
    assert 'checks["row_values_exported"] is not False' in capable_model


def test_buildx_metadata_reader_handles_runtime_target_names(tmp_path: Path) -> None:
    digest = "sha256:" + ("a" * 64)
    metadata = tmp_path / "metadata.json"
    metadata.write_text(
        json.dumps(
            {
                "runtime": {"containerimage.digest": digest},
                "terra-runtime": {"containerimage.digest": digest},
            }
        ),
        encoding="utf-8",
    )
    script = _repo_root() / "images/scripts/read_buildx_digest.py"

    for target in ("runtime", "terra-runtime"):
        completed = subprocess.run(
            [sys.executable, str(script), str(metadata), target],
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, completed.stderr
        assert completed.stdout.strip() == digest

    missing = subprocess.run(
        [sys.executable, str(script), str(metadata), "missing-target"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert missing.returncode != 0
    assert "target 'missing-target' is missing" in missing.stderr

    metadata.write_text(
        json.dumps({"runtime": {"containerimage.digest": "invalid"}}),
        encoding="utf-8",
    )
    invalid = subprocess.run(
        [sys.executable, str(script), str(metadata), "runtime"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert invalid.returncode != 0
    assert "has no valid container image digest" in invalid.stderr


def test_platform_registry_verifier_checks_only_public_terra_tags(tmp_path: Path) -> None:
    candidate_digest = "sha256:" + ("c" * 64)
    _RegistryHandler.accepted_tags = {
        "edge-terra",
        "sha-abc123-terra",
        candidate_digest,
    }
    _RegistryHandler.requested_accept_headers = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RegistryHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        manifest = tmp_path / "platforms.toml"
        manifest.write_text(
            f"""schema_version = "heartwood.platform-images.v1"
image_name = "127.0.0.1:{server.server_port}/test/heartwood"
moving_channel = "edge"

[platforms.terra]
manifest_media_type = "application/vnd.docker.distribution.manifest.v2+json"
config_media_type = "application/vnd.docker.container.image.v1+json"
allow_non_platform_manifests = false
supported_platforms = ["linux/amd64"]
image_user = "jupyter"
working_dir = "/home/jupyter"
entrypoint = ["/opt/conda/bin/jupyter", "notebook"]
exposed_ports = ["8000/tcp"]
required_env = ["HEARTWOOD_PYTHON=/opt/heartwood/.venv/bin/python"]
forbidden_path_entries = ["/opt/heartwood/.venv/bin"]
runtime_tag = "edge-terra"
commit_runtime_tag = "sha-<git-sha>-terra"

[platforms.terra.required_env_contains]
PATH = ["/opt/llama.cpp", "/opt/conda/bin", "/usr/local/bin"]
LD_LIBRARY_PATH = ["/opt/llama.cpp"]
JUPYTER_PATH = ["/opt/conda/share/jupyter"]
""",
            encoding="utf-8",
        )
        completed = subprocess.run(
            [
                sys.executable,
                str(_repo_root() / "images/platform/scripts/verify_registry_manifest.py"),
                "--manifest",
                str(manifest),
                "--platform",
                "terra",
                "--git-sha",
                "abc123",
                "--registry-scheme",
                "http",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        candidate = subprocess.run(
            [
                sys.executable,
                str(_repo_root() / "images/platform/scripts/verify_registry_manifest.py"),
                "--manifest",
                str(manifest),
                "--platform",
                "terra",
                "--reference",
                candidate_digest,
                "--registry-scheme",
                "http",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        invalid = subprocess.run(
            [
                sys.executable,
                str(_repo_root() / "images/platform/scripts/verify_registry_manifest.py"),
                "--manifest",
                str(manifest),
                "--platform",
                "terra",
                "--reference",
                "../invalid",
                "--registry-scheme",
                "http",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        invalid_manifest = tmp_path / "invalid-platforms.toml"
        invalid_manifest.write_text(
            manifest.read_text(encoding="utf-8").replace(
                'runtime_tag = "edge-terra"',
                'runtime_tag = "invalid:tag"',
            ),
            encoding="utf-8",
        )
        invalid_declared_tag = subprocess.run(
            [
                sys.executable,
                str(_repo_root() / "images/platform/scripts/verify_registry_manifest.py"),
                "--manifest",
                str(invalid_manifest),
                "--platform",
                "terra",
                "--git-sha",
                "abc123",
                "--registry-scheme",
                "http",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert candidate.returncode == 0, candidate.stdout + candidate.stderr
    assert invalid.returncode != 0
    assert "invalid registry tag or sha256 digest" in invalid.stderr
    assert invalid_declared_tag.returncode != 0
    assert "invalid registry tag or sha256 digest" in invalid_declared_tag.stderr
    assert completed.stdout.count("Verifying") == 2
    assert f"test/heartwood@{candidate_digest}" in candidate.stdout
    assert _RegistryHandler.requested_accept_headers == [
        _RegistryHandler.manifest_media_type,
        _RegistryHandler.manifest_media_type,
        _RegistryHandler.manifest_media_type,
    ]


def _assert_no_embedded_model_contract(dockerfile: str) -> None:
    for forbidden in (
        "HEARTWOOD_BUNDLE_LOCAL_MODEL",
        "HEARTWOOD_LOCAL_MODEL_MANIFEST",
        "download_model_artifact.py",
        "HEARTWOOD_LOCAL_MODEL_PATH=",
        "HEARTWOOD_AGENT_BACKEND=",
        "HEARTWOOD_HOME=",
        "HEARTWOOD_WORKSPACE=",
        "HEARTWOOD_MODEL_CACHE=",
        "HEARTWOOD_PROVIDER_CONFIG",
        "HEARTWOOD_AGENT_SERVER",
        "agent-server",
    ):
        assert forbidden not in dockerfile


def _target_names(bake: str) -> set[str]:
    return {
        line.removeprefix('target "').split('"', 1)[0]
        for line in bake.splitlines()
        if line.startswith('target "')
    }


def _read(path: str) -> str:
    return (_repo_root() / path).read_text(encoding="utf-8")


def _toml(path: str) -> dict[str, Any]:
    return tomllib.loads(_read(path))


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _exact_package_pin(requirement: str) -> tuple[str, str]:
    parsed = Requirement(requirement)
    specifiers = list(parsed.specifier)
    assert len(specifiers) == 1
    specifier = specifiers[0]
    assert specifier.operator == "=="
    return parsed.name, specifier.version


class _RegistryHandler(BaseHTTPRequestHandler):
    manifest_media_type: ClassVar[str] = "application/vnd.docker.distribution.manifest.v2+json"
    config_media_type: ClassVar[str] = "application/vnd.docker.container.image.v1+json"
    token: ClassVar[str] = "registry-token"
    accepted_tags: ClassVar[set[str]] = set()
    requested_accept_headers: ClassVar[list[str]] = []

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/token":
            self._write_json(200, "application/json", {"token": self.token})
            return
        if self.headers.get("Authorization") != f"Bearer {self.token}":
            server = cast(ThreadingHTTPServer, self.server)
            self.send_response(401)
            self.send_header(
                "WWW-Authenticate",
                f'Bearer realm="http://127.0.0.1:{server.server_port}/token",service="registry.test",scope="repository:test/heartwood:pull"',
            )
            self.end_headers()
            return
        if path.startswith("/v2/test/heartwood/manifests/"):
            tag = path.rsplit("/", 1)[1]
            if tag not in self.accepted_tags:
                self._write_json(404, "application/json", {"error": "unknown tag"})
                return
            self.requested_accept_headers.append(self.headers.get("Accept", ""))
            self._write_json(
                200,
                self.manifest_media_type,
                {
                    "schemaVersion": 2,
                    "mediaType": self.manifest_media_type,
                    "config": {
                        "mediaType": self.config_media_type,
                        "size": 54,
                        "digest": "sha256:config",
                    },
                    "layers": [],
                },
            )
            return
        if path == "/v2/test/heartwood/blobs/sha256:config":
            self._write_json(
                200,
                "application/json",
                {
                    "os": "linux",
                    "architecture": "amd64",
                    "config": {
                        "User": "jupyter",
                        "WorkingDir": "/home/jupyter",
                        "Entrypoint": ["/opt/conda/bin/jupyter", "notebook"],
                        "ExposedPorts": {"8000/tcp": {}},
                        "Env": [
                            "PATH=/opt/llama.cpp:/opt/conda/bin:/usr/local/bin:/usr/bin",
                            "LD_LIBRARY_PATH=/opt/llama.cpp",
                            "JUPYTER_PATH=/opt/conda/share/jupyter",
                            "HEARTWOOD_PYTHON=/opt/heartwood/.venv/bin/python",
                        ],
                    },
                },
            )
            return
        self._write_json(404, "application/json", {"error": "unknown path"})

    def log_message(self, _message_format: str, *_args: object) -> None:
        return

    def _write_json(self, status: int, content_type: str, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
