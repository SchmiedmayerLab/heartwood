<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Container Images

Heartwood publishes one image family from one Dockerfile and one Buildx Bake file. Image flavors differ by build arguments, tags, model artifact inclusion, and intended use; they do not fork the security baseline.

## Tag Scheme

| Tag | Meaning |
|---|---|
| `edge` | Moving main-branch runtime image. Not a stable release. |
| `edge-smoke` | Moving main-branch smoke image with the tiny verified GGUF artifact for offline CI and tutorials. |
| `edge-providers` | Moving main-branch provider-route image with file-based provider secret configuration support and no bundled model weights. |
| `edge-terra` | Moving main-branch Terra-derived notebook image built from the selected Terra Jupyter Python base, without bundled model weights. |
| `edge-terra-smoke` | Moving main-branch Terra-derived notebook image with the tiny verified GGUF artifact for Terra demos and CI smoke. |
| `sha-<git-sha>` | Exact runtime image for one commit. |
| `sha-<git-sha>-smoke` | Exact smoke image for one commit. |
| `sha-<git-sha>-providers` | Exact provider-route image for one commit. |
| `sha-<git-sha>-terra` | Exact Terra-derived runtime image for one commit. |
| `sha-<git-sha>-terra-smoke` | Exact Terra-derived smoke image for one commit. |
| `v<semver>` | Future stable runtime release. |
| `v<semver>-<flavor>` | Future stable release for a non-default flavor. |

Do not use `latest` until the first stable release exists. Do not use branch names such as `main` or informal tags such as `dev-main` for user-facing image references.

The generic publish workflow builds `linux/amd64` and `linux/arm64` on native GitHub-hosted runners, pushes architecture helper tags such as `edge-amd64` and `edge-arm64`, creates the public multi-architecture generic tags with `docker buildx imagetools create`, keeps Buildx SBOM/provenance attestations on those generic image artifacts, and verifies the public tags through an unauthenticated registry inspection before the workflow succeeds. Treat architecture helper tags as publication internals for debugging and manifest assembly, not stable user-facing references. Terra-derived images publish as `linux/amd64` Docker schema-2 image manifests with media type `application/vnd.docker.distribution.manifest.v2+json` because the selected Terra notebook base is amd64 and Terra Leonardo image auto-detection accepts Docker manifest v2 media types but not OCI indexes. The Terra publish path disables Buildx default attestations for `edge-terra`, `edge-terra-smoke`, and commit-SHA Terra tags because attached attestations would force an OCI image index that Leonardo rejects. Platform-derived tag checks are driven by `images/platform/scripts/verify_registry_manifest.py` and the `images/platforms.toml` manifest so future platforms can declare their own manifest media type, config media type, supported architecture set, and non-platform manifest policy. Pull-request CI uses `edge-terra-smoke-ci` only as a local test tag built from a lightweight Terra-compatible base; it is not published as a user-facing platform image.

The `SchmiedmayerLab/heartwood` GHCR package must stay public because Terra cannot use the main demo tags without anonymous pull access. If GitHub reports the package as private, change the package visibility from the GitHub package settings page before relying on the image in Terra. The workflow verifies Terra tags with the same unauthenticated Docker schema-2 manifest request shape that Leonardo uses; this manual check is stricter than `docker manifest inspect`, which can succeed on OCI indexes that Leonardo rejects:

```bash
python3 images/platform/scripts/verify_registry_manifest.py --manifest images/platforms.toml --platform terra --image-name ghcr.io/schmiedmayerlab/heartwood --git-sha <published-git-sha>
```

Registry maintenance must protect public moving tags, commit-SHA tags retained by policy, future semver tags, generic SBOM/provenance artifacts, and any manifest referenced by a public multi-architecture index. Terra user-facing tags intentionally lack Buildx attestations until Leonardo accepts OCI indexes or OCI image manifests. Cleanup automation should begin with dry-run reports, delete only stale helper tags or unreferenced versions outside the retention window, and record the GHCR permissions required to perform deletions. The next documentation and governance pass must keep post-publish registry verification aligned with this tag policy and add the dry-run cleanup policy.

## Current Flavors

