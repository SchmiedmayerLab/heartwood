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
| `edge-gpu-nvidia` | `linux/amd64` | Moving generic NVIDIA runtime with isolated vLLM and no weights. |
| `sha-<git-sha>-gpu-nvidia` | `linux/amd64` | Immutable generic NVIDIA runtime. |
| `edge-terra-gpu-nvidia` | `linux/amd64` | Moving Terra-derived NVIDIA runtime with isolated vLLM and no weights. |
| `sha-<git-sha>-terra-gpu-nvidia` | `linux/amd64` | Immutable Terra-derived NVIDIA runtime. |
| `<semver>` | `linux/amd64`, `linux/arm64` | Protected release of the verified generic runtime. |
| `<semver>-terra` | `linux/amd64` | Protected release of the verified Terra-derived runtime. |
| `<semver>-gpu-nvidia` | `linux/amd64` | Protected release of the verified generic NVIDIA runtime. |
| `<semver>-terra-gpu-nvidia` | `linux/amd64` | Protected release of the verified Terra-derived NVIDIA runtime. |

Do not publish `latest` before the first stable release. Model names, provider names, branch names, and architecture-helper suffixes are not public flavor tags.

## Release Tags

The protected release workflow publishes strict Semantic Version tags without a `v` prefix only after every declared check passes on the exact current `main` commit and the designated maintainer approves publication. Version tags copy verified immutable commit manifests and refuse an existing tag with a different digest. Build metadata uses `_` instead of `+` only in the container tag because OCI tag syntax does not permit `+`. See [Releases](releases.md) for the gate and artifact contract. Image signing, retention automation, generated notices, and a formal compatibility and support policy remain release-assurance work.

Main publication separates staging from promotion. The publication jobs run only for the `main` ref; pull requests continue to run CI validation without publishing candidates or tags. A build may push content-addressed candidate manifests by digest, but it does not create or move a public tag until the exact candidate has passed its required checks. The immutable commit tag is created first and verified; a rerun may reuse it only when its digest or normalized manifest matches and must fail rather than overwrite a different artifact. The moving `edge` tag is updated last from that verified commit tag. The promotion step checks the current `main` commit immediately before moving the channel and refuses promotion if the branch has already advanced. A staging or candidate-validation failure leaves the candidate untagged and the previous moving tag unchanged. A later freshness or promotion failure may leave the validated immutable commit tag publicly reachable, but it cannot expose an unvalidated candidate under a public tag.

The generic workflow builds on native AMD64 and ARM64 GitHub runners and pushes each result by digest without a tag. Each runner pulls its exact staged digest and runs the no-network OpenHands and mounted llama.cpp smokes before uploading a digest marker. The promotion job accepts markers only from successful jobs, dry-runs and validates the combined index, creates and verifies `sha-<git-sha>`, and then copies that manifest to `edge`. This avoids persistent `-amd64` and `-arm64` helper tags while retaining software bill of materials and provenance attestations.

Terra is intentionally separate. Leonardo image auto-detection requires a single-platform Docker schema-2 manifest and does not accept the generic multi-platform Open Container Initiative index. Terra publication stages an untagged digest with `application/vnd.docker.distribution.manifest.v2+json`, disables attestations that would wrap the image in an index, and validates anonymous registry access, media type, platform, user, workdir, entrypoint, ports, required environment, Jupyter launch modes, OpenHands, and mounted local inference before creating `sha-<git-sha>-terra`. It then verifies the immutable tag and moves `edge-terra` last while preserving the single-manifest format.

## Model And Credential Policy

No Dockerfile accepts a model path or model manifest build argument, and no build step downloads weights. `images/generic/image-flavors.toml`, `images/platforms.toml`, and static tests enforce this contract.

Provider configuration is runtime state:

- `model` is a LiteLLM provider/model identifier consumed by OpenHands.
- `base_url` points to a custom or local OpenAI-compatible service when needed and must share the policy endpoint's origin.
- `policy_endpoint` is the declared normalized route Heartwood authorizes before initial task submission and before an approved or resumed continuation that may call the model. For provider-native routing without `base_url`, platform network controls must independently enforce the actual destination.
- `credential_kind` is `environment`, `file`, `managed-identity`, or `none` for loopback-only endpoints.
- `api_key_env` and `api_key_file` are references. Secret values are resolved only in memory.

Model profiles and the selected action-confirmation mode are stored in separate mode-`0600` JSON files outside session directories. Neither file contains credential values. Deployment policy must allow the selected capability tier, confirmation mode, and non-secret credential reference in addition to the endpoint. `credential_allowlist` uses environment-variable names, absolute mounted-file paths, or `managed-identity`. Valid settings cannot bypass a policy denial.

For an environment-referenced provider key, Heartwood passes only the active value to the in-process OpenHands model client and blanks every configured model-key environment reference in OpenHands terminal subprocesses. A mounted credential file or platform-managed identity available to the container user is not isolated from agent-executed code by this interactive-container architecture. Use least-privilege identities and a deployment-owned process, remote-workspace, or platform boundary when a model credential must be inaccessible to coding tools.

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

Native environments use the same application and dependency locks through GitHub Release assets rather than a platform image. The release publishes `heartwood-installer`, `heartwood-native.tar.gz`, and `SHA256SUMS`; pull requests and `main` build and dry-run the same assets without publishing. Native installation contains no model weights or credentials and does not request compute. The installed `heartwood launch` command owns platform-aware compute planning and runtime startup.

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

The local server binds to loopback by default. Use `heartwood models refresh local` and `heartwood models connect local <model-id>` to select the identifier reported by its OpenAI-compatible model-list route.

CPU and memory requirements are determined by the selected model and runtime, not the Heartwood image. The catalog records a reviewed envelope for each optional artifact. GPU acceleration requires a separately installed and tested GPU-capable runtime; attaching a GPU does not make the baseline CPU `llama-server` use it.

Use an explicit NVIDIA variant when the deployment needs an in-image GPU server. It installs vLLM in `/opt/heartwood-vllm`, separate from the Heartwood environment, from a version- and artifact-hash-locked requirements set, and uses `images/gpu/start_vllm.sh` with an externally mounted Hugging Face snapshot. The launcher binds to loopback and enables automatic tool choice; no image downloads or contains a model. The portable images remain the public defaults because they support AMD64 and ARM64 without a vendor driver contract.

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
- model connection, catalog, profile, and artifact integrity tests;
- a no-weight Terra CI image built through the production platform Dockerfile;
- Terra Jupyter contract, platform payload, inherited entrypoint, Leonardo route, and OpenHands loopback smokes.

Main publication repeats the integration checks against untagged, content-addressed generic and real Terra-derived candidates. Public commit and moving tags are promotion outputs, not test inputs. The CI fixture validates orchestration, policy, audit, and interface wiring; it makes no model capability claim.

## Registry Maintenance

Protect moving tags, retained commit tags, stable release tags, generic attestations, and manifests referenced by a multi-platform index. The digest-based build does not create architecture helper tags. Failed candidates remain untagged and cannot replace a public tag. They are not deleted in the publishing run because GitHub Container Registry deletion operates on package versions and valid tagged indexes depend on untagged child and attestation manifests. Cleanup automation must begin in report-only mode, traverse every protected tag and referrer to build a reachability graph, preserve all reachable manifests and blobs, apply an age threshold to unreachable versions, and use narrowly scoped package deletion permissions only after the report is reviewed.

See the [Platform Image Extension Guide](platform-images.md) for adding another platform-derived runtime.
