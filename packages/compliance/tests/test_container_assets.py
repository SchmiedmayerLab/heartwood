# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Static tests for the generic image and Compose smoke-test contract."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import ClassVar, cast

from packaging.requirements import Requirement


def test_generic_image_contains_runtime_surface_packages() -> None:
    dockerfile = (_repo_root() / "images" / "generic" / "Dockerfile").read_text(encoding="utf-8")
    gateway_pyproject = (_repo_root() / "packages" / "gateway" / "pyproject.toml").read_text(
        encoding="utf-8"
    )

    assert dockerfile.startswith("# syntax=docker/dockerfile:")
    assert "FROM node:24-trixie-slim AS webui-build" in dockerfile
    assert "npm ci --no-audit --fund=false" in dockerfile
    assert "npm run build" in dockerfile
    assert "org.opencontainers.image.source" in dockerfile
    assert 'org.heartwood.image.flavor="${HEARTWOOD_IMAGE_FLAVOR}"' in dockerfile
    assert "uv sync --locked --no-dev --all-extras" in dockerfile
    assert (
        "--mount=type=cache,target=/home/heartwood/.cache/uv,"
        "uid=${HEARTWOOD_UID},gid=${HEARTWOOD_GID}"
    ) in dockerfile
    assert "llama-cpp-python" not in gateway_pyproject
    assert "libtmux==0.61.0" in gateway_pyproject
    assert "openhands-agent-server==1.34.0" in gateway_pyproject
    assert "openhands-tools==1.34.0" in gateway_pyproject
    assert "ARG LLAMA_CPP_VERSION=b9937" in dockerfile
    assert (
        "LLAMA_CPP_UBUNTU_X64_SHA256=937e10a3fb6c4b1791f943230525e91bea168d1305c1d21079970acb70205df3"
        in dockerfile
    )
    assert (
        "LLAMA_CPP_UBUNTU_ARM64_SHA256=c8212588514e33150dcff64fbadbf151d978fd9c4de05d0f66b2267b24310ac4"
        in dockerfile
    )
    assert "llama-${LLAMA_CPP_VERSION}-bin-ubuntu-x64.tar.gz" in dockerfile
    assert "llama-${LLAMA_CPP_VERSION}-bin-ubuntu-arm64.tar.gz" in dockerfile
    assert "sha256sum --check" in dockerfile
    assert "tar -xzf /tmp/llama.cpp.tar.gz -C /opt/llama.cpp --strip-components=1" in dockerfile
    assert "LD_LIBRARY_PATH=/opt/llama.cpp" in dockerfile
    assert "libgomp1" in dockerfile
    assert "download_model_artifact.py" in dockerfile
    downloader = (
        _repo_root() / "images" / "generic" / "scripts" / "download_model_artifact.py"
    ).read_text(encoding="utf-8")
    assert "--timeout-seconds" in downloader
    assert "--retries" in downloader
    assert "retrying in" in downloader
    assert "HEARTWOOD_LOCAL_RUNTIME_PROFILE=llama-cpp-cpu" in dockerfile
    assert "HEARTWOOD_AGENT_BACKEND=openhands-bash" in dockerfile
    assert (
        "HEARTWOOD_PROVIDER_CONFIG=/opt/heartwood/images/generic/providers/provider-routes.example.toml"
        in dockerfile
    )
    assert "HEARTWOOD_WEB_ROOT=/opt/heartwood/packages/webui/dist" in dockerfile
    assert (
        "COPY --from=webui-build --chown=heartwood:heartwood "
        "/src/packages/webui/dist ./packages/webui/dist" in dockerfile
    )
    assert "HEARTWOOD_AGENT_SERVER_API_KEY" not in dockerfile
    assert "ARG HEARTWOOD_BUNDLE_LOCAL_MODEL=0" in dockerfile
    assert "ARG HEARTWOOD_IMAGE_FLAVOR=runtime" in dockerfile
    assert "build-essential" not in dockerfile
    assert " cmake" not in dockerfile
    assert (
        "--retry 5 --retry-delay 2 --retry-connrefused --connect-timeout 15 --max-time 600"
        in dockerfile
    )
    assert "ARG HEARTWOOD_UID=10001" in dockerfile
    assert "ARG HEARTWOOD_GID=10001" in dockerfile
    assert "groupadd --system --gid" in dockerfile
    assert "useradd --system --uid" in dockerfile
    assert "COPY --chown=heartwood:heartwood packages ./packages" in dockerfile
    assert "COPY --chown=heartwood:heartwood fixtures ./fixtures" in dockerfile
    assert "COPY --chown=heartwood:heartwood skills ./skills" in dockerfile
    assert "COPY --chown=heartwood:heartwood images ./images" in dockerfile
    assert "COPY --chown=heartwood:heartwood README.md ACRONYMS.md ./" in dockerfile
    assert "COPY --chown=heartwood:heartwood docs ./docs" in dockerfile
    assert "COPY --chown=heartwood:heartwood design ./design" in dockerfile
    assert "USER heartwood" in dockerfile
    assert 'PATH="/opt/llama.cpp:/opt/heartwood/.venv/bin:${PATH}"' in dockerfile
    assert 'CMD ["heartwood", "--help"]' in dockerfile


def test_openhands_runtime_pins_stay_consistent_across_assets() -> None:
    gateway_manifest = tomllib.loads(
        (_repo_root() / "packages" / "gateway" / "pyproject.toml").read_text(encoding="utf-8")
    )
    runtime_manifest = tomllib.loads(
        (_repo_root() / "images" / "generic" / "local-runtime" / "profiles.toml").read_text(
            encoding="utf-8"
        )
    )
    getting_started = (_repo_root() / "docs" / "getting-started-offline.md").read_text(
        encoding="utf-8"
    )
    implementation_plan = (_repo_root() / "design" / "09-implementation-plan.md").read_text(
        encoding="utf-8"
    )

    agent_extra = gateway_manifest["project"]["optional-dependencies"]["agent-server"]
    pins = dict(_exact_package_pin(requirement) for requirement in agent_extra)
    expected_packages = ("openhands-agent-server", "openhands-tools", "libtmux")
    profile_dependency = runtime_manifest["agent_server"]["openhands"]["runtime_dependency"]

    for package_name in expected_packages:
        pinned_dependency = f"{package_name}=={pins[package_name]}"
        assert pinned_dependency in profile_dependency
        assert pinned_dependency in getting_started

    assert f"openhands-agent-server=={pins['openhands-agent-server']}" in implementation_plan
    assert f"openhands-tools=={pins['openhands-tools']}" in implementation_plan


