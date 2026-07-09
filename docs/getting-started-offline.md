<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Getting Started With The Offline Stack

This guide runs the Phase 0 generic Heartwood smoke stack without public runtime network access. It demonstrates the intended shipping shape: one multi-architecture image family with a default runtime image, a smoke image with a tiny verified model artifact, a provider-route image for platform embedding, the CLI, gateway-backed session flow, notebook package, researcher web UI, synthetic fixtures, verified skills, policy-gated local model path, a pinned OpenHands agent-server package, audit export, and a synthetic evidence bundle.

The default local-runtime profile in this guide is `llama-cpp-cpu`. The smoke image starts the pinned llama.cpp `llama-server` binary on `127.0.0.1`, loads a tiny verified GGUF artifact bundled into that smoke flavor, runs one approved local model call through the gateway policy path, starts the gateway-managed OpenHands agent-server, and writes a bounded synthetic workspace artifact through authenticated OpenHands `/api/bash/execute_bash_command` execution. The model artifact exists to prove offline load/query behavior and has no production or biomedical quality claim. The deterministic `stub-loopback` profile remains available for fixture checks by setting `HEARTWOOD_LOCAL_RUNTIME_PROFILE=stub-loopback`.

## Local Inference Scope

The smoke test proves offline load, query, policy gating, event flow, tool execution, audit export, and evidence-bundle generation. The checked-in model manifest pins `ggml-org/models-moved` `tinyllamas/stories260K.gguf` by URL, revision, byte size, and SHA-256. The smoke image downloads and verifies that artifact during the image build, then runs it without public network access at runtime. The default `edge` runtime image does not bundle model weights.

## Image Flavors

| Flavor | Tag | Use |
|---|---|---|
| Runtime | `edge` | Default platform-ready image with no bundled model weights. |
| Smoke | `edge-smoke` | Offline stack CI and tutorial image with the tiny verified GGUF artifact. |
| Providers | `edge-providers` | Provider-route image with file-based secret references and no provider secrets. |
| Terra Runtime | `edge-terra` | Terra-derived notebook image with no bundled model weights. |
| Terra Smoke | `edge-terra-smoke` | Terra-derived notebook image with the tiny verified GGUF artifact for synthetic Terra demos and CI smoke. |

The `edge-terra-smoke-ci` tag is a local CI tag only. It is built from a lightweight Terra-compatible base to test the platform Dockerfile, notebook assumptions, packaged web UI, local model path, and offline stack without pulling the real Terra base in every pull request.

Commit-pinned tags use `sha-<git-sha>`, `sha-<git-sha>-smoke`, and `sha-<git-sha>-providers`. Stable release tags will use `v<semver>` after the first release; `latest` is intentionally not used before then.

The bundled artifact is intentionally tiny so pull-request CI can exercise the same runtime on `linux/amd64` and `linux/arm64`. Larger tutorial models can be added as explicit manifests after their source, license posture, redistribution allowance, checksum, and resource envelope are recorded.

## Architecture And Acceleration Scope

The image family is expected to publish and smoke-test `linux/amd64` and `linux/arm64` variants. The current technology stack is Python-first with a built static web UI and portable container tooling, so both architectures are reasonable baseline targets. GitHub-hosted CI uses native `ubuntu-24.04` and `ubuntu-24.04-arm` runners for the smoke path so the ARM image is not validated through QEMU runtime emulation.

Docker can expose GPUs to containers, but GPU support is a host capability, not something a portable CPU image can guarantee. The baseline real runtime therefore remains `llama-cpp-cpu`; NVIDIA acceleration is tracked separately as `llama-cpp-cuda`, requiring an explicit CUDA-enabled runtime, Docker GPU device exposure, and self-hosted GPU CI or scheduled platform checks. The CPU profile must keep working without a GPU.

## Agent-Server Scope

The generic image installs `openhands-agent-server==1.34.0`, `openhands-tools==1.34.0`, and `libtmux==0.61.0` for Python 3.12 and includes `images/generic/scripts/start_agent_server.sh`, which binds only to loopback, configures a local session key, disables VSCode/VNC/tool-preload services for the smoke path, and stores OpenHands state under a temporary workspace. The offline smoke run enables `HEARTWOOD_AGENT_SERVER_ENABLED=1` for the agentic CLI turn so the session gateway starts and stops the OpenHands process as a managed localhost child. `HEARTWOOD_AGENT_BACKEND=openhands-bash` then lists registered OpenHands tools and executes a bounded bash command through authenticated OpenHands `/api` routes after approval.

The local session key is not baked into the Dockerfile as an `ARG` or `ENV`. The smoke scripts provide a local-only runtime default and allow callers to override it with `HEARTWOOD_AGENT_SERVER_API_KEY`; production deployments should provide their own runtime secret through the platform secret mechanism rather than an image layer. Pull-request CI runs Docker's Buildx Dockerfile checks before the Compose smoke so secret-like `ARG` or `ENV` warnings are treated as build hygiene failures.

## Provider Routes

