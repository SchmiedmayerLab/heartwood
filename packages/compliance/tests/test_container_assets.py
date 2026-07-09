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
from pathlib import Path


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
    assert "openhands-tools==1.33.0" in gateway_pyproject
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
    assert "ARG HEARTWOOD_UID=10001" in dockerfile
    assert "ARG HEARTWOOD_GID=10001" in dockerfile
    assert "groupadd --system --gid" in dockerfile
    assert "useradd --system --uid" in dockerfile
    assert "COPY --chown=heartwood:heartwood packages ./packages" in dockerfile
    assert "COPY --chown=heartwood:heartwood fixtures ./fixtures" in dockerfile
    assert "COPY --chown=heartwood:heartwood skills ./skills" in dockerfile
    assert "COPY --chown=heartwood:heartwood images ./images" in dockerfile
    assert "USER heartwood" in dockerfile
    assert 'PATH="/opt/llama.cpp:/opt/heartwood/.venv/bin:${PATH}"' in dockerfile
    assert 'CMD ["heartwood", "--help"]' in dockerfile


def test_web_ui_package_has_ci_and_container_launcher() -> None:
    package = json.loads(
        (_repo_root() / "packages" / "webui" / "package.json").read_text(encoding="utf-8")
    )
    workflow = (_repo_root() / ".github" / "workflows" / "web-ui.yml").read_text(encoding="utf-8")
    launcher = (_repo_root() / "images" / "generic" / "scripts" / "start_web_ui.sh").read_text(
        encoding="utf-8"
    )

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
    assert '--web-root "${web_root}"' in launcher
    assert '--base-path "${base_path}"' in launcher


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
    assert flavors["flavors"]["runtime"]["bundles_model_artifact"] is False
    assert flavors["flavors"]["smoke"]["moving_tag"] == "edge-smoke"
    assert flavors["flavors"]["smoke"]["bundles_model_artifact"] is True
    assert flavors["flavors"]["providers"]["moving_tag"] == "edge-providers"
    assert flavors["flavors"]["providers"]["provider_config"].endswith(
        "provider-routes.example.toml"
    )
    assert 'target "runtime"' in bake
    assert 'target "smoke"' in bake
    assert 'target "providers"' in bake
    assert 'variable "IMAGE_TAG_SUFFIX"' in bake
    assert "${IMAGE_NAME}:${IMAGE_CHANNEL}${IMAGE_TAG_SUFFIX}" in bake
    assert "${IMAGE_NAME}:${IMAGE_CHANNEL}-smoke${IMAGE_TAG_SUFFIX}" in bake
    assert "${IMAGE_NAME}:${IMAGE_CHANNEL}-providers${IMAGE_TAG_SUFFIX}" in bake
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
    smoke = catalog["models"]["llama-cpp-stories260k-ci"]
    qwen_small = catalog["models"]["qwen25-coder-1_5b-instruct"]
    qwen_agent = catalog["models"]["qwen3-coder-30b-a3b-instruct"]
    assert smoke["status"] == "implemented"
    assert smoke["image_flavors"] == ["smoke"]
    assert smoke["quality_claim"] is False
    assert qwen_small["status"] == "candidate"
    assert qwen_small["license"] == "Apache-2.0"
    assert qwen_small["expected_artifact_format"] == "GGUF"
    assert qwen_agent["status"] == "candidate"
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
    assert "HEARTWOOD_AGENT_SERVER_READY_TIMEOUT_SECONDS" in script
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
    assert real["artifact_checksum"] == (
        "SHA-256 270cba1bd5109f42d03350f60406024560464db173c0e387d91f0426d3bd256d"
    )
    assert real["artifact_size_bytes"] == 1185376
    assert real["runtime_resolution"].startswith("The runtime flavor expects")
    assert real["ships_in_generic_image"] is True
    assert real["image_flavors"] == ["runtime", "smoke", "providers"]
    assert real["artifact_bundled_in_flavors"] == ["smoke"]
    assert real["artifact_not_bundled_in_flavors"] == ["runtime", "providers"]
    assert agent_server["status"] == "implemented"
    assert "openhands-agent-server==1.33.0" in agent_server["runtime_dependency"]
    assert "openhands-tools==1.33.0" in agent_server["runtime_dependency"]
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
    assert "--alias" in launcher
    assert "--ctx-size" in launcher


def test_local_model_manifest_records_verified_gguf_artifact() -> None:
    manifest = tomllib.loads(
        (
            _repo_root() / "images" / "generic" / "local-runtime" / "models" / "stories260k.toml"
        ).read_text(encoding="utf-8")
    )

    assert manifest["runtime_profile"] == "llama-cpp-cpu"
    assert manifest["artifact_format"] == "GGUF"
    assert manifest["artifact_size_bytes"] == 1185376
    assert manifest["artifact_sha256"] == (
        "270cba1bd5109f42d03350f60406024560464db173c0e387d91f0426d3bd256d"
    )
    assert manifest["model_alias"] == "heartwood-local-runtime"


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
    assert "docker buildx imagetools create \\" in workflow
    assert '"${IMAGE_NAME}:${IMAGE_CHANNEL}-arm64"' in workflow
    assert '"${IMAGE_NAME}:${IMAGE_CHANNEL}-smoke-arm64"' in workflow
    assert '"${IMAGE_NAME}:${IMAGE_CHANNEL}-providers-arm64"' in workflow
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


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]
