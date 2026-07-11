<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Container Images

Heartwood publishes one generic runtime and thin platform-derived runtimes. Every published image contains the same Heartwood application, OpenHands SDK adapter, repository-verified Skills, CLI, notebook bridge, web UI, policy controls, audit implementation, and optional `llama-server` runtime. Images never contain model weights or credentials.

This document describes current image and publication behavior. [Platform Support](platform-support.md) records which paths are implemented, CI-validated, or still awaiting live-platform evidence. Future image and release work belongs in the [Delivery Roadmap](../design/09-implementation-plan.md).

## Current Published Tags

| Tag | Platform | Purpose |
|---|---|---|
| `edge` | `linux/amd64`, `linux/arm64` | Moving main-branch generic runtime. |
| `sha-<git-sha>` | `linux/amd64`, `linux/arm64` | Immutable generic runtime for one commit. |
| `edge-terra` | `linux/amd64` | Moving main-branch runtime derived from the pinned Terra Jupyter base. |
| `sha-<git-sha>-terra` | `linux/amd64` | Immutable Terra-derived runtime for one commit. |

Do not publish `latest` before the first stable release. Model names, provider names, branch names, and architecture-helper suffixes are not public flavor tags.

## Future Release Tags

Stable `v<semver>` tags are not published yet. Their retention, signing, release notes, and compatibility policy must be implemented before the first stable release.

The generic workflow builds on native AMD64 and ARM64 GitHub runners, pushes each result by digest, transfers only digest markers between jobs, and joins the digests into the final multi-platform tags. This avoids persistent `-amd64` and `-arm64` helper tags. Generic images retain software bill of materials and provenance attestations.

Terra is intentionally separate. Leonardo image auto-detection requires a single-platform Docker schema-2 manifest and does not accept the generic multi-platform Open Container Initiative index. Terra publication therefore emits `application/vnd.docker.distribution.manifest.v2+json`, disables attestations that would wrap the image in an index, and validates anonymous registry access, media type, platform, user, workdir, entrypoint, ports, and required environment through `images/platform/scripts/verify_registry_manifest.py`.

## Model And Credential Policy

No Dockerfile accepts a model path or model manifest build argument, and no build step downloads weights. `images/generic/image-flavors.toml`, `images/platforms.toml`, and static tests enforce this contract.

Provider configuration is runtime state:

- `model` is a LiteLLM provider/model identifier consumed by OpenHands.
- `base_url` points to a custom or local OpenAI-compatible service when needed and must share the policy endpoint's origin.
- `policy_endpoint` is the declared normalized route Heartwood authorizes before initial task submission and before an approved or resumed continuation that may call the model. For provider-native routing without `base_url`, platform network controls must independently enforce the actual destination.
- `credential_kind` is `environment`, `file`, `managed-identity`, or `none` for loopback-only endpoints.
- `api_key_env` and `api_key_file` are references. Secret values are resolved only in memory.

Model profiles and the selected action-confirmation mode are stored in separate mode-`0600` JSON files outside session directories. Neither file contains credential values. Deployment policy must allow the selected capability tier, confirmation mode, and non-secret credential reference in addition to the endpoint. `credential_allowlist` uses environment-variable names, absolute mounted-file paths, or `managed-identity`. Valid settings cannot bypass a policy denial.

For an environment-referenced provider key, Heartwood passes only the active value to the in-process OpenHands model client and blanks every configured model-key environment reference in OpenHands terminal subprocesses. A mounted credential file or platform managed identity available to the container user is not isolated from agent-executed code by this interactive-container architecture. Use least-privilege identities and a deployment-owned process, remote-workspace, or platform boundary when a model credential must be inaccessible to coding tools.

For local models, use one of three equivalent deployment patterns:

1. Run Ollama, vLLM, SGLang, llama.cpp, or another OpenAI-compatible service beside Heartwood and configure its endpoint.
2. Mount an existing model artifact and set `HEARTWOOD_LOCAL_MODEL_PATH` before starting `images/generic/scripts/start_local_runtime.sh`.
3. Explicitly download a reviewed catalog artifact into mounted storage with `heartwood models download <artifact-id>`, then pass the printed path to the local runtime launcher.

The reviewed downloader pins the Hugging Face repository revision, byte size, SHA-256 digest, format, and license posture. Download success is an integrity check, not a model-quality or biomedical-suitability claim.

## Generic Runtime

Start the unconfigured web interface with persistent state and model cache volumes:

