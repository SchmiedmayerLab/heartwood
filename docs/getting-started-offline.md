<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Getting Started With The Offline Stack

This guide runs the Phase 0 generic Heartwood smoke stack without public runtime network access. It demonstrates the intended shipping shape: one multi-architecture image family with a default runtime image, a smoke image with a tiny verified model artifact, a provider-route image for platform embedding, the CLI, gateway-backed session flow, notebook package, synthetic fixtures, verified skills, policy-gated local model path, a pinned OpenHands agent-server package, audit export, and a synthetic evidence bundle.

The default local-runtime profile in this guide is `llama-cpp-cpu`. The smoke image starts the pinned llama.cpp `llama-server` binary on `127.0.0.1`, loads a tiny verified GGUF artifact bundled into that smoke flavor, runs one approved local model call through the gateway policy path, starts the gateway-managed OpenHands agent-server, and writes a bounded synthetic workspace artifact through authenticated OpenHands `/api/bash/execute_bash_command` execution. The model artifact exists to prove offline load/query behavior and has no production or biomedical quality claim. The deterministic `stub-loopback` profile remains available for fixture checks by setting `HEARTWOOD_LOCAL_RUNTIME_PROFILE=stub-loopback`.

## Local Inference Scope

The smoke test proves offline load, query, policy gating, event flow, tool execution, audit export, and evidence-bundle generation. The checked-in model manifest pins `ggml-org/models-moved` `tinyllamas/stories260K.gguf` by URL, revision, byte size, and SHA-256. The smoke image downloads and verifies that artifact during the image build, then runs it without public network access at runtime. The default `edge` runtime image does not bundle model weights.

## Image Flavors

| Flavor | Tag | Use |
|---|---|---|
| Runtime | `edge` | Default platform-ready image with no bundled model weights. |
| Smoke | `edge-smoke` | Offline stack CI and tutorial image with the tiny verified GGUF artifact. |
| Providers | `edge-providers` | Provider-route image with file-based secret references and no provider secrets. |

Commit-pinned tags use `sha-<git-sha>`, `sha-<git-sha>-smoke`, and `sha-<git-sha>-providers`. Stable release tags will use `v<semver>` after the first release; `latest` is intentionally not used before then.

The bundled artifact is intentionally tiny so pull-request CI can exercise the same runtime on `linux/amd64` and `linux/arm64`. Larger tutorial models can be added as explicit manifests after their source, license posture, redistribution allowance, checksum, and resource envelope are recorded.

## Architecture And Acceleration Scope

The image family is expected to publish and smoke-test `linux/amd64` and `linux/arm64` variants. The current technology stack is Python-first and uses portable container tooling, so both architectures are reasonable baseline targets. GitHub-hosted CI uses standard `ubuntu-latest` runners with QEMU for the `linux/arm64` smoke path until native hosted `arm64` runners are stable enough for this repository's required checks.

Docker can expose GPUs to containers, but GPU support is a host capability, not something a portable CPU image can guarantee. The baseline real runtime therefore remains `llama-cpp-cpu`; NVIDIA acceleration is tracked separately as `llama-cpp-cuda`, requiring an explicit CUDA-enabled runtime, Docker GPU device exposure, and self-hosted GPU CI or scheduled platform checks. The CPU profile must keep working without a GPU.

## Agent-Server Scope

The generic image installs `openhands-agent-server==1.33.0`, `openhands-tools==1.33.0`, and `libtmux==0.61.0` for Python 3.12 and includes `images/generic/scripts/start_agent_server.sh`, which binds only to loopback, configures a local session key, disables VSCode/VNC/tool-preload services for the smoke path, and stores OpenHands state under a temporary workspace. The offline smoke run enables `HEARTWOOD_AGENT_SERVER_ENABLED=1` for the agentic CLI turn so the session gateway starts and stops the OpenHands process as a managed localhost child. `HEARTWOOD_AGENT_BACKEND=openhands-bash` then lists registered OpenHands tools and executes a bounded bash command through authenticated OpenHands `/api` routes after approval.

The local session key is not baked into the Dockerfile as an `ARG` or `ENV`. The smoke scripts provide a local-only runtime default and allow callers to override it with `HEARTWOOD_AGENT_SERVER_API_KEY`; production deployments should provide their own runtime secret through the platform secret mechanism rather than an image layer. Pull-request CI runs Docker's Buildx Dockerfile checks before the Compose smoke so secret-like `ARG` or `ENV` warnings are treated as build hygiene failures.

## Provider Routes

Provider route examples live in `images/generic/providers/provider-routes.example.toml`. Routes can name OpenAI-compatible local endpoints, OpenAI, Azure OpenAI, Anthropic, Vertex AI, Bedrock, and other future provider adapters, but credentials are never stored inline. Secret-bearing routes use `auth = "secret-file"` and point to a runtime mount such as `/run/secrets/openai_api_key`; cloud identity routes use `auth = "managed-identity"`. The active policy profile must still explicitly allowlist the selected endpoint before invocation.

## Run From The Published Image

After the main-branch image is published, run the offline smoke workflow with Docker only:

```bash
docker pull ghcr.io/schmiedmayerlab/heartwood:edge-smoke
docker run --rm --network none ghcr.io/schmiedmayerlab/heartwood:edge-smoke bash images/generic/scripts/offline_stack_smoke.sh
```

The command starts the `llama-cpp-cpu` runtime profile, runs detection, approves the synthetic model call, invokes `heartwood run --local-model`, starts the gateway-managed OpenHands process for the agentic run, executes `openhands.bash.execute`, writes `agent-artifacts/synthetic-workspace-summary.md`, exports a scrubbed audit JSONL file, and writes the synthetic evidence bundle under `/tmp/heartwood-reviewer-packet`.

## Run From A Checkout

From a repository checkout, run the same smoke test through Docker Compose:

```bash
docker compose -f images/generic/compose.yaml run --rm heartwood
```

Compose builds the local image, pulls the current base image tag, disables runtime network access with `network_mode: none`, pins the runtime user to the image's non-root UID/GID, and runs the same offline smoke script used by CI.

## What The Smoke Test Proves

- The CLI can drive the gateway-backed session contract inside the image.
- The generic policy allows only configured model endpoints and includes the loopback chat-completions endpoint.
- The llama-cpp model call happens over `127.0.0.1` while external network is disabled.
- The model response content is not persisted into session events or audit exports; only response metadata is recorded.
- The OpenHands-backed backend emits tool proposal, confirmation, and execution events after the model call, calls authenticated OpenHands `/api` routes, and writes a bounded synthetic artifact through the agent-server bash service.
- The audit export and reviewer packet can be produced from the same offline session.

## What It Does Not Prove Yet

- It does not validate controlled data.
- It does not yet validate optional GPU acceleration; that belongs to a separate CUDA profile and a GPU-capable runner.
- It does not yet prove autonomous coding quality from a larger local tutorial model; the bundled tiny model is only a load/query artifact, while the tool-execution smoke is intentionally bounded and deterministic after approval.
- It does not run the researcher web UI or platform proxy routes.
- It does not publish the static documentation site; that belongs with the next documentation-site pass.
