<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Generic Heartwood Image

The generic image family packages the Python workspace, CLI, gateway, notebook bridge, built researcher web UI, synthetic fixtures, verified skills, Jupyter/widget runtime dependencies, the `llama-cpp-cpu` runtime profile, provider route validation and invocation support, the pinned OpenHands agent-server package, and the deterministic loopback stub used for fixture checks. It is published as multi-architecture `runtime`, `smoke`, and `providers` flavors for `linux/amd64` and `linux/arm64` wherever the dependency stack supports both platforms.

The `runtime` flavor publishes as `edge` and model-explicit alias `edge-coder-7b`; it bundles Qwen2.5-Coder-7B-Instruct Q4_K_M so the default Docker image is useful out of the box. The `smoke` flavor publishes as `edge-smoke` and bundles only the tiny verified GGUF smoke artifact for CI. The `providers` flavor publishes as `edge-providers` and documents file-based runtime provider secret references without baking credentials or model weights into the image.

The final image carries `README.md`, `ACRONYMS.md`, `docs/`, and `design/` under `/opt/heartwood`, including the Terra-style Jupyter demo notebook. The generic image remains the portable runtime baseline; Terra users should select the separate `edge-terra`, `edge-terra-coder-7b`, or `edge-terra-smoke` platform image after the main-branch publish workflow creates those tags.

The Compose service disables runtime network access and runs the offline stack smoke test against an isolated temporary session workspace using the `smoke` build arguments. CI runs this smoke path for `linux/amd64` and `linux/arm64` on native GitHub-hosted Linux runners so the ARM check exercises the ARM image without QEMU runtime emulation. The smoke test starts the default `llama-cpp-cpu` runtime profile on `127.0.0.1`, routes `heartwood run --local-model` through the policy-gated session path, starts the OpenHands agent-server as a gateway-owned localhost child during the agentic run, executes a bounded authenticated `openhands.bash.execute` workspace write through the server API, exports the scrubbed audit log, and generates the synthetic evidence bundle.

The image does not bake an OpenHands API key into `ARG` or `ENV`. The smoke launcher supplies a local-only default session key at runtime and can be overridden by `HEARTWOOD_AGENT_SERVER_API_KEY` when a caller needs a different ephemeral value. Compose also runs the smoke container with an explicit non-root UID/GID, runtime network disabled, a read-only root filesystem, tmpfs write points, dropped Linux capabilities, `no-new-privileges`, and a process limit.

CI runs `docker buildx build --check` against the Dockerfile before the Compose smoke so Dockerfile warnings such as secret-like `ARG` or `ENV` usage fail in pull requests. Main-branch publication uses `docker-bake.hcl`, pulls the current base image tag, builds `runtime`, `smoke`, and `providers` on native `linux/amd64` and `linux/arm64` runners with BuildKit cache, SBOM, and provenance attestations, then merges the architecture images into the public multi-architecture GitHub Container Registry tags. Pull-request CI builds only the tiny smoke path; the multi-gigabyte 7B runtime artifact is resolved during main-branch image publication.

The local-runtime manifest is checked in at `images/generic/local-runtime/profiles.toml`. The `llama-cpp-cpu` profile is implemented with the pinned ggml-org/llama.cpp `llama-server` CPU binary. The default runtime artifact is `Qwen2.5-Coder-7B-Instruct-Q4_K_M.gguf`, pinned by SHA-256 and byte size in `images/generic/local-runtime/models/qwen25-coder-7b-q4_k_m.toml`; this is a demo-quality local coding model, not a biomedical or production claim. The smoke artifact is `ggml-org/models-moved` `tinyllamas/stories260K.gguf`, pinned in `images/generic/local-runtime/models/stories260k.toml`, and is bundled only in smoke flavors. The `stub-loopback` profile remains available by setting `HEARTWOOD_LOCAL_RUNTIME_PROFILE=stub-loopback`.

Docker can run GPU-accelerated workloads when the host exposes a supported GPU runtime, but the portable generic image stays CPU-first. NVIDIA acceleration is tracked as the deferred `llama-cpp-cuda` profile so GPU-specific base images, wheels, drivers, device reservations, and self-hosted runner tests do not become hidden requirements for the baseline image.

`images/generic/scripts/start_demo_stack.sh` is the default local interactive demo launcher for the `edge` image. It starts the bundled local model runtime, seeds the synthetic model-call approval for the default web UI session, enables a bounded synthetic response preview, sets the local model response budget to 768 tokens, starts the gateway-managed localhost OpenHands child server, and serves the packaged web UI on `0.0.0.0:8767`; set `HEARTWOOD_DEMO_WEB_HOST` only when the demo container must bind a different internal host. `images/generic/scripts/start_web_ui.sh` remains the lower-level gateway/UI launcher; keep its default `127.0.0.1` bind behind Terra or another notebook proxy, and set `HEARTWOOD_WEB_HOST=0.0.0.0` only for direct local Docker port publishing. Set `HEARTWOOD_WEB_BASE_PATH=/proxy/<port>/` when serving behind `jupyter-server-proxy`; otherwise the default root path is suitable. When the image backend is `openhands-bash` or `openhands-agent-server`, the web UI launcher enables the gateway-managed localhost OpenHands child server unless `HEARTWOOD_AGENT_SERVER_ENABLED` is set explicitly.