```bash
docker run --rm -p 127.0.0.1:8767:8767 \
  -v heartwood-state:/home/heartwood/.local/share/heartwood \
  -v heartwood-models:/home/heartwood/.cache/heartwood/models \
  ghcr.io/schmiedmayerlab/heartwood:edge \
  bash images/generic/scripts/start_demo_stack.sh
```

The service starts without a secret or model. Configure a profile from the web settings panel or the CLI. Action confirmation defaults to **Ask Every Time**; generic synthetic development can select **Auto-Approve Low Risk** through the same panel or `heartwood actions`. To use a runtime credential, pass an environment variable or mount a secret file at container start. Never use Docker `ARG` or Dockerfile `ENV` for a secret value.

The state volume contains sessions, non-secret model and action settings, installed Skills, OpenHands state, workspaces, and audit data. The separate model volume allows large weights to use a different quota and retention policy and also owns Hugging Face transfer metadata through `HF_HOME`. Override `HEARTWOOD_MODEL_CACHE` and `HF_HOME` together when mounting a different model path. [Issue #22](https://github.com/SchmiedmayerLab/heartwood/issues/22) tracks a canonical versioned root and one-volume default while preserving the split cache as an advanced option; the current two-volume layout remains the supported contract until that migration is implemented and restart-tested.

Run an explicitly mounted local model in the same container:

```bash
docker run --rm -p 127.0.0.1:8767:8767 \
  -v heartwood-state:/home/heartwood/.local/share/heartwood \
  -v heartwood-models:/home/heartwood/.cache/heartwood/models \
  -e HEARTWOOD_DEMO_START_LOCAL_RUNTIME=1 \
  -e HEARTWOOD_LOCAL_MODEL_PATH=/home/heartwood/.cache/heartwood/models/<artifact-id>/<file>.gguf \
  ghcr.io/schmiedmayerlab/heartwood:edge \
  bash images/generic/scripts/start_demo_stack.sh
```

The local server binds to loopback by default. Configure an OpenAI-compatible profile with base URL `http://127.0.0.1:8765/v1`, policy endpoint `http://127.0.0.1:8765/v1/chat/completions`, and credential kind `none`.

CPU and memory requirements are determined by the selected model and runtime, not the Heartwood image. The catalog records a reviewed envelope for each optional artifact. GPU acceleration requires a separately installed and tested GPU-capable runtime; attaching a GPU does not make the baseline CPU `llama-server` use it.

## Terra Runtime

The Terra Dockerfile starts from `us.gcr.io/broad-dsp-gcr-public/terra-jupyter-python:1.1.6` and adds Heartwood under `/opt/heartwood`. It deliberately preserves:

- user `jupyter` and home `/home/jupyter`;
- the platform Jupyter environment ahead of Heartwood on `PATH`;
- the inherited Jupyter notebook entrypoint and port `8000`;
- the Leonardo `/notebooks/...` launch behavior;
- a separate registered `Python 3 (Heartwood)` kernel;
- persistent Heartwood state and model cache paths under `/home/jupyter/heartwood-workspace`.

The Terra runtime is no-weight for the same reason as the generic image. Use workspace-persistent storage for optional local artifacts or configure an institution-authorized endpoint. A hosted service is appropriate for regulated data only when the institution has verified its agreement, covered product, identity, regional, logging, retention, and network configuration.

The image and publication contract are implemented and CI-validated. Live synthetic validation in the Terra control plane remains outstanding, so the repository does not yet claim live Terra support for a specific institutional deployment.

## Continuous Integration

Pull requests run:

- Docker Buildx checks for both Dockerfiles;
- the generic runtime on native AMD64 and ARM64 runners;
- a no-network Compose smoke that uses the deterministic OpenAI-compatible fixture through a real OpenHands `Conversation`;
- fresh named-volume creation and cross-container recovery for state and model storage;
- OpenHands native loading of every repository-verified Skill;
- model profile and artifact integrity tests;
- a no-weight Terra CI image built through the production platform Dockerfile;
- Terra Jupyter contract, platform payload, inherited entrypoint, Leonardo route, and OpenHands loopback smokes.

Main publication repeats the integration checks against the published generic and real Terra-derived tags. The CI fixture validates orchestration, policy, audit, and interface wiring; it makes no model capability claim.

## Registry Maintenance

Protect moving tags, retained commit tags, stable release tags, generic attestations, and manifests referenced by a multi-platform index. The digest-based build does not create architecture helper tags. Any cleanup automation must begin in report-only mode, preserve protected tags and referenced digests, define a retention window for unreferenced commit artifacts, and use narrowly scoped package deletion permissions.

See the [Platform Image Extension Guide](platform-images.md) for adding another platform-derived runtime.
