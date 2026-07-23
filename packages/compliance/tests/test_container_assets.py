# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Contract tests for no-weight runtime and platform images."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tomllib
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any, ClassVar, cast

import pytest
from packaging.requirements import Requirement

from heartwood.gateway import verify_model_snapshot


def test_generic_image_packages_one_no_weight_runtime() -> None:
    dockerfile = _read("images/Dockerfile")
    llama_installer = _read("deploy/install-llama-cpp.sh")
    profiles = _toml("images/generic/local-runtime/profiles.toml")

    assert dockerfile.startswith("# syntax=docker/dockerfile:")
    assert "FROM --platform=$BUILDPLATFORM node:24-trixie-slim AS webui-build" in dockerfile
    assert "uv sync --locked --no-dev --all-extras" in dockerfile
    assert "USER ${HEARTWOOD_RUNTIME_USER}" in dockerfile
    assert 'CMD ["heartwood", "--help"]' in dockerfile
    assert "WORKDIR ${HEARTWOOD_WORKDIR}" in dockerfile
    assert "/workspace" in dockerfile
    assert "LITELLM_LOCAL_MODEL_COST_MAP=True" in dockerfile
    assert "OPENHANDS_SUPPRESS_BANNER=1" in dockerfile
    assert "COPY deploy/install-llama-cpp.sh" in dockerfile
    assert "heartwood-install-llama-cpp /opt/llama.cpp" in dockerfile
    assert "llama-${version}-bin-ubuntu-x64.tar.gz" in llama_installer
    assert "llama-${version}-bin-ubuntu-arm64.tar.gz" in llama_installer
    assert "for package in ca-certificates curl git jq libgomp1 tmux; do" in dockerfile
    assert "sha256sum --check --strict" in llama_installer
    assert 'chmod 755 "${staging}"' in llama_installer
    llama_profile = profiles["profiles"]["llama-cpp-cpu"]
    installer_version = re.search(r'^version="([^"]+)"$', llama_installer, re.MULTILINE)
    assert installer_version is not None
    assert installer_version.group(1) in llama_profile["runtime_dependency"]
    for runtime_artifact in llama_profile["runtime_artifacts"]:
        asset, digest = runtime_artifact.split(" SHA-256 ", maxsplit=1)
        assert asset.replace(installer_version.group(1), "${version}") in llama_installer
        assert digest in llama_installer
    assert "/etc/ld.so.conf.d/heartwood-llama.conf" in dockerfile
    assert "ldconfig" in dockerfile
    assert 'runtime_group="$(id -gn "${HEARTWOOD_RUNTIME_USER}")"' in dockerfile
    assert '--owner="${HEARTWOOD_RUNTIME_USER}" --group="${runtime_group}"' in dockerfile
    assert '"${HEARTWOOD_RUNTIME_HOME}/.cache/flashinfer"' in dockerfile
    assert '"${HEARTWOOD_RUNTIME_HOME}/.cache/huggingface"' in dockerfile
    assert '"${HEARTWOOD_RUNTIME_HOME}/.cache/vllm"' in dockerfile
    assert 'chown -R "${HEARTWOOD_RUNTIME_USER}:${runtime_group}"' in dockerfile
    assert "COPY --chown=heartwood:heartwood" not in dockerfile
    assert dockerfile.index("uv sync --locked --no-dev --all-extras") < dockerfile.index(
        "USER ${HEARTWOOD_RUNTIME_USER}"
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
    dockerfile = _read("images/Dockerfile")
    bake = _read("docker-bake.hcl")
    manifest = _toml("images/platforms.toml")
    terra = manifest["platforms"]["terra"]

    for source in (
        "pyproject.toml uv.lock",
        "packages",
        "fixtures",
        "skills",
        "evals",
        "images",
        "README.md NOTICE",
        "documentation",
    ):
        assert source in dockerfile
    for runtime_setting in (
        "LITELLM_LOCAL_MODEL_COST_MAP=True",
        "OPENHANDS_SUPPRESS_BANNER=1",
    ):
        assert runtime_setting in dockerfile

    assert (
        "FROM --platform=${HEARTWOOD_BASE_PLATFORM} ${HEARTWOOD_BASE_IMAGE} "
        "AS heartwood-runtime-base"
    ) in dockerfile
    assert 'PATH="/opt/llama.cpp:${PATH}"' in dockerfile
    assert "for package in ca-certificates curl git jq libgomp1 tmux; do" in dockerfile
    assert "dpkg-query --show --showformat='${db:Status-Abbrev}'" in dockerfile
    assert 'if [ -n "${missing_packages}" ]; then' in dockerfile
    assert "/opt/heartwood/.venv/bin:${PATH}" not in dockerfile
    assert "ipykernel install" in dockerfile
    assert '--env IPYTHONDIR "/tmp/heartwood-ipython"' in dockerfile
    assert "heartwood-workspace" not in dockerfile
    assert "heartwood-project" not in dockerfile
    assert "USER ${HEARTWOOD_RUNTIME_USER}" in dockerfile
    assert "WORKDIR ${HEARTWOOD_WORKDIR}" in dockerfile
    assert "HEARTWOOD_GPU_RUNTIME=${HEARTWOOD_GPU_RUNTIME}" in dockerfile
    assert "HEARTWOOD_IMAGE_FLAVOR=${HEARTWOOD_IMAGE_FLAVOR}" in dockerfile
    assert "HEARTWOOD_PLATFORM=${HEARTWOOD_PLATFORM}" in dockerfile
    assert "HEARTWOOD_PLATFORM_HOME=${HEARTWOOD_RUNTIME_HOME}" in dockerfile
    assert "install -d --mode=0755 \\" in dockerfile
    assert '"${HEARTWOOD_RUNTIME_HOME}/.cache/flashinfer"' in dockerfile
    assert 'dockerfile = "images/Dockerfile"' in bake
    assert 'target = "runtime-image"' in bake
    assert 'target = "platform-runtime-image"' in bake
    assert 'HEARTWOOD_BASE_IMAGE = "${TERRA_BASE_IMAGE}"' in bake
    assert 'HEARTWOOD_BASE_PLATFORM = "linux/amd64"' in bake
    assert 'HEARTWOOD_CREATE_USER = "false"' in bake
    assert 'HEARTWOOD_INSTALL_JUPYTER_KERNEL = "true"' in bake
    assert not (_repo_root() / "images/generic/Dockerfile").exists()
    assert not (_repo_root() / "images/platform/Dockerfile").exists()
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

    assert terra["runtime_target"] == "terra-runtime"
    assert terra["gpu_runtime_target"] == "terra-runtime-gpu-nvidia"
    assert terra["ci_target"] == "terra-ci"
    assert terra["runtime_tag"] == "edge-terra"
    assert terra["gpu_runtime_tag"] == "edge-terra-gpu-nvidia"
    assert terra["commit_runtime_tag"] == "sha-<git-sha>-terra"
    assert terra["commit_gpu_runtime_tag"] == "sha-<git-sha>-terra-gpu-nvidia"
    assert terra["gpu_runtime"] == "vLLM 0.25.1+cu129 with PyTorch 2.11.0+cu129"
    assert terra["bundles_model_artifact"] is False
    assert terra["supported_platforms"] == ["linux/amd64"]
    assert terra["manifest_media_type"] == "application/vnd.docker.distribution.manifest.v2+json"
    assert terra["config_media_type"] == "application/vnd.docker.container.image.v1+json"
    assert terra["publish_attestations"] is False
    assert terra["ci_required"] is True
    assert terra["live_workspace_validation_required"] is False
    evidence_names = {item["name"] for item in terra["required_evidence"]}
    assert evidence_names == {"local-ci-smoke", "main-publish-real-terra"}


def test_container_smoke_uses_bake_as_the_heartwood_build_contract() -> None:
    workflow = _read(".github/workflows/container-smoke.yml")

    assert workflow.count("docker buildx bake --file docker-bake.hcl --call=check") == 2
    assert "--set runtime.tags=heartwood-capable:local" in workflow
    assert "--build-arg HEARTWOOD_" not in workflow
    assert "--file images/Dockerfile" not in workflow


def test_openhands_sdk_is_the_only_agent_runtime_dependency() -> None:
    gateway = _toml("packages/gateway/pyproject.toml")
    dependencies = gateway["project"]["dependencies"]
    pins = dict(
        _exact_package_pin(requirement)
        for requirement in dependencies
        if Requirement(requirement).name.startswith("openhands-")
    )

    assert pins == {"openhands-sdk": "1.36.1", "openhands-tools": "1.36.1"}
    assert "optional-dependencies" not in gateway["project"]
    assert "openhands-agent-server" not in _read("packages/gateway/pyproject.toml")
    assert "openhands-agent-server" not in _read("uv.lock")


def test_runtime_image_sets_the_release_version_label() -> None:
    dockerfile = _read("images/Dockerfile")
    bake = _read("docker-bake.hcl")

    assert "ARG HEARTWOOD_VERSION=development" in dockerfile
    assert 'org.opencontainers.image.version="${HEARTWOOD_VERSION}"' in dockerfile
    assert 'variable "HEARTWOOD_VERSION"' in bake
    assert 'default = "0.2.0-beta.7"' in bake
    assert bake.count('HEARTWOOD_VERSION = "${HEARTWOOD_VERSION}"') == 2


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
    assert bake.count('dockerfile = "images/Dockerfile"') == 2
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
    dockerfile = _read("images/Dockerfile")
    installer = _read("images/gpu/install_runtime.sh")
    launcher = _read("images/gpu/start_vllm.sh")
    verifier = _read("images/gpu/verify_runtime.sh")
    runtime_contract = _toml("images/gpu/compatibility.toml")
    runtime_verifier = _read("images/gpu/verify_vllm.py")
    executable = _read("images/gpu/heartwood-vllm")
    lock = _read("images/gpu/vllm-requirements.txt")
    exclusions = _read("images/gpu/vllm-exclusions.txt")
    overrides = _read("images/gpu/vllm-overrides.txt")

    assert "images/gpu/install_runtime.sh" in dockerfile
    assert "--target /opt/heartwood-vllm --python 3.12" in dockerfile
    assert "HEARTWOOD_GPU_RUNTIME" in dockerfile
    assert "AS gpu-ci-validate" in dockerfile
    assert "RUN /opt/heartwood/images/gpu/verify_runtime.sh" in dockerfile
    assert dockerfile.index("images/gpu/install_runtime.sh") < dockerfile.index(
        "COPY packages ./packages"
    )
    assert dockerfile.count("UV_CACHE_DIR=/root/.cache/uv") == 2
    for line in dockerfile.splitlines():
        if "chown" in line:
            assert "/opt/heartwood" not in line
            assert "/opt/heartwood-vllm" not in line
    assert '"${uv}" venv "${target}"' in installer
    assert '"${uv}" pip sync' in installer
    assert '"${runtime_sources}/vllm-requirements.txt"' in installer
    assert '"${runtime_sources}/verify_vllm.py"' in installer
    assert '"${runtime_sources}/compatibility.toml"' in installer
    assert '"${runtime_sources}/heartwood-vllm"' in installer
    assert "vllm-0.25.1%2Bcu129-cp38-abi3-manylinux_2_28_x86_64.whl" in lock
    assert "torch-2.11.0%2Bcu129-cp312-cp312-manylinux_2_28_x86_64.whl" in lock
    assert "torchaudio-2.11.0%2Bcu129-cp312-cp312-manylinux_2_28_x86_64.whl" in lock
    assert "torchvision-0.26.0%2Bcu129-cp312-cp312-manylinux_2_28_x86_64.whl" in lock
    assert "nvidia-cuda-runtime-cu12==12.9.79" in lock
    assert "flashinfer-python==0.6.13" in lock
    assert "setuptools==83.0.0" in lock
    assert "setuptools==83.0.0" in overrides
    assert "xgrammar==0.2.3" in lock
    assert "--extra-index-url" not in lock
    for package in (
        "cuda-tile==",
        "nvidia-cuda-crt==",
        "nvidia-cuda-nvcc==",
        "nvidia-cuda-runtime==",
        "nvidia-cuda-tileiras==",
        "nvidia-nvvm==",
    ):
        assert package not in lock
        assert package.removesuffix("==") in exclusions
    assert "--hash=sha256:" in lock
    assert 'host="${HEARTWOOD_LOCAL_RUNTIME_HOST:-127.0.0.1}"' in launcher
    assert "--enable-auto-tool-choice" in launcher
    assert 'tool_parser="${HEARTWOOD_VLLM_TOOL_PARSER:-hermes}"' in launcher
    assert 'flashinfer_sampler="${HEARTWOOD_VLLM_USE_FLASHINFER_SAMPLER:-0}"' in launcher
    assert 'export VLLM_USE_FLASHINFER_SAMPLER="${flashinfer_sampler}"' in launcher
    assert "huggingface.co" not in launcher
    assert "/opt/heartwood-vllm/bin/heartwood-vllm" in launcher
    assert "/opt/heartwood-vllm/bin/python" in verifier
    assert "__heartwood_verify_runtime__" in verifier
    assert 'torch.version.cuda == "12.9"' in verifier
    assert "-name '*.gguf' -o -name '*.safetensors'" in verifier
    assert "-name '*.bin' -size +10M" in verifier
    assert "compressed_tensors/transform/utils/hadamards.safetensors" in verifier
    assert "verify_no_model_artifacts /opt /home" in verifier
    assert "GPU runtime image contains a model artifact" in verifier
    assert "PYTHONPATH" not in executable
    assert runtime_contract["runtime"] == {
        "python_version": "3.12",
        "vllm_version": "0.25.1+cu129",
        "pytorch_version": "2.11.0+cu129",
        "torchaudio_version": "2.11.0+cu129",
        "torchvision_version": "0.26.0+cu129",
        "cuda_version": "12.9",
        "minimum_driver_version": "525.60.13",
        "cuda_13_qualified": False,
    }
    assert "ToolParserManager.list_registered" in runtime_verifier
    assert 'import_module("flashinfer")' in runtime_verifier
    assert "_FORBIDDEN_CUDA_13_PACKAGES" in runtime_verifier
    assert not (_repo_root() / "images/gpu/heartwood_vllm.py").exists()
    assert not (_repo_root() / "images/gpu/sitecustomize.py").exists()
    assert os.access(_repo_root() / "images/gpu/verify_runtime.sh", os.X_OK)
    assert os.access(_repo_root() / "images/gpu/heartwood-vllm", os.X_OK)
    assert os.access(_repo_root() / "images/gpu/install_runtime.sh", os.X_OK)


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
    environment = tmp_path / "environment.txt"
    executable = tmp_path / "vllm"
    executable.write_text(
        (
            "#!/usr/bin/env bash\n"
            f"printf '%s\\n' \"$@\" > {arguments}\n"
            f"printf '%s\\n' \"$VLLM_USE_FLASHINFER_SAMPLER\" > {environment}\n"
        ),
        encoding="utf-8",
    )
    executable.chmod(0o755)
    script = _repo_root() / "images/gpu/start_vllm.sh"
    env = {
        **os.environ,
        "HEARTWOOD_LOCAL_MODEL_PATH": str(model),
        "HEARTWOOD_VLLM_EXECUTABLE": str(executable),
        "HEARTWOOD_MANAGED_MODEL_ALIAS": "test-model",
    }

    completed = subprocess.run(["bash", str(script)], env=env, check=False)

    assert completed.returncode == 0
    values = arguments.read_text(encoding="utf-8").splitlines()
    assert values[:2] == ["serve", str(model)]
    assert values[values.index("--host") + 1] == "127.0.0.1"
    assert values[values.index("--served-model-name") + 1] == "test-model"
    assert values[values.index("--tool-call-parser") + 1] == "hermes"
    assert environment.read_text(encoding="utf-8") == "0\n"

    env["HEARTWOOD_VLLM_ENFORCE_EAGER"] = "1"
    eager = subprocess.run(["bash", str(script)], env=env, check=False)
    assert eager.returncode == 0
    assert arguments.read_text(encoding="utf-8").splitlines()[-1] == "--enforce-eager"

    env["HEARTWOOD_VLLM_ENFORCE_EAGER"] = "invalid"
    invalid = subprocess.run(["bash", str(script)], env=env, check=False)
    assert invalid.returncode == 64

    env.pop("HEARTWOOD_VLLM_ENFORCE_EAGER")
    env["HEARTWOOD_VLLM_USE_FLASHINFER_SAMPLER"] = "1"
    enabled_sampler = subprocess.run(["bash", str(script)], env=env, check=False)
    assert enabled_sampler.returncode == 0
    assert environment.read_text(encoding="utf-8") == "1\n"

    env["HEARTWOOD_VLLM_USE_FLASHINFER_SAMPLER"] = "invalid"
    invalid_sampler = subprocess.run(["bash", str(script)], env=env, check=False)
    assert invalid_sampler.returncode == 64

    env.pop("HEARTWOOD_VLLM_USE_FLASHINFER_SAMPLER")
    env["HEARTWOOD_VLLM_TENSOR_PARALLEL_SIZE"] = "0"
    invalid_tensor_parallel = subprocess.run(["bash", str(script)], env=env, check=False)
    assert invalid_tensor_parallel.returncode == 64

    env["HEARTWOOD_VLLM_TENSOR_PARALLEL_SIZE"] = "2"
    env["HEARTWOOD_VLLM_GPU_MEMORY_UTILIZATION"] = "0"
    invalid_memory_utilization = subprocess.run(["bash", str(script)], env=env, check=False)
    assert invalid_memory_utilization.returncode == 64

    env["HEARTWOOD_VLLM_GPU_MEMORY_UTILIZATION"] = "1.0"
    valid_resources = subprocess.run(["bash", str(script)], env=env, check=False)
    assert valid_resources.returncode == 0
    values = arguments.read_text(encoding="utf-8").splitlines()
    assert values[values.index("--tensor-parallel-size") + 1] == "2"
    assert values[values.index("--gpu-memory-utilization") + 1] == "1.0"

    env["HEARTWOOD_LOCAL_RUNTIME_HOST"] = "0.0.0.0"
    denied = subprocess.run(["bash", str(script)], env=env, check=False)
    assert denied.returncode == 64


def test_gpu_qualification_uses_isolated_heartwood_python() -> None:
    script = _read("images/gpu/coding_agent_e2e.sh")
    coding_agent = _read("images/generic/scripts/coding_agent_e2e.sh")

    assert "command -v setsid" in script
    assert 'setsid bash "${script_dir}/start_vllm.sh"' in script
    assert 'kill -TERM -- "-${runtime_pid}"' in script
    assert 'kill -KILL -- "-${runtime_pid}"' in script
    system_python = re.compile(
        r"(?:^|[;&|]\s*|\bexec\s+)(?:/[^\s;|&]+/)?python(?:3(?:\.\d+)?)?\s",
        re.MULTILINE,
    )

    assert 'heartwood_python="${HEARTWOOD_PYTHON:-${runtime_root}/.venv/bin/python}"' in script
    assert 'configuration="$("${heartwood_python}"' in script
    assert 'HEARTWOOD_VLLM_ROOT="${vllm_root}"' in script
    assert '"${script_dir}/verify_runtime.sh" "${vllm_root}"' in script
    assert system_python.search(script) is None
    assert (
        'heartwood_python="${HEARTWOOD_PYTHON:-${runtime_root}/.venv/bin/python}"' in coding_agent
    )
    assert (
        'heartwood_cli="${HEARTWOOD_CLI:-$(dirname -- "${heartwood_python}")/heartwood}"'
        in coding_agent
    )
    assert "${runtime_root}/.venv/bin/heartwood" not in coding_agent
    assert 'inference="${project}/qualification-inference.json"' in coding_agent
    assert 'mkdir -p "${project}/input"' in coding_agent
    assert system_python.search(coding_agent) is None


def test_coding_agent_qualification_finds_cli_beside_selected_python(tmp_path: Path) -> None:
    runtime_bin = tmp_path / "native-runtime" / "bin"
    runtime_bin.mkdir(parents=True)
    path_bin = tmp_path / "path-bin"
    path_bin.mkdir()
    python = runtime_bin / "python"
    python.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    python.chmod(0o700)

    marker = tmp_path / "heartwood-invocations.txt"
    heartwood = runtime_bin / "heartwood"
    heartwood.write_text(
        '#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$HEARTWOOD_CLI_MARKER"\n',
        encoding="utf-8",
    )
    heartwood.chmod(0o700)
    decoy_marker = tmp_path / "decoy-heartwood-invoked"
    decoy_heartwood = path_bin / "heartwood"
    decoy_heartwood.write_text(
        '#!/usr/bin/env bash\ntouch "$HEARTWOOD_DECOY_MARKER"\nexit 42\n',
        encoding="utf-8",
    )
    decoy_heartwood.chmod(0o700)
    timeout = path_bin / "timeout"
    timeout.write_text('#!/usr/bin/env bash\nshift\nexec "$@"\n', encoding="utf-8")
    timeout.chmod(0o700)

    model = tmp_path / "model"
    model.mkdir()
    project = tmp_path / "qualification"
    env = os.environ.copy()
    env.pop("HEARTWOOD_CLI", None)
    env.update(
        {
            "HEARTWOOD_CAPABLE_PROJECT": str(project),
            "HEARTWOOD_CLI_MARKER": str(marker),
            "HEARTWOOD_DECOY_MARKER": str(decoy_marker),
            "HEARTWOOD_LOCAL_MODEL_PATH": str(model),
            "HEARTWOOD_PYTHON": str(python),
            "HEARTWOOD_RUNTIME_ROOT": str(_repo_root()),
            "PATH": f"{path_bin}:{env['PATH']}",
        }
    )

    result = subprocess.run(
        ["bash", str(_repo_root() / "images/generic/scripts/coding_agent_e2e.sh")],
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert "models refresh heartwood" in marker.read_text(encoding="utf-8")
    assert not decoy_marker.exists()


def test_carina_native_launch_requires_verified_synthetic_allocation() -> None:
    bootstrap = _read("deploy/carina/bootstrap.sh")
    installer = _read("deploy/install.sh")
    runtime_verifier = _read("images/gpu/verify_runtime.sh")
    launch_runtime = _read("packages/cli/src/heartwood/cli/_launch.py")
    environment = _toml("images/generic/image-flavors.toml")
    bootstrap_environment = _read("deploy/carina/environment.yml")

    assert "micromamba create" in bootstrap
    assert "images/gpu/install_runtime.sh" in bootstrap
    assert 'images/gpu/verify_runtime.sh "${root}/vllm"' in bootstrap
    assert "from importlib.metadata import version" not in bootstrap
    assert '--target "${root}/vllm"' in bootstrap
    assert '--installer-state "${installer_state}"' in installer
    assert ': "${installer_state:?--installer-state is required}"' in bootstrap
    assert '--python "${bootstrap_python}"' in bootstrap
    assert '--uv "${root}/bootstrap/bin/uv"' in bootstrap
    assert 'HEARTWOOD_VLLM_PYTHON="${root}/vllm/bin/python"' in bootstrap
    assert 'HEARTWOOD_VLLM_EXECUTABLE="${root}/vllm/bin/heartwood-vllm"' in bootstrap
    assert "micromamba install" in bootstrap
    assert "module load" in bootstrap
    assert "HEARTWOOD_MODULE_INIT" in bootstrap
    assert 'extract_threads="${SLURM_CPUS_PER_TASK:-8}"' in bootstrap
    assert 'MAMBA_EXTRACT_THREADS="${extract_threads}"' in bootstrap
    assert "if ((10#${extract_threads} > 8)); then" in bootstrap
    assert "if ((10#${MAMBA_EXTRACT_THREADS} > 8)); then" in bootstrap
    assert '"${platform}" == "carina"' in installer
    assert '-z "${SLURM_JOB_ID:-}"' in installer
    assert '--partition="${install_partition}"' in installer
    assert "--cpus-per-task=8" in installer
    assert "--mem=32G" in installer
    assert '"$0" "${original_arguments[@]}"' in installer
    assert "/usr/share/lmod/lmod/init/profile" in bootstrap
    assert '"${root}/bootstrap/conda-meta"' in bootstrap
    assert "images/gpu/vllm-requirements.txt" not in bootstrap
    assert '"${root}/vllm/bin/python"' in bootstrap
    assert "import torch, vllm" in runtime_verifier
    assert "VLLM_USE_FLASHINFER_SAMPLER" not in bootstrap
    assert "ffmpeg" not in bootstrap_environment
    assert "  - tmux" in bootstrap_environment
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
    assert '"heartwood"' in launch_runtime
    assert "127.0.0.1:8765/v1/models" in launch_runtime
    gpu_environment = _read("packages/gateway/src/heartwood/gateway/_gpu_environment.py")
    assert '"sinfo", "--noheader", "--format=%P|%G|%a|%m|%c"' in gpu_environment
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
    real_smoke = _read("deploy/tests/native_installer_real_smoke.sh")
    ubuntu_smoke = _read("deploy/tests/native_installer_ubuntu_smoke.sh")
    llama_installer = _read("deploy/install-llama-cpp.sh")

    assert "sha256sum --check --strict" in installer
    assert "--bundle" in installer
    assert "--dry-run" in installer
    assert "HEARTWOOD_INSTALL_ROOT" not in installer
    assert "HEARTWOOD_HOME" not in installer
    assert "HEARTWOOD_MODEL_CACHE" not in installer
    assert "exec %q" in installer
    assert "checksum manifest must contain exactly heartwood-native.tar.gz" in installer
    assert "installer release ${installer_release} does not match bundle" in installer
    assert "__HEARTWOOD_RELEASE_VERSION__" in installer
    assert "__HEARTWOOD_RELEASE_VERSION__" in packager
    assert "--version VERSION" not in installer
    assert "releases/latest/download" not in installer
    assert "[A-Za-z0-9._+-]{0,127}" in installer
    assert "[A-Za-z0-9._+-]{0,127}" in packager
    assert "git archive --format=tar HEAD" in packager
    assert "COPYFILE_DISABLE=1 tar --no-xattrs" in packager
    assert "native package version is unsafe" in packager
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
    assert "actions/setup-node@v7" in release_workflow
    assert 'node-version: "24"' in release_workflow
    assert "native_installer_ubuntu_smoke.sh" in release_workflow
    assert "observed media type:" in release_images
    assert "Linux platforms:" in release_images
    assert "Build And Verify Native Assets" in workflow
    assert "native_installer_smoke.sh" in workflow
    assert "native_installer_ubuntu_smoke.sh" in workflow
    assert "native_installer_real_smoke.sh" in ubuntu_smoke
    assert 'UV_PYTHON_INSTALL_DIR="${runtime_root}/python"' in installer
    assert 'deploy/install-llama-cpp.sh "${runtime_root}/llama.cpp"' in installer
    assert 'export PATH="${runtime}/bin:${runtime}:${PATH}"' in installer
    assert (
        'export LD_LIBRARY_PATH="${runtime}/lib:${runtime}${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"'
        in installer
    )
    assert '"${command_path}" --version' in installer
    assert "libgomp.so.1" in llama_installer
    assert 'verify_archive "${runtime}/${archive_name}"' in llama_installer
    assert "existing llama.cpp runtime has no pinned upstream archive" in llama_installer
    assert "from its pinned archive" in llama_installer
    assert "llama.cpp installer trusted a runtime's self-authenticated manifest" in smoke
    assert 'tar -xzf "${workspace}/heartwood-native.tar.gz"' in installer
    assert 'generation_root="$(mktemp -d "${installations_root}/' in installer
    assert 'replace_symlink "${current_target}" "${root}/current"' in installer
    assert "installer accepted a failed application build" in smoke
    assert "installer accepted a failed published command" in smoke
    assert "installer followed redirected ${owned_name} state" in smoke
    assert "installer ignored an active installation lock" in smoke
    assert 'installer_state="$(mktemp -d "${installer_base}/run.XXXXXX")"' in installer
    assert "another installation is using this root" in installer
    assert 'test ! -e "${dry_run_root}"' in smoke
    assert "github.com/astral-sh/uv/releases/download/${uv_version}" in ubuntu_smoke
    assert "04f8b82f5d47f0512dcd32c67a4a6f16a0ea27c81537c338fd0ad6b23cebe829" in (ubuntu_smoke)
    assert "astral.sh/uv/install.sh" not in ubuntu_smoke
    assert 'cd "${workspace}/heartwood/packages/webui"' in packager
    assert "npm ci --no-audit --fund=false" in packager
    assert "npm run build" in packager
    assert "packages/webui/dist/index.html" in packager
    assert '"${installation}/bin/heartwood-jupyter" --version' in real_smoke
    assert '"${installation}/bin/heartwood" --interface web' in real_smoke
    assert "installer accepted a corrupted checksum" in smoke
    assert "installer accepted an unsafe checksum manifest" in smoke
    assert '"${installation}/bin/heartwood" --version' in real_smoke
    assert '"${runtime}/llama.cpp/llama-server"' in real_smoke
    assert "Readiness: setup-required" in real_smoke
    assert "models download llama-cpp-stories260k-ci" in real_smoke
    assert "local_inference_smoke.sh" in real_smoke
    assert "heartwood-runtime-tamper-test" in real_smoke
    assert "heartwood-source-tamper-test" in real_smoke


def test_gpu_publication_builds_only_explicit_main_variants() -> None:
    workflow = _read(".github/workflows/gpu-container-image.yml")
    runner_cleanup = _read("deploy/reclaim-github-runner-space.sh")
    dependency_review = _read(".github/workflows/dependency-review.yml")
    pull_request_build = workflow.split("  pull-request-build:\n", maxsplit=1)[1].split(
        "\n  build:\n", maxsplit=1
    )[0]
    qualification = workflow.split("  gpu-qualification:\n", maxsplit=1)[1].split(
        "\n  pull-request-build:\n", maxsplit=1
    )[0]
    main_build = workflow.split("  build:\n", maxsplit=1)[1].split("\n  promote:\n", maxsplit=1)[0]

    assert "runtime-gpu-nvidia" in workflow
    assert "terra-runtime-gpu-nvidia" in workflow
    assert "Build GPU candidate ${{ matrix.target }}" in workflow
    assert 'target=gpu-ci-validate"' in pull_request_build
    assert "bash -n images/gpu/install_runtime.sh" in workflow
    assert "test -x images/gpu/install_runtime.sh" in workflow
    assert 'output=type=cacheonly"' in pull_request_build
    assert "output=type=docker" not in pull_request_build
    assert "docker/setup-buildx-action@v4" in pull_request_build
    assert pull_request_build.count("deploy/reclaim-github-runner-space.sh") == 1
    assert main_build.count("deploy/reclaim-github-runner-space.sh") == 1
    assert "/usr/local/lib/android" in runner_cleanup
    assert "/usr/share/dotnet" in runner_cleanup
    assert "attest=type=sbom,disabled=true" in pull_request_build
    assert "attest=type=provenance,disabled=true" in pull_request_build
    assert "Promote GPU Channel Tags" in workflow
    assert "publish_commit_candidate:" in workflow
    assert "Publish validated immutable GPU tags for this commit" in workflow
    assert "qualification_configuration:" in workflow
    assert "used only when GPU qualification is enabled" in workflow
    assert "inputs.qualification_configuration" in qualification
    assert "inputs.qualification_configuration" not in pull_request_build
    assert "inputs.qualification_configuration" not in main_build
    assert "github.event_name == 'workflow_dispatch' && inputs.publish_commit_candidate" in (
        main_build
    )
    assert "if: github.ref == 'refs/heads/main'" in workflow
    assert "push-by-digest=true" in workflow
    assert 'BUILDX_NO_DEFAULT_ATTESTATIONS: "1"' in workflow
    assert "docker buildx prune --all --force" in main_build
    assert (
        main_build.index("Build candidate by digest")
        < main_build.index("docker buildx prune --all --force")
        < main_build.index("Verify candidate contents")
    )
    assert "--prefer-index=false" in workflow
    assert "application/vnd.docker.distribution.manifest.v2+json" in workflow
    assert "observed media type:" in workflow
    assert "Linux platforms:" in workflow
    assert 'docker pull --platform linux/amd64 "${CANDIDATE}"' in main_build
    assert "--entrypoint /opt/heartwood/images/gpu/verify_runtime.sh" in main_build
    assert "Verify shared agent and platform interfaces" in main_build
    assert "terra_jupyter_contract_smoke.sh" in main_build
    assert "terra_image_smoke.sh" in main_build
    assert "offline_stack_smoke.sh" in main_build
    assert "immutable GPU commit tag does not match" in workflow
    assert "sha-${GIT_SHA}-${COMMIT_SUFFIX}" in main_build
    assert "edge-gpu-nvidia" not in main_build
    assert "edge-terra-gpu-nvidia" not in main_build
    assert "refusing to move GPU channel tags from a stale main workflow" in workflow
    assert "promoted ${channel} digest does not match" in workflow
    assert "allow-ghsas: GHSA-w8v5-vhqr-4h9v, GHSA-rrmf-rvhw-rf47" in dependency_review
    assert "GHSA-8fr4-5q9j-m8gm" not in dependency_review


def test_gpu_qualification_workflow_offers_every_candidate_configuration() -> None:
    workflow = _read(".github/workflows/gpu-container-image.yml")
    matrix = _toml("images/gpu/compatibility.toml")
    configuration_input = workflow.split("      qualification_configuration:\n", maxsplit=1)[
        1
    ].split("      qualification_runner:\n", maxsplit=1)[0]
    options = {
        line.removeprefix("- ")
        for line in (item.strip() for item in configuration_input.splitlines())
        if line.startswith("- ")
    }
    configuration_ids = {
        configuration["configuration_id"] for configuration in matrix["configurations"]
    }

    assert options == configuration_ids


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
        r'(?im)(?:^name\s*=\s*["\']|^|["\'\s])(diskcache|torch|vllm)(?:["\'\s<>=!~@\[])'
    )
    unexpected = [
        path.relative_to(root).as_posix()
        for path in sorted(dependency_files)
        if path not in gpu_dependencies and declaration.search(path.read_text(encoding="utf-8"))
    ]

    assert unexpected == []
    text = lock.read_text(encoding="utf-8")
    assert "diskcache==5.6.3" in text
    assert "vllm-0.25.1%2Bcu129" in text
    assert "torch-2.11.0%2Bcu129-cp312-cp312-manylinux_2_28_x86_64.whl" in text
    assert "vllm-0.25.1%2Bcu129" in input_file.read_text(encoding="utf-8")
    assert "torch-2.11.0%2Bcu129-cp312-cp312-manylinux_2_28_x86_64.whl" in (
        input_file.read_text(encoding="utf-8")
    )


def test_isolated_smoke_uses_real_openhands_sdk_without_weights() -> None:
    compose = _read("images/generic/compose.yaml")
    workflow = _read(".github/workflows/container-smoke.yml")
    smoke = _read("images/generic/scripts/offline_stack_smoke.sh")
    capable = _read("images/generic/scripts/capable_model_e2e.sh")
    coding_agent = _read("images/generic/scripts/coding_agent_e2e.sh")
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
    assert "coding-agent model must be outside the disposable test project" in coding_agent
    assert 'workspace = Path.cwd() / ".heartwood" / "sessions"' in smoke
    assert 'cohort_path="${project}/cohort-summary.json"' in coding_agent
    assert "Checking direct model inference" in coding_agent
    assert "verify_coding_agent_e2e.py" in coding_agent
    assert "/tmp/heartwood-model-cache/.heartwood/models:/models:ro" in workflow
    assert "models refresh heartwood" in smoke
    assert "models connect heartwood heartwood-managed-runtime" in smoke
    assert "models add inactive-smoke" in smoke
    assert "HEARTWOOD_UNUSED_MODEL_API_KEY" in smoke
    assert "HEARTWOOD_UNUSED_MODEL_API_KEY" in model_stub
    assert 'self.path != "/v1/models"' in model_stub
    assert "models validate heartwood" in smoke
    assert ' --prompt "' in smoke
    assert " chat " not in smoke
    assert " detect " not in smoke
    assert "call-heartwood-reference-analysis" not in smoke
    assert "call-heartwood-offline-smoke" not in smoke
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


def test_local_model_stub_preserves_explicit_action_risk() -> None:
    path = _repo_root() / "images/generic/scripts/local_model_stub.py"
    spec = importlib.util.spec_from_file_location("heartwood_local_model_stub", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    terminal_call = cast(
        Callable[..., dict[str, object]],
        module._terminal_call,
    )

    call = terminal_call(
        "call-medium-risk",
        "curl https://example.invalid",
        "run a medium-risk network command",
        security_risk="MEDIUM",
    )
    function = cast(dict[str, object], call["function"])
    arguments = json.loads(cast(str, function["arguments"]))

    assert arguments["security_risk"] == "MEDIUM"

    prompt_call = module._prompt_terminal_call(
        "printf heartwood-openhands-action",
        "run a bounded offline smoke command",
    )
    assert prompt_call.startswith("<function=terminal>\n")
    assert "<parameter=command>printf heartwood-openhands-action</parameter>" in prompt_call
    assert "<parameter=security_risk>LOW</parameter>" in prompt_call
    assert prompt_call.endswith("</function>")


def test_launch_scripts_are_valid_and_require_explicit_local_artifact() -> None:
    scripts = (
        "images/generic/scripts/capable_model_e2e.sh",
        "images/generic/scripts/coding_agent_e2e.sh",
        "images/generic/scripts/offline_stack_smoke.sh",
        "images/generic/scripts/container_persistence_smoke.sh",
        "images/generic/scripts/local_inference_smoke.sh",
        "images/generic/scripts/start_local_runtime.sh",
        "images/gpu/coding_agent_e2e.sh",
        "images/platform/scripts/terra_image_smoke.sh",
        "images/platform/scripts/terra_jupyter_contract_smoke.sh",
        "images/platform/scripts/terra_jupyter_launch_smoke.sh",
        "images/platform/scripts/terra_ci_model_safety_smoke.sh",
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
    assert (
        '[\n            "heartwood",\n            "gateway",\n            "serve",' in jupyter_smoke
    )
    assert 'RUNTIME_ROOT / "packages" / "webui" / "dist"' in jupyter_smoke
    assert '"confirmation.requested"' in jupyter_smoke
    assert '"tool.execution.recorded"' in jupyter_smoke
    assert "os.chdir(PROJECT_ROOT)" in jupyter_smoke
    assert "NotebookSession(session_id=" in jupyter_smoke
    assert '"project/readiness"' in jupyter_smoke

    terra_launch = _read("images/platform/scripts/terra_jupyter_launch_smoke.sh")
    assert "heartwood gateway serve --host 0.0.0.0" in terra_launch
    assert "project/readiness" in terra_launch
    assert "proxy/${gateway_port}/" in terra_launch

    terra_model_safety = _read("images/platform/scripts/terra_ci_model_safety_smoke.sh")
    assert "heartwood doctor --json" in terra_model_safety
    assert 'payload.get("platform_id") != "terra"' in terra_model_safety
    assert 'payload.get("state") != "setup-required"' in terra_model_safety
    assert 'payload.get("project_root")' in terra_model_safety
    assert "No active model selected" in terra_model_safety
    assert "CI-only model must not become an agent profile" in terra_model_safety
    assert "terra-project-storage" in terra_model_safety
    assert 'checks.get("terra-gpu"' in terra_model_safety

    terra_persistence = _read("images/platform/scripts/terra_project_persistence_smoke.sh")
    assert '--volume "${state_volume}:/home/jupyter"' in terra_persistence
    assert terra_persistence.count("terra_image_smoke.sh") == 2
    assert "terra-project-persistence replay" in terra_persistence
    assert "heartwood doctor --json" in terra_persistence
    assert " terra-project-persistence detect" not in terra_persistence
    assert "test ! -e /home/jupyter/.heartwood" in terra_persistence


def test_publish_workflow_uses_digest_merge_and_clean_public_tags() -> None:
    publish = _read(".github/workflows/container-image.yml")
    smoke = _read(".github/workflows/container-smoke.yml")
    compose = _read("images/generic/compose.yaml")
    capable_model = _read("images/generic/scripts/capable_model_e2e.sh")
    coding_agent = _read("images/generic/scripts/coding_agent_e2e.sh")
    qualification = _read("images/generic/scripts/verify_coding_agent_e2e.py")

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
    assert "Run staged host-user bind-mount smoke" in publish
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
    assert publish.index("Verify staged Terra CI-only model remains non-agent") < publish.index(
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
    assert "Verify Terra CI-only model remains non-agent" in smoke
    assert "terra_ci_model_safety_smoke.sh" in smoke
    assert "terra_project_persistence_smoke.sh" in smoke
    assert "images/generic/scripts/offline_stack_smoke.sh" in smoke
    assert "HEARTWOOD_SMOKE_PROJECT=/home/jupyter/synthetic-agent-analysis" in smoke
    assert "HEARTWOOD_TERRA_DEMO_PROJECT_ROOT=/home/jupyter/synthetic-notebook-analysis" in smoke
    assert "container_persistence_smoke.sh" in smoke
    assert "Verify host-user bind-mount persistence" in smoke
    assert "bind_mount_user_smoke.sh" in smoke
    assert publish.count("--env CLUSTER_NAME=terra-ci-model-safety-smoke") == 2
    assert smoke.count("--env CLUSTER_NAME=terra-ci-model-safety-smoke") == 2
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
    assert "if: inputs.run_capable_model" in smoke
    assert "run_capable_model: ${{ github.event_name != 'pull_request' }}" in _read(
        ".github/workflows/main-validation.yml"
    )
    assert "qwen25-7b-instruct-q4_k_m" in smoke
    assert "capable_model_e2e.sh" in smoke
    assert "--network none --read-only" in smoke
    assert smoke.count("uid=10001,gid=10001,mode=0700") == 2
    assert compose.count("uid=10001,gid=10001,mode=0700") == 2
    assert "not 1 <= len(terminal_executions) <= 3" in qualification
    assert "not 1 <= len(tool_executions) <= 3" in qualification
    assert "&& cat cohort-summary.json" in coding_agent
    assert 'f"http://127.0.0.1:{port}/health"' in capable_model
    assert "llama.cpp runtime log (last 200 lines)" in capable_model
    assert "artifact_path.read_text" in qualification
    assert "--jinja" in _read("images/generic/scripts/start_local_runtime.sh")
    assert '"source_participant_count": 24' in qualification
    assert '"participant_count": 20' in qualification
    assert 'cohort["quality_checks"].get("aggregate_only_output") is not True' in qualification


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
required_env = [
    "HEARTWOOD_PLATFORM=terra",
    "HEARTWOOD_PLATFORM_HOME=/home/jupyter",
    "HEARTWOOD_PYTHON=/opt/heartwood/.venv/bin/python",
]
forbidden_path_entries = ["/opt/heartwood/.venv/bin"]
runtime_tag = "edge-terra"
commit_runtime_tag = "sha-<git-sha>-terra"

[platforms.terra.required_env_contains]
PATH = ["/opt/llama.cpp", "/opt/conda/bin", "/usr/local/bin"]
LD_LIBRARY_PATH = ["/usr/local/cuda/lib64", "/usr/local/nvidia/lib64"]
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
                            ("LD_LIBRARY_PATH=/usr/local/cuda/lib64:/usr/local/nvidia/lib64"),
                            "JUPYTER_PATH=/opt/conda/share/jupyter",
                            "HEARTWOOD_PLATFORM=terra",
                            "HEARTWOOD_PLATFORM_HOME=/home/jupyter",
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
