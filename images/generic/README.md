<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Generic Heartwood Image

The generic image family packages the Python workspace, CLI, gateway, notebook bridge, synthetic fixtures, verified skills, Jupyter/widget runtime dependencies, the `llama-cpp-cpu` runtime profile, provider route validation, the pinned OpenHands agent-server package, and the deterministic loopback stub used for fixture checks. It is published as multi-architecture `runtime`, `smoke`, and `providers` flavors for `linux/amd64` and `linux/arm64` wherever the dependency stack supports both platforms.

The `runtime` flavor publishes as `edge` and does not bundle model weights. The `smoke` flavor publishes as `edge-smoke` and bundles only the tiny verified GGUF smoke artifact. The `providers` flavor publishes as `edge-providers` and documents file-based runtime provider secret references without baking credentials into the image.

The Compose service disables runtime network access and runs the offline stack smoke test against an isolated temporary session workspace using the `smoke` build arguments. CI runs this smoke path for `linux/amd64` and `linux/arm64` on native GitHub-hosted Linux runners so the ARM check exercises the ARM image without QEMU runtime emulation. The smoke test starts the default `llama-cpp-cpu` runtime profile on `127.0.0.1`, routes `heartwood run --local-model` through the policy-gated session path, starts the OpenHands agent-server as a gateway-owned localhost child during the agentic run, executes a bounded authenticated `openhands.bash.execute` workspace write through the server API, exports the scrubbed audit log, and generates the synthetic evidence bundle.

The image does not bake an OpenHands API key into `ARG` or `ENV`. The smoke launcher supplies a local-only default session key at runtime and can be overridden by `HEARTWOOD_AGENT_SERVER_API_KEY` when a caller needs a different ephemeral value. Compose also runs the smoke container with an explicit non-root UID/GID, runtime network disabled, a read-only root filesystem, tmpfs write points, dropped Linux capabilities, `no-new-privileges`, and a process limit.

CI runs `docker buildx build --check` against the Dockerfile before the Compose smoke so Dockerfile warnings such as secret-like `ARG` or `ENV` usage fail in pull requests. Main-branch publication uses `docker-bake.hcl`, pulls the current base image tag, builds `runtime`, `smoke`, and `providers` on native `linux/amd64` and `linux/arm64` runners with BuildKit cache, SBOM, and provenance attestations, then merges the architecture images into the public multi-architecture GitHub Container Registry tags.

The local-runtime manifest is checked in at `images/generic/local-runtime/profiles.toml`. The `llama-cpp-cpu` profile is implemented for the smoke path with the pinned ggml-org/llama.cpp `llama-server` CPU binary and `ggml-org/models-moved` `tinyllamas/stories260K.gguf`, pinned by SHA-256 and byte size in `images/generic/local-runtime/models/stories260k.toml`. This model is a tiny load/query artifact with no production or biomedical quality claim and is bundled only in the `smoke` flavor. The `stub-loopback` profile remains available by setting `HEARTWOOD_LOCAL_RUNTIME_PROFILE=stub-loopback`.

Docker can run GPU-accelerated workloads when the host exposes a supported GPU runtime, but the portable generic image stays CPU-first. NVIDIA acceleration is tracked as the deferred `llama-cpp-cuda` profile so GPU-specific base images, wheels, drivers, device reservations, and self-hosted runner tests do not become hidden requirements for the baseline image.