| Flavor | Bake Target | Moving Tag | Bundled Model Artifact | Intended Use |
|---|---|---|---|---|
| Runtime | `runtime` | `edge` | No | Default platform-ready image for CLI, gateway, notebook bridge, built researcher web UI, OpenHands launcher, local inference runtime dependencies, provider route config/invocation support, and externally mounted model artifacts. |
| Smoke | `smoke` | `edge-smoke` | Yes, tiny checked GGUF | Pull-request CI, main-branch CI, and Docker-only tutorials proving offline load/query, gateway policy, OpenHands bash execution, audit export, and reviewer packet generation. |
| Providers | `providers` | `edge-providers` | No | Platform embedding where model access is supplied by in-boundary provider endpoints and credentials are mounted at runtime. |
| Terra Runtime | `terra-runtime` | `edge-terra` | No | Terra-derived Jupyter notebook image built from `us.gcr.io/broad-dsp-gcr-public/terra-jupyter-python:1.1.6`, with Heartwood installed under `/opt/heartwood` and a registered Heartwood kernel. |
| Terra Smoke | `terra-smoke` | `edge-terra-smoke` | Yes, tiny checked GGUF | Terra-derived Jupyter notebook image for synthetic Terra demos and CI, proving local inference, gateway policy, OpenHands bash execution, notebook API, web UI, audit export, and reviewer packet generation. |

The smoke flavor is intentionally not the default image because the default image should stay platform-ready and should not imply that bundled weights are required. The smoke model exists to prove the stack runs air-gapped; it is not a coding-quality, biomedical, or production model.

## Platform Notebook Images

The generic image family is the portable Heartwood runtime baseline. It is suitable for local Docker, Docker Compose, CI, and as the source runtime for platform-specific images, but it is not the Terra custom notebook image users should select in Terra. The repeatable mechanism for adding or adapting a platform-derived notebook image is defined in [Platform Image Extension Guide](platform-images.md).

Terra's current Jupyter custom-environment documentation directs custom notebook images to extend a Terra Jupyter base image or a project-specific image accepted by Terra's notebook service. The implemented Terra target derives from `us.gcr.io/broad-dsp-gcr-public/terra-jupyter-python:1.1.6`, installs Heartwood under `/opt/heartwood`, preserves `/opt/heartwood/docs/terra-jupyter-demo.ipynb`, registers a `heartwood` Jupyter kernel under `/opt/conda`, keeps Jupyter on the Terra base image's expected notebook service path, and exposes the Heartwood gateway on loopback for notebook-proxy access.

Use `ghcr.io/schmiedmayerlab/heartwood:edge-terra-smoke` for the first synthetic Terra demo after the main-branch publish workflow completes and the GHCR package is public. Do not document `edge`, `edge-smoke`, or `edge-providers` as supported Terra custom environment images. CI proves the platform Dockerfile and offline smoke path through the Terra-compatible CI base, while the main-branch publish workflow builds the real Terra-derived image and fails if an unauthenticated Leonardo-compatible Docker manifest request cannot inspect the published tag; live Terra workspace validation must still record the Terra base image digest, Heartwood image digest, VM shape, startup behavior, proxy path behavior, one synthetic web UI chat command, CLI/notebook replay evidence, audit export path, reviewer packet path, and any identity headers available to the gateway.

The pull-request Terra smoke job builds `images/platform/terra-ci-base.Dockerfile` and then builds `terra-smoke-ci` through the same `images/platform/Dockerfile`. That job validates Heartwood packaging, the Jupyter home/prefix assumptions, kernel registration, the platform smoke script, and the offline stack with runtime network disabled without pulling the multi-gigabyte Terra base on every pull request. The main-branch publish job builds `terra-runtime` and `terra-smoke` from the real Terra base after freeing runner disk space, then verifies the published Terra tags.

## Provider Secrets

Provider credentials are runtime inputs, never image inputs. Do not place provider API keys in Dockerfile `ARG`, Dockerfile `ENV`, labels, checked-in TOML values, shell scripts, examples, logs, session events, audit exports, or reviewer packets.

Provider routes use file-based secret references:

```toml
[[routes]]
route_id = "openai"
provider = "openai"
endpoint = "https://api.openai.com/v1/chat/completions"
model = "configured-by-platform"
capability_tier = "supervised"
auth = "secret-file"
secret_file = "/run/secrets/openai_api_key"
```

