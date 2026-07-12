# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Contract tests for no-weight runtime and platform images."""

from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any, ClassVar, cast

from packaging.requirements import Requirement


def test_generic_image_packages_one_no_weight_runtime() -> None:
    dockerfile = _read("images/generic/Dockerfile")

    assert dockerfile.startswith("# syntax=docker/dockerfile:")
    assert "FROM node:24-trixie-slim AS webui-build" in dockerfile
    assert "uv sync --locked --no-dev --all-extras" in dockerfile
    assert "USER heartwood" in dockerfile
    assert 'CMD ["heartwood", "--help"]' in dockerfile
    assert "HEARTWOOD_AGENT_BACKEND=auto" in dockerfile
    assert "HEARTWOOD_SKILLS_DIR=/opt/heartwood/skills/verified" in dockerfile
    assert "HEARTWOOD_MODEL_CATALOG=" in dockerfile
    assert "HEARTWOOD_MODEL_CACHE=" in dockerfile
    assert "HF_HOME=/home/heartwood/.cache/heartwood/models/.huggingface" in dockerfile
    assert "LITELLM_LOCAL_MODEL_COST_MAP=True" in dockerfile
    assert "OPENHANDS_SUPPRESS_BANNER=1" in dockerfile
    assert "llama-${LLAMA_CPP_VERSION}-bin-ubuntu-x64.tar.gz" in dockerfile
    assert "llama-${LLAMA_CPP_VERSION}-bin-ubuntu-arm64.tar.gz" in dockerfile
    assert "      jq \\" in dockerfile
    assert "sha256sum --check" in dockerfile
    assert "/home/heartwood/.local/share/heartwood" in dockerfile
    assert "/home/heartwood/.cache/heartwood/models" in dockerfile
    assert "chown -R heartwood:heartwood /opt/heartwood /home/heartwood" in dockerfile
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
        "HEARTWOOD_AGENT_BACKEND=auto",
        "HEARTWOOD_SKILLS_DIR=/opt/heartwood/skills/verified",
        "HEARTWOOD_MODEL_CATALOG=",
        "HEARTWOOD_MODEL_CACHE=",
        "HF_HOME=",
        "HEARTWOOD_WEB_ROOT=",
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
    assert '"${HEARTWOOD_PLATFORM_HOME}/heartwood-workspace/models"' in platform
    assert '"${HEARTWOOD_PLATFORM_HOME}/heartwood-workspace/sessions"' in platform
    assert "USER ${HEARTWOOD_PLATFORM_USER}" in platform
    assert "WORKDIR ${HEARTWOOD_PLATFORM_HOME}" in platform
    _assert_no_embedded_model_contract(platform)

    assert terra["runtime_target"] == "terra-runtime"
    assert terra["ci_target"] == "terra-ci"
    assert terra["runtime_tag"] == "edge-terra"
    assert terra["commit_runtime_tag"] == "sha-<git-sha>-terra"
    assert terra["bundles_model_artifact"] is False
    assert terra["supported_platforms"] == ["linux/amd64"]
    assert terra["manifest_media_type"] == "application/vnd.docker.distribution.manifest.v2+json"
    assert terra["config_media_type"] == "application/vnd.docker.container.image.v1+json"
    assert terra["publish_attestations"] is False


def test_openhands_sdk_is_the_only_agent_runtime_dependency() -> None:
    gateway = _toml("packages/gateway/pyproject.toml")
    agent_dependencies = gateway["project"]["optional-dependencies"]["agent"]
    pins = dict(_exact_package_pin(requirement) for requirement in agent_dependencies)

    assert pins == {"openhands-sdk": "1.33.0", "openhands-tools": "1.34.0"}
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
    assert flavors["platform_flavors"]["terra_runtime"]["bundles_model_artifact"] is False
    assert "No Heartwood image contains model weights" in flavors["model_weight_policy"]


def test_bake_file_has_one_runtime_and_one_platform_variant() -> None:
    bake = _read("docker-bake.hcl")

    assert _target_names(bake) == {"runtime", "_terra_common", "terra-runtime", "terra-ci"}
    assert 'targets = ["runtime"]' in bake
    assert 'platforms = ["linux/amd64", "linux/arm64"]' in bake
    assert 'output = ["type=registry,oci-mediatypes=false"]' in bake
    assert "HEARTWOOD_BUNDLE_LOCAL_MODEL" not in bake
    assert "coder-7b" not in bake
    assert 'target "smoke"' not in bake
    assert 'target "providers"' not in bake


def test_isolated_smoke_uses_real_openhands_sdk_without_weights() -> None:
    compose = _read("images/generic/compose.yaml")
    smoke = _read("images/generic/scripts/offline_stack_smoke.sh")
    model_stub = _read("images/generic/scripts/local_model_stub.py")

    assert "network_mode: none" in compose
    assert "read_only: true" in compose
    assert "cap_drop:" in compose
    assert "- ALL" in compose
    assert "no-new-privileges:true" in compose
    assert "HEARTWOOD_BUNDLE_LOCAL_MODEL" not in compose
    assert "HEARTWOOD_AGENT_BACKEND: openhands-sdk" in compose
    assert "image: heartwood-runtime-smoke:local" in compose
    assert "HEARTWOOD_LOCAL_RUNTIME_PROFILE=stub-loopback" in smoke
    assert '"${state_root}/openhands"' in smoke
    assert '"${state_root}/workspaces"' in smoke
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
    assert "Action approved" in smoke
    assert "Action denied" in smoke
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
        "images/generic/scripts/start_demo_stack.sh",
        "images/generic/scripts/start_local_runtime.sh",
        "images/generic/scripts/start_web_ui.sh",
        "images/platform/scripts/terra_image_smoke.sh",
        "images/platform/scripts/terra_jupyter_contract_smoke.sh",
        "images/platform/scripts/terra_jupyter_launch_smoke.sh",
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
    demo = _read("images/generic/scripts/start_demo_stack.sh")
    assert 'model_path="${HEARTWOOD_LOCAL_MODEL_PATH:-}"' in local_runtime
    assert "mounted or downloaded GGUF file" in local_runtime
    assert "HEARTWOOD_DEMO_START_LOCAL_RUNTIME:-0" in demo
    assert "start_agent_server" not in demo

    jupyter_smoke = _read("images/generic/scripts/terra_jupyter_demo_smoke.py")
    assert '"chat"' in jupyter_smoke
    assert '"approve"' in jupyter_smoke
    assert '"confirmation.requested"' in jupyter_smoke
    assert '"tool.execution.recorded"' in jupyter_smoke


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
        "Run staged Terra OpenHands smoke"
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
    assert "--print runtime" in smoke
    assert "--print terra-runtime terra-ci" in smoke
    assert "edge-terra-ci" in smoke
    assert "images/generic/scripts/offline_stack_smoke.sh" in smoke
    assert "container_persistence_smoke.sh" in smoke
    assert "Download and verify CI-only model fixture" in smoke
    assert "heartwood models download llama-cpp-stories260k-ci" in smoke
    assert "--volume heartwood-ci-model:/home/heartwood/.cache/heartwood/models" in smoke
    assert "--volume heartwood-ci-model:/models:ro" in smoke
    assert "--volume heartwood-terra-ci-model:/home/jupyter/heartwood-workspace/models" in smoke
    assert "--volume heartwood-terra-ci-model:/models:ro" in smoke
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
    assert offline_guide.count("uid=10001,gid=10001,mode=0700") == 2
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