Provider route examples live in `images/generic/providers/provider-routes.example.toml`. Routes can name OpenAI-compatible local endpoints, OpenAI, Azure OpenAI, Anthropic, Vertex AI, Bedrock, and other future provider adapters, but credentials are never stored inline. Secret-bearing routes use `auth = "secret-file"` and point to a runtime mount such as `/run/secrets/openai_api_key`; cloud identity routes use `auth = "managed-identity"`. The active policy profile must still explicitly allowlist the selected endpoint before invocation.

## Run From The Published Image

After the main-branch image is published, run the offline smoke workflow with Docker only:

```bash
docker pull ghcr.io/schmiedmayerlab/heartwood:edge-smoke
docker run --rm --network none ghcr.io/schmiedmayerlab/heartwood:edge-smoke bash images/generic/scripts/offline_stack_smoke.sh
```

The command starts the `llama-cpp-cpu` runtime profile, runs detection, approves the synthetic model call, invokes `heartwood run --local-model`, starts the gateway-managed OpenHands process for the agentic run, executes `openhands.bash.execute`, writes `agent-artifacts/synthetic-workspace-summary.md`, exports a scrubbed audit JSONL file, writes the synthetic evidence bundle under `/tmp/heartwood-reviewer-packet`, then runs the Python-only Terra-style Jupyter demo smoke against the packaged web UI and notebook API.

The packaged image includes the project README, acronym glossary, `docs/`, and `design/` under `/opt/heartwood`, including `/opt/heartwood/docs/terra-jupyter-demo.ipynb`. This lets a runtime image carry the tutorial material needed for a local or platform notebook demonstration without a repository checkout.

To open the packaged researcher UI with the local model, OpenHands backend, seeded synthetic approval, and bounded demo response preview, publish the gateway port and start the full demo launcher:

```bash
docker pull ghcr.io/schmiedmayerlab/heartwood:edge-smoke
docker run --rm -p 8767:8767 ghcr.io/schmiedmayerlab/heartwood:edge-smoke bash images/generic/scripts/start_demo_stack.sh
```

Open `http://127.0.0.1:8767/`, click **Run Local Model**, then inspect the Conversation, Local Model, Policy, Approvals, Activity, and Exports panels. The Conversation panel shows the prompt submitted in the current browser session, the bounded synthetic model response preview, the agent message, and compact event-derived trace summaries for policy and tool steps; it does not expose hidden model chain-of-thought or persist prompt text into replay logs by default. The demo stack starts the bundled llama.cpp smoke model on `127.0.0.1:8765`, starts the gateway-managed OpenHands child server, pre-approves the synthetic model-call decision for `session-local`, and enables `HEARTWOOD_DEMO_RESPONSE_PREVIEW=1` so the UI can show a bounded synthetic response preview. Set `HEARTWOOD_DEMO_SEED_APPROVALS=0` to exercise the approval gate manually: the first run records the model-call decision, the Approvals panel exposes the approval action, and the second run invokes the local model after approval. The UI renders the same session events as the CLI and notebook bridge, uses WebSocket streaming with Server-Sent Events fallback, and replays the persisted event log after reconnects. Heartwood supports both common notebook proxy shapes: preserved-prefix routes such as `/proxy/8767/`, where `HEARTWOOD_WEB_BASE_PATH=/proxy/8767/` is passed to the launcher, and stripped `jupyter-server-proxy` routes such as `/user/<name>/proxy/8767/`, where the gateway serves `/` and the proxy strips the browser prefix before forwarding. CI smoke tests both the preserved-prefix gateway route and the stripped Jupyter-style route used by Terra-like notebook environments. See [Terra-Style Jupyter Demo](terra-jupyter-demo.md) for the synthetic workspace walkthrough.

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
- Model response content is not persisted by default. The interactive Docker demo enables a bounded synthetic response preview with `HEARTWOOD_DEMO_RESPONSE_PREVIEW=1`; audit exports remain scrubbed.
- The OpenHands-backed backend emits tool proposal, confirmation, and execution events after the model call, calls authenticated OpenHands `/api` routes, and writes a bounded synthetic artifact through the agent-server bash service.
- The audit export and reviewer packet can be produced from the same offline session.
- The packaged web UI can be served by the gateway from self-contained assets without a CDN, and the same session event stream can be surfaced through WebSocket or Server-Sent Events under local or Jupyter-style proxy routes.
- The Jupyter-style smoke path serves the UI through an external `/user/synthetic/proxy/<port>/` route, strips that prefix before forwarding to the gateway, and verifies static assets, command submission, event replay, and Server-Sent Events through the external notebook URL shape.
- The packaged runtime image can execute the same Jupyter-style smoke without Node.js or a repository checkout; the smoke uses only Python, the installed `heartwood` executable, packaged static assets, and the notebook bridge.

## What It Does Not Prove Yet

- It does not validate controlled data.
- It does not yet validate optional GPU acceleration; that belongs to a separate CUDA profile and a GPU-capable runner.
- It does not yet prove autonomous coding quality from a larger local tutorial model; the bundled tiny model is only a load/query artifact, while the tool-execution smoke is intentionally bounded and deterministic after approval.
- It does not validate Terra, Seven Bridges, or DNAnexus controlled-platform identity binding; Terra platform-image CI is local and synthetic until a real Terra workspace smoke records platform launch, proxy behavior, and identity evidence.
- It does not publish the static documentation site; that belongs with the next documentation-site pass.
