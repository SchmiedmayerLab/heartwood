<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Generic Heartwood Image

The generic image packages the Python workspace, CLI, gateway, notebook bridge, synthetic fixtures, verified skills, Jupyter/widget runtime dependencies, local-runtime profile metadata, and a deterministic loopback stub for smoke tests. It is published as a multi-architecture image for `linux/amd64` and `linux/arm64` wherever the dependency stack supports both platforms. It does not yet bundle or launch a production OpenHands agent-server; current agent-server coverage remains at the gateway-owned localhost boundary and fake OpenHands-style event translation.

The Compose service disables runtime network access and runs the offline stack smoke test against an isolated temporary session workspace. CI runs this smoke path for `linux/amd64` and `linux/arm64`; the `arm64` job uses QEMU emulation on the standard GitHub-hosted runner until native hosted `arm64` coverage is stable enough for this workflow. The smoke test starts the `stub-loopback` runtime profile on `127.0.0.1`, routes `heartwood run --local-model` through the policy-gated session path, exports the scrubbed audit log, and generates the synthetic evidence bundle.

The local-runtime manifest is checked in at `images/generic/local-runtime/profiles.toml`. The `stub-loopback` profile is implemented and has no model-quality claim. The selected real profile is `llama-cpp-cpu`; it defines the CPU runtime, localhost serving API, `linux/amd64` and `linux/arm64` support target, GGUF artifact policy, checksum and license requirements, startup/shutdown behavior, and CI expectations that must be satisfied before the image can claim real local inference support.

The image does not yet contain the `llama-cpp-cpu` runtime dependency or model weights. The loopback stub is an integration test endpoint for the local-model control path; real local inference remains gated on the profile contract being implemented with a pinned runtime dependency, model artifact provenance, license review, checksum verification, and an offline load/query smoke test.

Docker can run GPU-accelerated workloads when the host exposes a supported GPU runtime, but the portable generic image stays CPU-first. NVIDIA acceleration is tracked as the deferred `llama-cpp-cuda` profile so GPU-specific base images, wheels, drivers, device reservations, and self-hosted runner tests do not become hidden requirements for the baseline image.