Docker Compose mounts secrets under `/run/secrets/<name>`. Terra, Seven Bridges, DNAnexus, and other controlled platforms should map their own secret or identity mechanisms into a runtime file path or managed identity route, then the active policy profile must explicitly allowlist the endpoint before any invocation. The implemented provider invocation path supports OpenAI-compatible chat-completions routes, including local loopback, OpenAI, Azure OpenAI, llama.cpp, and vLLM routes; managed identity routes are validated as metadata and require a platform adapter before invocation.

## Researcher Web UI

The generic image builds `packages/webui` during the Docker build and copies the static `dist` assets into `/opt/heartwood/packages/webui/dist`. The final image does not carry `node_modules`; runtime serving uses the Python gateway only.

Start the self-contained local demo from the smoke image with:

```bash
docker run --rm -p 8767:8767 ghcr.io/schmiedmayerlab/heartwood:edge-smoke bash images/generic/scripts/start_demo_stack.sh
```

`images/generic/scripts/start_demo_stack.sh` starts the local llama.cpp smoke runtime, seeds the synthetic model-call approval for `session-local`, enables the demo-only bounded synthetic response preview, starts the gateway-managed localhost OpenHands child server, and serves the packaged web UI on `0.0.0.0:8767` for local Docker port publishing. Set `HEARTWOOD_DEMO_WEB_HOST` only when the demo container must bind a different internal host. Use `images/generic/scripts/start_web_ui.sh` directly when the local model is supplied elsewhere or when serving through Terra/Jupyter, where the default `127.0.0.1` bind address is expected. The launcher reads `HEARTWOOD_WORKSPACE`, `HEARTWOOD_WEB_HOST`, `HEARTWOOD_WEB_PORT`, `HEARTWOOD_WEB_ROOT`, and `HEARTWOOD_WEB_BASE_PATH`. Use `HEARTWOOD_WEB_BASE_PATH=/proxy/<port>/` when the upstream proxy preserves the prefix before forwarding. For `jupyter-server-proxy` routes that expose `/user/<name>/proxy/<port>/` in the browser and strip that prefix before the request reaches Heartwood, keep the launcher base path at `/`; the web UI infers the browser proxy base for gateway calls, while the gateway serves root-relative static and session routes internally. When the selected backend is `openhands-bash` or `openhands-agent-server`, the launcher enables the gateway-managed localhost OpenHands child server unless callers explicitly override `HEARTWOOD_AGENT_SERVER_ENABLED`. CI covers both proxy shapes, including a stripped Jupyter-style route that verifies static assets, command submission, replay, and Server-Sent Events through the external notebook URL. The generic image also carries `images/generic/scripts/terra_jupyter_demo_smoke.py`, a Python-only runtime smoke that verifies the packaged static assets and notebook API without Node.js or a repository checkout. The web UI is a presentation adapter over the same session command/event contract as the CLI and notebook bridge, uses WebSocket as the primary stream, falls back to Server-Sent Events, and replays persisted events after reconnect.

The image also carries `README.md`, `ACRONYMS.md`, `docs/`, and `design/` under `/opt/heartwood` so a packaged runtime contains the tutorial notebook and design record needed for an offline demonstration.

## Local Model Strategy

The implemented smoke model is `llama-cpp-stories260k-ci`, recorded in `images/generic/local-runtime/models/stories260k.toml`. It is bundled only in `edge-smoke` and commit-pinned smoke tags.

The first optional bundled coding-model image should target `Qwen/Qwen2.5-Coder-1.5B-Instruct` after a quantized GGUF artifact is selected, pinned, licensed, and tested. Required records before publication are exact source revision, artifact URL, byte size, SHA-256, redistribution posture, quantization, CPU and memory envelope for `linux/amd64` and `linux/arm64`, and whether the image is release-only or scheduled-CI only.

Higher-capability local coding-agent models such as `Qwen/Qwen3-Coder-30B-A3B-Instruct` belong in separate high-resource or GPU profiles, not in required pull-request CI and not in the default runtime image.

## Future Tracking

The Markdown implementation plan remains the source of truth during the repository bootstrap. After the image flavors, provider-route config, and documentation site are stable, move remaining implementation tasks into GitHub Issues and a GitHub Project with fields for phase, platform, risk, owner, status, and required evidence. The design documents should then link to the project board instead of carrying long operational task lists.