def test_platform_image_defines_terra_runtime_contract() -> None:
    dockerfile = (_repo_root() / "images" / "platform" / "Dockerfile").read_text(encoding="utf-8")
    ci_base = (_repo_root() / "images" / "platform" / "terra-ci-base.Dockerfile").read_text(
        encoding="utf-8"
    )
    verifier = (
        _repo_root() / "images" / "platform" / "scripts" / "verify_registry_manifest.py"
    ).read_text(encoding="utf-8")
    manifest = tomllib.loads((_repo_root() / "images" / "platforms.toml").read_text("utf-8"))

    assert dockerfile.startswith("# syntax=docker/dockerfile:")
    assert "FROM --platform=$BUILDPLATFORM node:24-trixie-slim AS webui-build" in dockerfile
    assert (
        "FROM --platform=${HEARTWOOD_PLATFORM_BASE_PLATFORM} "
        "ghcr.io/astral-sh/uv:python3.12-trixie-slim AS uv-bin"
    ) in dockerfile
    assert (
        "ARG HEARTWOOD_PLATFORM_BASE_IMAGE=us.gcr.io/broad-dsp-gcr-public/"
        "terra-jupyter-python:1.1.6"
    ) in dockerfile
    assert "ARG HEARTWOOD_PLATFORM_BASE_PLATFORM=linux/amd64" in dockerfile
    assert (
        "FROM --platform=${HEARTWOOD_PLATFORM_BASE_PLATFORM} ${HEARTWOOD_PLATFORM_BASE_IMAGE}"
        in dockerfile
    )
    assert "org.heartwood.platform.base" in dockerfile
    assert "org.heartwood.platform.base.platform" in dockerfile
    assert "UV_PROJECT_ENVIRONMENT=/opt/heartwood/.venv" in dockerfile
    assert "UV_PYTHON_INSTALL_DIR=/opt/heartwood/python" in dockerfile
    assert (
        "HEARTWOOD_WORKSPACE=${HEARTWOOD_PLATFORM_HOME}/heartwood-workspace/sessions" in dockerfile
    )
    assert "HEARTWOOD_PYTHON=/opt/heartwood/.venv/bin/python" in dockerfile
    assert 'JUPYTER_PATH="${HEARTWOOD_JUPYTER_PREFIX}/share/jupyter"' in dockerfile
    assert 'PATH="/opt/llama.cpp:${PATH}"' in dockerfile
    assert "/opt/heartwood/.venv/bin:${PATH}" not in dockerfile
    assert 'LD_LIBRARY_PATH="/opt/llama.cpp:${LD_LIBRARY_PATH}"' in dockerfile
    assert "COPY --from=uv-bin /usr/local/bin/uv /usr/local/bin/uvx /usr/local/bin/" in dockerfile
    assert (
        "--retry 5 --retry-delay 2 --retry-connrefused --connect-timeout 15 --max-time 600"
        in dockerfile
    )
    assert "uv sync --locked --no-dev --all-extras --python 3.12" in dockerfile
    assert "ln -sf /opt/heartwood/.venv/bin/heartwood /usr/local/bin/heartwood" in dockerfile
    assert "ln -sf /opt/heartwood/.venv/bin/python /usr/local/bin/heartwood-python" in dockerfile
    assert "ln -sf /opt/heartwood/.venv/bin/agent-server /usr/local/bin/agent-server" in dockerfile
    assert "ipykernel install" in dockerfile
    assert "--name heartwood \\" in dockerfile
    assert "ARG HEARTWOOD_RUNTIME_ARCH=amd64" in dockerfile
    assert 'case "${HEARTWOOD_RUNTIME_ARCH}" in' in dockerfile
    assert "HEARTWOOD_AGENT_SERVER_API_KEY" not in dockerfile
    assert "ARG HEARTWOOD_BUNDLE_LOCAL_MODEL=0" in dockerfile
    assert "ARG HEARTWOOD_PLATFORM_USER=jupyter" in dockerfile
    assert "WORKDIR ${HEARTWOOD_PLATFORM_HOME}" in dockerfile
    assert "USER ${HEARTWOOD_PLATFORM_USER}" in dockerfile
    assert "HOME=/tmp IPYTHONDIR=/tmp/heartwood-ipython" in dockerfile
    assert (
        'chown -R "${HEARTWOOD_PLATFORM_USER}" /opt/heartwood '
        '"${HEARTWOOD_PLATFORM_HOME}/heartwood-workspace"' in dockerfile
    )
    assert "COPY docs ./docs" in dockerfile
    assert "COPY design ./design" in dockerfile
    assert "COPY --from=webui-build /src/packages/webui/dist ./packages/webui/dist" in dockerfile
    assert 'CMD ["heartwood", "--help"]' not in dockerfile
    assert "FROM python:3.12-slim" in ci_base
    assert "USER=jupyter" in ci_base
    assert "HOME=/home/jupyter" in ci_base
    assert "JUPYTER_PORT=8000" in ci_base
    assert "JUPYTER_HOME=/etc/jupyter" in ci_base
    assert 'PATH="/opt/conda/bin:${PATH}"' in ci_base
    assert "notebook==6.5.4" in ci_base
    assert "jupyter_server==1.24.0" in ci_base
    assert "jupyter_server_proxy==4.0.0" in ci_base
    assert "setuptools==80.9.0" in ci_base
    assert "Pull-request-only surrogate for Terra's notebook base" in ci_base
    assert "/opt/conda/bin/jupyter" in ci_base
    assert "/opt/conda/bin/jupyter-notebook" in ci_base
    assert "c.NotebookApp.port = 8000" in ci_base
    assert 'c.NotebookApp.base_url = "/notebooks" + fragment' in ci_base
    assert "GOOGLE_PROJECT" in ci_base
    assert "CLUSTER_NAME" in ci_base
    assert "${JUPYTER_HOME}/scripts/run-jupyter.sh" in ci_base
    assert "/opt/conda/bin/python3 /opt/conda/bin/jupyter-notebook" in ci_base
    assert 'ENTRYPOINT ["/opt/conda/bin/jupyter", "notebook"]' in ci_base
    terra_image_smoke = (
        _repo_root() / "images" / "platform" / "scripts" / "terra_image_smoke.sh"
    ).read_text(encoding="utf-8")
    jupyter_contract_smoke = (
        _repo_root() / "images" / "platform" / "scripts" / "terra_jupyter_contract_smoke.sh"
    ).read_text(encoding="utf-8")
    jupyter_launch_smoke = (
        _repo_root() / "images" / "platform" / "scripts" / "terra_jupyter_launch_smoke.sh"
    ).read_text(encoding="utf-8")
    assert "HEARTWOOD_PYTHON" in terra_image_smoke
    assert "Heartwood venv must not shadow the platform Python" in jupyter_contract_smoke
    assert ".ipython/heartwood-jupyter-contract-write" in jupyter_contract_smoke
    assert "kernelspec list --json" in jupyter_contract_smoke
    assert "Heartwood Jupyter kernel is not registered" in jupyter_contract_smoke
    assert "terra-jupyter-contract" in jupyter_contract_smoke
    assert "HEARTWOOD_TERRA_JUPYTER_MODE" in jupyter_launch_smoke
    assert "GOOGLE_PROJECT" in jupyter_launch_smoke
    assert "CLUSTER_NAME" in jupyter_launch_smoke
    assert "--entrypoint /etc/jupyter/scripts/run-jupyter.sh" in jupyter_launch_smoke
    assert "WWW-Authenticate" in verifier
    assert "parse_bearer_challenge" in verifier
    assert 'registry != "localhost"' in verifier
    assert "IMAGE_MANIFEST_MEDIA_TYPES" in verifier
    assert "INDEX_MEDIA_TYPES" in verifier
    assert "allow_non_platform_manifests" in verifier
    assert "manifest_media_type" in verifier
    assert "supported_platforms" in verifier
    assert "verify_image_config_contract" in verifier
    assert "forbidden_path_entries" in verifier

    terra = manifest["platforms"]["terra"]
    assert terra["status"] == "implemented"
    assert terra["base_image"] == "us.gcr.io/broad-dsp-gcr-public/terra-jupyter-python:1.1.6"
    assert terra["base_image_parent"] == ("us.gcr.io/broad-dsp-gcr-public/terra-jupyter-base:1.1.4")
    assert terra["base_platform"] == "linux/amd64"
    assert terra["platform_home"] == "/home/jupyter"
    assert terra["platform_user"] == "jupyter"
    assert terra["jupyter_prefix"] == "/opt/conda"
    assert terra["image_user"] == "jupyter"
    assert terra["working_dir"] == "/home/jupyter"
    assert terra["entrypoint"] == ["/opt/conda/bin/jupyter", "notebook"]
    assert terra["exposed_ports"] == ["8000/tcp"]
    assert terra["required_env"] == ["HEARTWOOD_PYTHON=/opt/heartwood/.venv/bin/python"]
    assert terra["forbidden_path_entries"] == ["/opt/heartwood/.venv/bin"]
    assert terra["required_env_contains"]["PATH"] == [
        "/opt/llama.cpp",
        "/opt/conda/bin",
        "/usr/local/bin",
    ]
    assert terra["required_env_contains"]["LD_LIBRARY_PATH"] == ["/opt/llama.cpp"]
    assert terra["required_env_contains"]["JUPYTER_PATH"] == ["/opt/conda/share/jupyter"]
    assert terra["runtime_target"] == "terra-runtime"
    assert terra["smoke_target"] == "terra-smoke"
    assert terra["coder_target"] == "terra-runtime"
    assert terra["ci_smoke_target"] == "terra-smoke-ci"
    assert terra["runtime_tag"] == "edge-terra"
    assert terra["smoke_tag"] == "edge-terra-smoke"
    assert terra["coder_tag"] == "edge-terra-coder-7b"
    assert terra["ci_smoke_tag"] == "edge-terra-smoke-ci"
    assert terra["commit_coder_tag"] == "sha-<git-sha>-terra-coder-7b"
    assert terra["bundles_model_artifact_in_runtime"] is True
    assert terra["bundles_model_artifact_in_coder"] is True
    assert terra["runtime_model_manifest"].endswith("qwen25-coder-7b-q4_k_m.toml")
    assert terra["coder_model_manifest"].endswith("qwen25-coder-7b-q4_k_m.toml")
    assert terra["manifest_media_type"] == "application/vnd.docker.distribution.manifest.v2+json"
    assert terra["config_media_type"] == "application/vnd.docker.container.image.v1+json"
    assert terra["publish_attestations"] is False
    assert terra["allow_non_platform_manifests"] is False
    assert "Terra Leonardo image auto-detection" in terra["registry_policy"]
    assert terra["supported_platforms"] == ["linux/amd64"]
    assert terra["unsupported_platforms"] == ["linux/arm64"]
    assert terra["ci_required"] is True
    assert terra["live_workspace_validation_required"] is True


