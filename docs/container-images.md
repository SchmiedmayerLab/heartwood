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
| `sha-<git-sha>` | Exact runtime image for one commit. |
| `sha-<git-sha>-smoke` | Exact smoke image for one commit. |
| `sha-<git-sha>-providers` | Exact provider-route image for one commit. |
| `v<semver>` | Future stable runtime release. |
| `v<semver>-<flavor>` | Future stable release for a non-default flavor. |

Do not use `latest` until the first stable release exists. Do not use branch names such as `main` or informal tags such as `dev-main` for user-facing image references.

The publish workflow builds `linux/amd64` and `linux/arm64` on native GitHub-hosted runners, pushes architecture helper tags such as `edge-amd64` and `edge-arm64`, then creates the public multi-architecture tags listed above with `docker buildx imagetools create`. Treat architecture helper tags as publication internals for debugging and manifest assembly, not stable user-facing references.

Registry maintenance must protect public moving tags, commit-SHA tags retained by policy, future semver tags, SBOM/provenance artifacts, and any manifest referenced by a public multi-architecture index. Cleanup automation should begin with dry-run reports, delete only stale helper tags or unreferenced versions outside the retention window, and record the GHCR permissions required to perform deletions. The next documentation and governance pass must add post-publish registry inspection that proves each public tag resolves to both `linux/amd64` and `linux/arm64`.

## Current Flavors

| Flavor | Bake Target | Moving Tag | Bundled Model Artifact | Intended Use |
|---|---|---|---|---|
| Runtime | `runtime` | `edge` | No | Default platform-ready image for CLI, gateway, notebook bridge, built researcher web UI, OpenHands launcher, local inference runtime dependencies, provider route config/invocation support, and externally mounted model artifacts. |
| Smoke | `smoke` | `edge-smoke` | Yes, tiny checked GGUF | Pull-request CI, main-branch CI, and Docker-only tutorials proving offline load/query, gateway policy, OpenHands bash execution, audit export, and reviewer packet generation. |
| Providers | `providers` | `edge-providers` | No | Platform embedding where model access is supplied by in-boundary provider endpoints and credentials are mounted at runtime. |

The smoke flavor is intentionally not the default image because the default image should stay platform-ready and should not imply that bundled weights are required. The smoke model exists to prove the stack runs air-gapped; it is not a coding-quality, biomedical, or production model.

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

Start the gateway and UI inside the image with:

```bash
bash images/generic/scripts/start_web_ui.sh
```

The launcher reads `HEARTWOOD_WORKSPACE`, `HEARTWOOD_WEB_HOST`, `HEARTWOOD_WEB_PORT`, `HEARTWOOD_WEB_ROOT`, and `HEARTWOOD_WEB_BASE_PATH`. Use `HEARTWOOD_WEB_BASE_PATH=/proxy/<port>/` when the upstream proxy preserves the prefix before forwarding. For `jupyter-server-proxy` routes that expose `/user/<name>/proxy/<port>/` in the browser and strip that prefix before the request reaches Heartwood, keep the launcher base path at `/`; the web UI infers the browser proxy base for gateway calls, while the gateway serves root-relative static and session routes internally. CI covers both proxy shapes, including a stripped Jupyter-style route that verifies static assets, command submission, replay, and Server-Sent Events through the external notebook URL. The web UI is a presentation adapter over the same session command/event contract as the CLI and notebook bridge, uses WebSocket as the primary stream, falls back to Server-Sent Events, and replays persisted events after reconnect.

## Local Model Strategy

The implemented smoke model is `llama-cpp-stories260k-ci`, recorded in `images/generic/local-runtime/models/stories260k.toml`. It is bundled only in `edge-smoke` and commit-pinned smoke tags.

The first optional bundled coding-model image should target `Qwen/Qwen2.5-Coder-1.5B-Instruct` after a quantized GGUF artifact is selected, pinned, licensed, and tested. Required records before publication are exact source revision, artifact URL, byte size, SHA-256, redistribution posture, quantization, CPU and memory envelope for `linux/amd64` and `linux/arm64`, and whether the image is release-only or scheduled-CI only.

Higher-capability local coding-agent models such as `Qwen/Qwen3-Coder-30B-A3B-Instruct` belong in separate high-resource or GPU profiles, not in required pull-request CI and not in the default runtime image.

## Future Tracking

The Markdown implementation plan remains the source of truth during the repository bootstrap. After the image flavors, provider-route config, and documentation site are stable, move remaining implementation tasks into GitHub Issues and a GitHub Project with fields for phase, platform, risk, owner, status, and required evidence. The design documents should then link to the project board instead of carrying long operational task lists.