def test_generic_and_terra_runtime_targets_keep_heartwood_payloads_aligned() -> None:
    generic = (_repo_root() / "images" / "generic" / "Dockerfile").read_text(encoding="utf-8")
    platform = (_repo_root() / "images" / "platform" / "Dockerfile").read_text(encoding="utf-8")
    bake = (_repo_root() / "docker-bake.hcl").read_text(encoding="utf-8")
    runtime_target = _bake_target_block(bake, "runtime")
    terra_runtime_target = _bake_target_block(bake, "terra-runtime")
    shared_model_manifest = "images/generic/local-runtime/models/qwen25-coder-7b-q4_k_m.toml"

    assert f'HEARTWOOD_LOCAL_MODEL_MANIFEST = "{shared_model_manifest}"' in runtime_target
    assert f'HEARTWOOD_LOCAL_MODEL_MANIFEST = "{shared_model_manifest}"' in terra_runtime_target

    for source_path in (
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
        assert source_path in generic
        assert source_path in platform

    for runtime_fragment in (
        "npm ci --no-audit --fund=false",
        "npm run build",
        "uv sync --locked --no-dev --all-extras",
        "HEARTWOOD_LOCAL_RUNTIME_PROFILE=llama-cpp-cpu",
        "HEARTWOOD_LOCAL_MODEL_PATH=/opt/heartwood/local-runtime/models/model.gguf",
        "HEARTWOOD_AGENT_BACKEND=openhands-bash",
        "HEARTWOOD_PROVIDER_CONFIG=/opt/heartwood/images/generic/providers/provider-routes.example.toml",
        "HEARTWOOD_WEB_ROOT=/opt/heartwood/packages/webui/dist",
        "ARG LLAMA_CPP_VERSION=b9937",
        "LLAMA_CPP_UBUNTU_X64_SHA256=937e10a3fb6c4b1791f943230525e91bea168d1305c1d21079970acb70205df3",
        "LLAMA_CPP_UBUNTU_ARM64_SHA256=c8212588514e33150dcff64fbadbf151d978fd9c4de05d0f66b2267b24310ac4",
        "download_model_artifact.py",
    ):
        assert runtime_fragment in generic
        assert runtime_fragment in platform


def test_platform_registry_manifest_verifier_uses_platform_contract(tmp_path: Path) -> None:
    _RegistryHandler.accepted_tags = {
        "edge-terra",
        "edge-terra-smoke",
        "edge-terra-coder-7b",
        "sha-abc123-terra",
        "sha-abc123-terra-smoke",
        "sha-abc123-terra-coder-7b",
    }
    _RegistryHandler.requested_accept_headers = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RegistryHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        image_name = f"127.0.0.1:{server.server_port}/test/heartwood"
        manifest = tmp_path / "platforms.toml"
        manifest.write_text(
            f"""
schema_version = "heartwood.platform-images.v1"
image_name = "{image_name}"
moving_channel = "edge"

[platforms.terra]
manifest_media_type = "application/vnd.docker.distribution.manifest.v2+json"
config_media_type = "application/vnd.docker.container.image.v1+json"
publish_attestations = false
allow_non_platform_manifests = false
supported_platforms = ["linux/amd64"]
image_user = "jupyter"
working_dir = "/home/jupyter"
entrypoint = ["/opt/conda/bin/jupyter", "notebook"]
exposed_ports = ["8000/tcp"]
required_env = ["HEARTWOOD_PYTHON=/opt/heartwood/.venv/bin/python"]
forbidden_path_entries = ["/opt/heartwood/.venv/bin"]
runtime_tag = "edge-terra"
smoke_tag = "edge-terra-smoke"
coder_tag = "edge-terra-coder-7b"
commit_runtime_tag = "sha-<git-sha>-terra"
commit_smoke_tag = "sha-<git-sha>-terra-smoke"
commit_coder_tag = "sha-<git-sha>-terra-coder-7b"

[platforms.terra.required_env_contains]
PATH = ["/opt/llama.cpp", "/opt/conda/bin", "/usr/local/bin"]
LD_LIBRARY_PATH = ["/opt/llama.cpp"]
JUPYTER_PATH = ["/opt/conda/share/jupyter"]
""",
            encoding="utf-8",
        )
        result = subprocess.run(
            [
                sys.executable,
                str(
                    _repo_root() / "images" / "platform" / "scripts" / "verify_registry_manifest.py"
                ),
                "--manifest",
                str(manifest),
                "--platform",
                "terra",
                "--image-name",
                image_name,
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

    assert result.returncode == 0, result.stdout + result.stderr
    assert "runtime moving tag" in result.stdout
    assert "smoke commit tag" in result.stdout
    assert "coder commit tag" in result.stdout
    assert _RegistryHandler.requested_accept_headers == [
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]


def test_web_ui_package_has_ci_and_container_launcher() -> None:
    package = json.loads(
        (_repo_root() / "packages" / "webui" / "package.json").read_text(encoding="utf-8")
    )
    workflow = (_repo_root() / ".github" / "workflows" / "web-ui.yml").read_text(encoding="utf-8")
    launcher = (_repo_root() / "images" / "generic" / "scripts" / "start_web_ui.sh").read_text(
        encoding="utf-8"
    )
    demo_launcher = (
        _repo_root() / "images" / "generic" / "scripts" / "start_demo_stack.sh"
    ).read_text(encoding="utf-8")
    terra_smoke = (
        _repo_root() / "images" / "generic" / "scripts" / "terra_jupyter_demo_smoke.py"
    ).read_text(encoding="utf-8")

    assert package["name"] == "@heartwood/webui"
    assert package["scripts"]["build"] == "tsc --noEmit && vite build"
    assert package["scripts"]["test:e2e"] == "playwright test"
    assert package["scripts"]["test:gateway"] == "node scripts/smoke-gateway.cjs"
    assert package["scripts"]["test:jupyter-proxy"] == "node scripts/smoke-jupyter-proxy.cjs"
    assert "@stanfordspezi/spezi-web-design-system" in package["dependencies"]
    assert "@stanfordspezi/spezi-web-configurations" in package["devDependencies"]
    assert "name: Web UI" in workflow
    assert "actions/setup-node@v6" in workflow
    assert "astral-sh/setup-uv@v8.3.2" in workflow
    assert 'node-version: "24"' in workflow
    assert "npm run license:check" in workflow
    assert "npm audit --audit-level=moderate" in workflow
    assert "npx playwright install --with-deps chromium" in workflow
    assert "uv sync --locked" in workflow
    assert "npm run test:e2e" in workflow
    assert "npm run test:gateway --prefix packages/webui" in workflow
    assert "npm run test:jupyter-proxy --prefix packages/webui" in workflow
    assert "heartwood \\" in launcher
    assert "HEARTWOOD_AGENT_BACKEND:-deterministic-local" in launcher
    assert 'HEARTWOOD_AGENT_SERVER_ENABLED="${HEARTWOOD_AGENT_SERVER_ENABLED:-1}"' in launcher
    assert "bash images/generic/scripts/start_agent_server.sh" in launcher
    assert "HEARTWOOD_AGENT_SERVER_WORKSPACE" in launcher
    assert '--web-root "${web_root}"' in launcher
    assert '--base-path "${base_path}"' in launcher
    assert "HEARTWOOD_DEMO_RESPONSE_PREVIEW" in demo_launcher
    assert "HEARTWOOD_DEMO_SEED_APPROVALS" in demo_launcher
    assert "HEARTWOOD_LOCAL_MODEL_MAX_TOKENS" in demo_launcher
    assert "HEARTWOOD_LOCAL_MODEL_MAX_TOKENS:-768" in demo_launcher
    assert "HEARTWOOD_DEMO_WEB_HOST:-0.0.0.0" in demo_launcher
    assert "start_local_runtime.sh" in demo_launcher
    assert "start_web_ui.sh" in demo_launcher
    assert "--target-type model-call" in demo_launcher
    assert "ThreadingHTTPServer" in terra_smoke
    assert "NotebookSession" in terra_smoke
    assert "Terra-style Jupyter demo smoke: ok" in terra_smoke


def test_compose_smoke_runtime_disables_network() -> None:
    compose = (_repo_root() / "images" / "generic" / "compose.yaml").read_text(encoding="utf-8")

    assert "network_mode: none" in compose
    assert "pull: true" in compose
    assert 'HEARTWOOD_BUNDLE_LOCAL_MODEL: "1"' in compose
    assert "HEARTWOOD_IMAGE_FLAVOR: smoke" in compose
    assert 'user: "10001:10001"' in compose
    assert "read_only: true" in compose
    assert "cap_drop:" in compose
    assert "- ALL" in compose
    assert "no-new-privileges:true" in compose
    assert "pids_limit: 256" in compose
    assert "/tmp:rw,nosuid,nodev,size=1g" in compose
    assert "bash images/generic/scripts/offline_stack_smoke.sh" in compose
    assert '"${heartwood_python}" images/generic/scripts/terra_jupyter_demo_smoke.py' in (
        _repo_root() / "images" / "generic" / "scripts" / "offline_stack_smoke.sh"
    ).read_text(encoding="utf-8")


def test_dockerignore_excludes_development_and_model_artifacts() -> None:
    dockerignore = (_repo_root() / ".dockerignore").read_text(encoding="utf-8")

    assert ".git" in dockerignore
    assert ".venv" in dockerignore
    assert "node_modules" in dockerignore
    assert "packages/**/tests" in dockerignore
    assert ".pytest_cache" in dockerignore
    assert "*.gguf" in dockerignore
    assert "*.safetensors" in dockerignore


def test_image_flavors_define_channel_tags_and_weight_policy() -> None:
    flavors = tomllib.loads(
        (_repo_root() / "images" / "generic" / "image-flavors.toml").read_text(encoding="utf-8")
    )
    bake = (_repo_root() / "docker-bake.hcl").read_text(encoding="utf-8")

    assert flavors["moving_channel"] == "edge"
    assert flavors["architecture_helper_tag_pattern"] == "<moving-or-commit-tag>-<amd64|arm64>"
    assert flavors["flavors"]["runtime"]["moving_tag"] == "edge"
    assert flavors["flavors"]["runtime"]["bundles_model_artifact"] is True
    assert flavors["flavors"]["runtime"]["model_manifest"].endswith("qwen25-coder-7b-q4_k_m.toml")
    assert flavors["flavors"]["smoke"]["moving_tag"] == "edge-smoke"
    assert flavors["flavors"]["smoke"]["bundles_model_artifact"] is True
    assert flavors["flavors"]["providers"]["moving_tag"] == "edge-providers"
    assert flavors["flavors"]["providers"]["provider_config"].endswith(
        "provider-routes.example.toml"
    )
    assert flavors["flavors"]["coder_7b"]["target"] == "runtime"
    assert flavors["flavors"]["coder_7b"]["moving_tag"] == "edge-coder-7b"
    assert flavors["flavors"]["coder_7b"]["tag_alias_of"] == "runtime"
    assert flavors["flavors"]["coder_7b"]["bundles_model_artifact"] is True
    assert flavors["platform_flavors"]["terra_runtime"]["target"] == "terra-runtime"
    assert flavors["platform_flavors"]["terra_runtime"]["moving_tag"] == "edge-terra"
    assert (
        flavors["platform_flavors"]["terra_runtime"]["base_image"]
        == "us.gcr.io/broad-dsp-gcr-public/terra-jupyter-python:1.1.6"
    )
    assert flavors["platform_flavors"]["terra_runtime"]["bundles_model_artifact"] is True
    assert flavors["platform_flavors"]["terra_runtime"]["model_manifest"].endswith(
        "qwen25-coder-7b-q4_k_m.toml"
    )
    assert flavors["platform_flavors"]["terra_runtime"]["supported_platforms"] == ["linux/amd64"]
    assert (
        flavors["platform_flavors"]["terra_runtime"]["manifest_media_type"]
        == "application/vnd.docker.distribution.manifest.v2+json"
    )
    assert (
        flavors["platform_flavors"]["terra_runtime"]["config_media_type"]
        == "application/vnd.docker.container.image.v1+json"
    )
    assert flavors["platform_flavors"]["terra_runtime"]["publish_attestations"] is False
    assert flavors["platform_flavors"]["terra_runtime"]["allow_non_platform_manifests"] is False
    assert flavors["platform_flavors"]["terra_smoke"]["target"] == "terra-smoke"
    assert flavors["platform_flavors"]["terra_smoke"]["moving_tag"] == "edge-terra-smoke"
    assert flavors["platform_flavors"]["terra_smoke"]["bundles_model_artifact"] is True
    assert flavors["platform_flavors"]["terra_smoke"]["supported_platforms"] == ["linux/amd64"]
    assert (
        flavors["platform_flavors"]["terra_smoke"]["manifest_media_type"]
        == "application/vnd.docker.distribution.manifest.v2+json"
    )
    assert (
        flavors["platform_flavors"]["terra_smoke"]["config_media_type"]
        == "application/vnd.docker.container.image.v1+json"
    )
    assert flavors["platform_flavors"]["terra_smoke"]["publish_attestations"] is False
    assert flavors["platform_flavors"]["terra_smoke"]["allow_non_platform_manifests"] is False
    assert flavors["platform_flavors"]["terra_coder_7b"]["target"] == "terra-runtime"
    assert flavors["platform_flavors"]["terra_coder_7b"]["moving_tag"] == "edge-terra-coder-7b"
    assert flavors["platform_flavors"]["terra_coder_7b"]["tag_alias_of"] == "terra-runtime"
    assert flavors["platform_flavors"]["terra_coder_7b"]["bundles_model_artifact"] is True
    assert flavors["platform_flavors"]["terra_coder_7b"]["supported_platforms"] == ["linux/amd64"]
    assert flavors["platform_flavors"]["terra_smoke_ci"]["target"] == "terra-smoke-ci"
    assert flavors["platform_flavors"]["terra_smoke_ci"]["moving_tag"] == "edge-terra-smoke-ci"
    assert flavors["platform_flavors"]["terra_smoke_ci"]["base_image"] == (
        "heartwood-terra-ci-base:local"
    )
    assert flavors["platform_flavors"]["terra_smoke_ci"]["published"] is False
    assert 'target "runtime"' in bake
    assert 'target "smoke"' in bake
    assert 'target "providers"' in bake
    assert 'target "_platform_common"' in bake
    assert 'target "_terra_common"' in bake
    assert 'target "terra-runtime"' in bake
    assert 'target "terra-smoke"' in bake
    assert 'target "terra-smoke-ci"' in bake
    assert 'target "coder-7b"' not in bake
    assert 'target "terra-coder-7b"' not in bake
    assert (
        'target "_platform_common" {\n  context = "."\n  dockerfile = "images/platform/Dockerfile"'
        in bake
    )
    assert (
        'target "_platform_common" {\n  context = "."\n  dockerfile = "images/platform/Dockerfile"'
        "\n  pull = true\n}" in bake
    )
    assert 'cache-from = ["type=gha"]' in bake
    assert 'cache-to = ["type=gha,mode=min"]' in bake
    assert 'attest = ["type=sbom", "type=provenance,mode=max"]' in bake
    terra_runtime_block = _bake_target_block(bake, "terra-runtime")
    terra_smoke_block = _bake_target_block(bake, "terra-smoke")
    assert 'target "terra-runtime" {\n  inherits = ["_terra_common"]' in terra_runtime_block
    assert 'target "terra-smoke" {\n  inherits = ["_terra_common"]' in terra_smoke_block
    assert 'output = ["type=registry,oci-mediatypes=false"]' in terra_runtime_block
    assert 'output = ["type=registry,oci-mediatypes=false"]' in terra_smoke_block
    assert "attest" not in terra_runtime_block
    assert "attest" not in terra_smoke_block
    assert 'target "terra-smoke-ci" {\n  inherits = ["_terra_common"]\n  pull = false' in bake
    assert 'variable "TERRA_BASE_IMAGE"' in bake
    assert 'variable "TERRA_BASE_PLATFORM"' in bake
    assert 'variable "TERRA_CI_BASE_IMAGE"' in bake
    assert "us.gcr.io/broad-dsp-gcr-public/terra-jupyter-python:1.1.6" in bake
    assert "heartwood-terra-ci-base:local" in bake
    assert 'HEARTWOOD_PLATFORM_BASE_PLATFORM = "${TERRA_BASE_PLATFORM}"' in bake
    assert 'HEARTWOOD_RUNTIME_ARCH = "amd64"' in bake
    assert 'variable "IMAGE_TAG_SUFFIX"' in bake
    assert "${IMAGE_NAME}:${IMAGE_CHANNEL}${IMAGE_TAG_SUFFIX}" in bake
    assert "${IMAGE_NAME}:${IMAGE_CHANNEL}-coder-7b${IMAGE_TAG_SUFFIX}" in bake
    assert "${IMAGE_NAME}:${IMAGE_CHANNEL}-smoke${IMAGE_TAG_SUFFIX}" in bake
    assert "${IMAGE_NAME}:${IMAGE_CHANNEL}-providers${IMAGE_TAG_SUFFIX}" in bake
    assert "${IMAGE_NAME}:${IMAGE_CHANNEL}-terra${IMAGE_TAG_SUFFIX}" in bake
    assert "${IMAGE_NAME}:${IMAGE_CHANNEL}-terra-coder-7b${IMAGE_TAG_SUFFIX}" in bake
    assert "${IMAGE_NAME}:${IMAGE_CHANNEL}-terra-smoke${IMAGE_TAG_SUFFIX}" in bake
    assert "${IMAGE_NAME}:${IMAGE_CHANNEL}-terra-smoke-ci${IMAGE_TAG_SUFFIX}" in bake
    assert "${IMAGE_NAME}:sha-${GIT_SHA}-coder-7b${IMAGE_TAG_SUFFIX}" in bake
    assert "${IMAGE_NAME}:sha-${GIT_SHA}-terra${IMAGE_TAG_SUFFIX}" in bake
    assert "${IMAGE_NAME}:sha-${GIT_SHA}-terra-coder-7b${IMAGE_TAG_SUFFIX}" in bake
    assert "${IMAGE_NAME}:sha-${GIT_SHA}-terra-smoke${IMAGE_TAG_SUFFIX}" in bake
    assert 'cache-to = ["type=gha,mode=min"]' in bake
    assert 'attest = ["type=sbom", "type=provenance,mode=max"]' in bake


def test_provider_route_examples_use_file_based_secret_references() -> None:
    routes = tomllib.loads(
        (
            _repo_root() / "images" / "generic" / "providers" / "provider-routes.example.toml"
        ).read_text(encoding="utf-8")
    )

    assert routes["schema_version"] == "heartwood.provider-config.v1"
    assert routes["default_route"] == "local-loopback"
    for route in routes["routes"]:
        assert "api_key" not in route
        assert "token" not in route
        assert "secret" not in route
        if route["auth"] == "secret-file":
            assert route["secret_file"].startswith("/run/secrets/")
        if route["provider"] in {"openai", "azure-openai", "anthropic"}:
            assert route["auth"] == "secret-file"
        if route["provider"] in {"vertex-ai", "bedrock"}:
            assert route["auth"] == "managed-identity"


def test_model_catalog_records_smoke_model_and_deferred_coding_candidates() -> None:
    catalog = tomllib.loads(
        (_repo_root() / "images" / "generic" / "local-runtime" / "model-catalog.toml").read_text(
            encoding="utf-8"
        )
    )

    assert catalog["default_smoke_model"] == "llama-cpp-stories260k-ci"
    assert catalog["default_demo_coding_model"] == "qwen25-coder-7b-instruct-q4_k_m"
    smoke = catalog["models"]["llama-cpp-stories260k-ci"]
    qwen_default = catalog["models"]["qwen25-coder-7b-instruct-q4_k_m"]
    qwen_agent = catalog["models"]["qwen3-coder-30b-a3b-instruct"]
    assert smoke["status"] == "implemented"
    assert smoke["image_flavors"] == ["smoke", "terra-smoke", "terra-smoke-ci"]
    assert smoke["quality_claim"] is False
    assert qwen_default["status"] == "implemented"
    assert qwen_default["license"] == "Apache-2.0"
    assert qwen_default["artifact_format"] == "GGUF"
    assert qwen_default["quantization"] == "Q4_K_M"
    assert qwen_default["artifact_size_bytes"] == 4683073504
    assert qwen_default["artifact_sha256"] == (
        "9a961bb225cb2b9fd84b2297df0d53089895c049d7d9dc5f5f8aebbcd3247872"
    )
    assert qwen_default["image_flavors"] == [
        "runtime",
        "coder-7b",
        "terra-runtime",
        "terra-coder-7b",
    ]
    assert qwen_default["quality_claim"] is False
    assert qwen_default["minimum_resource_envelope"].startswith("4 vCPU, 16 GB RAM")
    assert qwen_default["recommended_resource_envelope"].startswith("8 vCPU, 32 GB RAM")
    assert "Not used by the current llama-cpp-cpu image" in qwen_default["gpu_acceleration"]
    assert qwen_agent["status"] == "deferred-high-memory"
    assert qwen_agent["reviewed_q4_k_m_sha256"] == (
        "fadc3e5f8d42bf7e894a785b05082e47daee4df26680389817e2093056f088ad"
    )
    assert "GPU" in " ".join(qwen_agent["implementation_requirements"])


def test_offline_stack_smoke_runs_local_model_and_cli() -> None:
    script = (_repo_root() / "images" / "generic" / "scripts" / "offline_stack_smoke.sh").read_text(
        encoding="utf-8"
    )

    assert "start_local_runtime.sh" in script
    assert "stub-loopback" in script
    assert "--local-model" in script
    assert "--target-id decision-synthetic-model-call" in script
    assert "HEARTWOOD_AGENT_BACKEND" in script
    assert "HEARTWOOD_AGENT_SERVER_ENABLED" in script
    assert "HEARTWOOD_AGENT_SERVER_API_KEY" in script
    assert "HEARTWOOD_PYTHON" in script
    assert "HEARTWOOD_AGENT_SERVER_READY_TIMEOUT_SECONDS" in script
    assert "HEARTWOOD_LOCAL_MODEL_MAX_TOKENS" in script
    assert "HEARTWOOD_LOCAL_MODEL_TIMEOUT_SECONDS" in script
    assert "HEARTWOOD_LOCAL_MODEL_MAX_TOKENS:-16" in script
    assert (
        'agent_server_ready_timeout="${HEARTWOOD_AGENT_SERVER_READY_TIMEOUT_SECONDS:-180}"'
        in script
    )
    assert "secrets.token_urlsafe" in script
    assert "heartwood-local-agent-server" not in script
    assert "start_agent_server.sh" in script
    assert 'grep -q "model=heartwood-local-runtime status=ok"' in script
    assert "openhands.bash.execute" in script
    assert "reviewer packet" in script


def test_local_runtime_profiles_distinguish_stub_from_real_runtime() -> None:
    manifest = tomllib.loads(
        (_repo_root() / "images" / "generic" / "local-runtime" / "profiles.toml").read_text(
            encoding="utf-8"
        )
    )

    assert manifest["default_profile"] == "llama-cpp-cpu"
    assert manifest["selected_real_profile"] == "llama-cpp-cpu"
    assert manifest["fallback_fixture_profile"] == "stub-loopback"
    stub = manifest["profiles"]["stub-loopback"]
    real = manifest["profiles"]["llama-cpp-cpu"]
    gpu = manifest["profiles"]["llama-cpp-cuda"]
    agent_server = manifest["agent_server"]["openhands"]
    assert stub["status"] == "implemented"
    assert stub["inference_runtime"] is False
    assert stub["quality_claim"] is False
    assert stub["supported_platforms"] == ["linux/amd64", "linux/arm64"]
    assert stub["ships_in_generic_image"] is True
    assert stub["image_flavors"] == ["runtime", "smoke", "providers"]
    assert real["status"] == "implemented"
    assert real["runtime"] == "llama.cpp llama-server binary"
    assert "ggml-org/llama.cpp b9937" in real["runtime_dependency"]
    assert real["runtime_binary"] == "/opt/llama.cpp/llama-server"
    assert any("ubuntu-x64" in artifact for artifact in real["runtime_artifacts"])
    assert any("ubuntu-arm64" in artifact for artifact in real["runtime_artifacts"])
    assert real["inference_runtime"] is True
    assert real["model_artifact_required"] is True
    assert real["supported_platforms"] == ["linux/amd64", "linux/arm64"]
    assert real["artifact_format"] == "GGUF"
    assert real["default_artifact_manifest"].endswith("stories260k.toml")
    assert real["demo_coding_artifact_manifest"].endswith("qwen25-coder-7b-q4_k_m.toml")
    assert real["artifact_checksum_policy"].startswith("Use the artifact_sha256")
    assert real["artifact_size_policy"].startswith("Use the artifact_size_bytes")
    assert real["runtime_resolution"].startswith("Bundled local-model flavors")
    assert real["ships_in_generic_image"] is True
    assert real["image_flavors"] == [
        "runtime",
        "smoke",
        "providers",
        "coder-7b",
        "terra-runtime",
        "terra-smoke",
        "terra-coder-7b",
    ]
    assert real["artifact_bundled_in_flavors"] == [
        "runtime",
        "smoke",
        "coder-7b",
        "terra-runtime",
        "terra-smoke",
        "terra-coder-7b",
    ]
    assert real["artifact_not_bundled_in_flavors"] == ["providers"]
    assert agent_server["status"] == "implemented"
    assert "openhands-agent-server==1.34.0" in agent_server["runtime_dependency"]
    assert "openhands-tools==1.34.0" in agent_server["runtime_dependency"]
    assert agent_server["gateway_owned"] is True
    assert agent_server["direct_client_endpoint"] is False
    assert agent_server["tool_execution_backend"].startswith(
        "HEARTWOOD_AGENT_BACKEND=openhands-bash"
    )
    assert gpu["status"] == "deferred"
    assert gpu["base_profile"] == "llama-cpp-cpu"
    assert gpu["supported_platforms"] == ["linux/amd64"]
    assert gpu["ships_in_generic_image"] is False


def test_local_runtime_launcher_keeps_real_profile_behind_explicit_contract() -> None:
    launcher = (
        _repo_root() / "images" / "generic" / "scripts" / "start_local_runtime.sh"
    ).read_text(encoding="utf-8")

    assert "HEARTWOOD_LOCAL_RUNTIME_PROFILE:-llama-cpp-cpu" in launcher
    assert "local runtime must bind to loopback" in launcher
    assert "python images/generic/scripts/local_model_stub.py" in launcher
    assert "llama-cpp-cpu" in launcher
    assert "llama-server" in launcher
    assert "requires llama-server on PATH" in launcher
    assert "HEARTWOOD_LOCAL_MODEL_PATH" in launcher
    assert "HEARTWOOD_LOCAL_MODEL_CONTEXT:-4096" in launcher
    assert "--alias" in launcher
    assert "--ctx-size" in launcher


def test_local_model_manifest_records_verified_gguf_artifact() -> None:
    smoke_manifest = tomllib.loads(
        (
            _repo_root() / "images" / "generic" / "local-runtime" / "models" / "stories260k.toml"
        ).read_text(encoding="utf-8")
    )
    coder_manifest = tomllib.loads(
        (
            _repo_root()
            / "images"
            / "generic"
            / "local-runtime"
            / "models"
            / "qwen25-coder-7b-q4_k_m.toml"
        ).read_text(encoding="utf-8")
    )

    assert smoke_manifest["runtime_profile"] == "llama-cpp-cpu"
    assert smoke_manifest["artifact_format"] == "GGUF"
    assert smoke_manifest["artifact_size_bytes"] == 1185376
    assert smoke_manifest["artifact_sha256"] == (
        "270cba1bd5109f42d03350f60406024560464db173c0e387d91f0426d3bd256d"
    )
    assert smoke_manifest["model_alias"] == "heartwood-local-runtime"
    assert coder_manifest["artifact_id"] == "qwen25-coder-7b-instruct-q4_k_m"
    assert coder_manifest["runtime_profile"] == "llama-cpp-cpu"
    assert coder_manifest["base_model"] == "Qwen/Qwen2.5-Coder-7B-Instruct"
    assert coder_manifest["base_model_license"] == "Apache-2.0"
    assert coder_manifest["quantized_artifact_license"] == "Apache-2.0"
    assert coder_manifest["artifact_format"] == "GGUF"
    assert coder_manifest["artifact_size_bytes"] == 4683073504
    assert coder_manifest["artifact_sha256"] == (
        "9a961bb225cb2b9fd84b2297df0d53089895c049d7d9dc5f5f8aebbcd3247872"
    )
    assert coder_manifest["minimum_resource_envelope"].startswith("4 vCPU, 16 GB RAM")
    assert coder_manifest["recommended_resource_envelope"].startswith("8 vCPU, 32 GB RAM")
    assert "does not use attached GPUs" in coder_manifest["gpu_acceleration"]
    assert coder_manifest["model_alias"] == "heartwood-local-runtime"


def test_local_model_downloader_verifies_size_and_hash(tmp_path: Path) -> None:
    source = tmp_path / "source.gguf"
    source.write_bytes(b"gguf-test-artifact")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest = tmp_path / "manifest.toml"
    manifest.write_text(
        "\n".join(
            (
                'source_url = "file://' + str(source) + '"',
                'artifact_sha256 = "' + digest + '"',
                f"artifact_size_bytes = {source.stat().st_size}",
                "",
            )
        ),
        encoding="utf-8",
    )
    output = tmp_path / "model.gguf"

    subprocess.run(
        [
            sys.executable,
            str(_repo_root() / "images" / "generic" / "scripts" / "download_model_artifact.py"),
            "--manifest",
            str(manifest),
            "--output",
            str(output),
        ],
        check=True,
    )

    assert output.read_bytes() == source.read_bytes()


def test_local_model_downloader_cleans_temp_file_after_download_failure(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "artifacts"
    manifest = tmp_path / "manifest.toml"
    manifest.write_text(
        "\n".join(
            (
                'source_url = "file://' + str(tmp_path / "missing.gguf") + '"',
                'artifact_sha256 = "' + ("0" * 64) + '"',
                "artifact_size_bytes = 1",
                "",
            )
        ),
        encoding="utf-8",
    )
    output = artifact_dir / "model.gguf"

    completed = subprocess.run(
        [
            sys.executable,
            str(_repo_root() / "images" / "generic" / "scripts" / "download_model_artifact.py"),
            "--manifest",
            str(manifest),
            "--output",
            str(output),
            "--retries",
            "1",
        ],
        check=False,
    )

    assert completed.returncode != 0
    assert not output.exists()
    assert not artifact_dir.exists() or not list(artifact_dir.iterdir())


def test_agent_server_launcher_keeps_openhands_localhost_only() -> None:
    launcher = (
        _repo_root() / "images" / "generic" / "scripts" / "start_agent_server.sh"
    ).read_text(encoding="utf-8")

    assert "agent-server must bind to loopback" in launcher
    assert "OpenHands agent-server command is not installed" in launcher
    assert "exec agent-server --host" in launcher
    assert "OH_WORKSPACE_PATH" in launcher
    assert "OH_PRELOAD_TOOLS" in launcher
    assert "OH_SESSION_API_KEYS_0" in launcher
    assert "OPENHANDS_SUPPRESS_BANNER" in launcher
    assert "secrets.token_urlsafe" in launcher
    assert "heartwood-local-agent-server" not in launcher


def test_container_image_workflow_publishes_ghcr_tags() -> None:
    workflow = (_repo_root() / ".github" / "workflows" / "container-image.yml").read_text(
        encoding="utf-8"
    )

    assert "packages: write" in workflow
    assert "docker/setup-qemu-action@v4" not in workflow
    assert "platform: linux/amd64" in workflow
    assert "runner: ubuntu-24.04" in workflow
    assert "platform: linux/arm64" in workflow
    assert "runner: ubuntu-24.04-arm" in workflow
    assert "docker/setup-buildx-action@v4" in workflow
    assert "ghcr.io/${GITHUB_REPOSITORY,,}" in workflow
    assert "IMAGE_CHANNEL: edge" in workflow
    assert "GIT_SHA: ${{ github.sha }}" in workflow
    assert "IMAGE_TAG_SUFFIX: -${{ matrix.suffix }}" in workflow
    assert "docker buildx bake --file docker-bake.hcl --push" in workflow
    assert "Free runner disk for generic image" in workflow
    generic_disk_step = _workflow_step_block(
        workflow,
        "Free runner disk for generic image",
        "Build and push architecture image flavors",
    )
    terra_disk_step = _workflow_step_block(
        workflow,
        "Free runner disk for Terra image",
        "Build and push Terra image flavors",
    )
    terra_build_step = workflow.split("- name: Build and push Terra image flavors", 1)[1].split(
        "- name: Verify Terra image tags", 1
    )[0]
    assert "--push" not in terra_build_step
    assert "docker buildx bake --file docker-bake.hcl\n          --set terra-runtime.platform" in (
        terra_build_step
    )
    assert "Build Terra image (linux/amd64)" in workflow
    assert "Free runner disk for Terra image" in workflow
    for disk_step in (generic_disk_step, terra_disk_step):
        assert "sudo rm -rf /usr/share/dotnet" in disk_step
        assert "sudo rm -rf /usr/local/lib/android" in disk_step
        assert "sudo rm -rf /opt/ghc" in disk_step
        assert "sudo rm -rf /opt/hostedtoolcache/CodeQL" in disk_step
        assert "docker system prune --all --force\n" in disk_step
        assert "docker system prune --all --force --volumes" not in disk_step
        assert "df -h" in disk_step
    assert 'BUILDX_NO_DEFAULT_ATTESTATIONS: "1"' in workflow
    assert '--set terra-runtime.platform="linux/amd64"' in workflow
    assert '--set terra-smoke.platform="linux/amd64"' in workflow
    assert "terra-runtime terra-smoke" in workflow
    assert "docker buildx imagetools inspect" in workflow
    assert "python3 images/platform/scripts/verify_registry_manifest.py" in workflow
    assert "--manifest images/platforms.toml" in workflow
    assert "--platform terra" in workflow
    assert '--image-name "${IMAGE_NAME}"' in workflow
    assert '--image-channel "${IMAGE_CHANNEL}"' in workflow
    assert '--git-sha "${GIT_SHA}"' in workflow
    assert "Free runner disk before Terra publish smoke" in workflow
    terra_publish_smoke_disk_step = _workflow_step_block(
        workflow,
        "Free runner disk before Terra publish smoke",
        "Pull published Terra smoke image",
    )
    assert "docker buildx prune --all --force" in terra_publish_smoke_disk_step
    assert "docker system prune --all --force --volumes" in terra_publish_smoke_disk_step
    assert "sudo rm -rf /usr/share/dotnet" not in terra_publish_smoke_disk_step
    assert "df -h" in terra_publish_smoke_disk_step
    assert "Pull published Terra smoke image" in workflow
    assert "Pull published Terra runtime image" not in workflow
    assert '"${IMAGE_NAME}:${IMAGE_CHANNEL}-terra-smoke"' in workflow
    assert "docker buildx imagetools create \\" in workflow
    assert "Verify image manifests" in workflow
    assert "verify_multi_platform_tag" in workflow
    assert '"${IMAGE_NAME}:${IMAGE_CHANNEL}-arm64"' in workflow
    assert '"${IMAGE_NAME}:${IMAGE_CHANNEL}-coder-7b-arm64"' in workflow
    assert '"${IMAGE_NAME}:${IMAGE_CHANNEL}-smoke-arm64"' in workflow
    assert '"${IMAGE_NAME}:${IMAGE_CHANNEL}-providers-arm64"' in workflow
    assert 'verify_multi_platform_tag "${IMAGE_CHANNEL}"' in workflow
    assert 'verify_multi_platform_tag "sha-${GIT_SHA}"' in workflow
    assert 'verify_multi_platform_tag "${IMAGE_CHANNEL}-coder-7b"' in workflow
    assert 'verify_multi_platform_tag "sha-${GIT_SHA}-coder-7b"' in workflow
    assert 'verify_multi_platform_tag "${IMAGE_CHANNEL}-smoke"' in workflow
    assert 'verify_multi_platform_tag "sha-${GIT_SHA}-smoke"' in workflow
    assert 'verify_multi_platform_tag "${IMAGE_CHANNEL}-providers"' in workflow
    assert 'verify_multi_platform_tag "sha-${GIT_SHA}-providers"' in workflow
    assert ":dev-main" not in workflow
    assert ":main" not in workflow
    assert "${{ github.sha }}" in workflow


def test_container_smoke_workflow_runs_baseline_platform_matrix() -> None:
    workflow = (_repo_root() / ".github" / "workflows" / "container-smoke.yml").read_text(
        encoding="utf-8"
    )

    assert "fail-fast: false" in workflow
    assert "platform: linux/amd64" in workflow
    assert "runner: ubuntu-24.04" in workflow
    assert "platform: linux/arm64" in workflow
    assert "runner: ubuntu-24.04-arm" in workflow
    assert "docker/setup-qemu-action@v4" not in workflow
    assert "docker/setup-buildx-action@v4" in workflow
    assert "docker buildx build --check --file images/generic/Dockerfile ." in workflow
    assert "docker buildx bake --file docker-bake.hcl --print runtime smoke providers" in workflow
    assert "DOCKER_DEFAULT_PLATFORM: ${{ matrix.platform }}" in workflow
    assert "docker compose -f images/generic/compose.yaml run --rm --build heartwood" in workflow
    assert "Terra image smoke test (linux/amd64)" in workflow
    assert "driver: docker" in workflow
    assert (
        "docker buildx build --check --platform linux/amd64 --file images/platform/Dockerfile ."
        in workflow
    )
    assert (
        "docker buildx bake --file docker-bake.hcl --print terra-runtime terra-smoke terra-smoke-ci"
        in workflow
    )
    assert "Build Terra-compatible CI base" in workflow
    assert "images/platform/terra-ci-base.Dockerfile" in workflow
    assert "heartwood-terra-ci-base:local" in workflow
    assert "docker buildx bake --file docker-bake.hcl --load" in workflow
    assert "--set terra-smoke-ci.platform=linux/amd64" in workflow
    assert (
        "docker run --rm --platform linux/amd64 --network none\n"
        "          --workdir /opt/heartwood --entrypoint bash" in workflow
    )
    assert "ghcr.io/schmiedmayerlab/heartwood:edge-terra-smoke-ci" in workflow
    assert "ghcr.io/schmiedmayerlab/heartwood:edge-terra-smoke\n" not in workflow
    assert "images/platform/scripts/terra_image_smoke.sh" in workflow
    assert "images/generic/scripts/offline_stack_smoke.sh" in workflow
    assert "Run Terra Jupyter launch smoke" in workflow
    assert "images/platform/scripts/terra_jupyter_launch_smoke.sh" in workflow


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _bake_target_block(bake: str, target: str) -> str:
    marker = f'target "{target}" {{'
    start = bake.index(marker)
    next_target = bake.find("\ntarget ", start + len(marker))
    if next_target == -1:
        return bake[start:]
    return bake[start:next_target]


def _workflow_step_block(workflow: str, step_name: str, next_step_name: str) -> str:
    return workflow.split(f"- name: {step_name}", 1)[1].split(f"- name: {next_step_name}", 1)[0]


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
                (
                    f'Bearer realm="http://127.0.0.1:{server.server_port}/token",'
                    'service="registry.test",scope="repository:test/heartwood:pull"'
                ),
            )
            self.end_headers()
            return
        if path.startswith("/v2/test/heartwood/manifests/"):
            tag = path.rsplit("/", 1)[1]
            if tag not in self.accepted_tags:
                self._write_json(404, "application/json", {"error": "unknown tag"})
                return
            self.requested_accept_headers.append(self.headers.get("Accept", ""))
            if self.headers.get("Accept") != self.manifest_media_type:
                self._write_json(406, "application/json", {"error": "unexpected accept"})
                return
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
                            (
                                "PATH=/opt/llama.cpp:/opt/conda/bin:/usr/local/bin:"
                                "/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin"
                            ),
                            "LD_LIBRARY_PATH=/opt/llama.cpp:/usr/local/nvidia/lib",
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


def _exact_package_pin(requirement: str) -> tuple[str, str]:
    parsed = Requirement(requirement)
    specifiers = list(parsed.specifier)
    assert len(specifiers) == 1
    specifier = specifiers[0]
    assert specifier.operator == "=="
    return parsed.name, specifier.version
