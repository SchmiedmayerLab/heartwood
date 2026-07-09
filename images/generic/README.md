<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Generic Heartwood Image

The generic image packages the Python workspace, CLI, gateway, notebook bridge, synthetic fixtures, verified skills, Jupyter/widget runtime dependencies, local-runtime profile metadata, and a deterministic loopback stub for smoke tests. It does not yet bundle or launch a production OpenHands agent-server; current agent-server coverage remains at the gateway-owned localhost boundary and fake OpenHands-style event translation.

The Compose service disables runtime network access and runs the offline stack smoke test against an isolated temporary session workspace. The smoke test starts the `stub-loopback` runtime profile on `127.0.0.1`, routes `heartwood run --local-model` through the policy-gated session path, exports the scrubbed audit log, and generates the synthetic evidence bundle.

The local-runtime manifest is checked in at `images/generic/local-runtime/profiles.toml`. The `stub-loopback` profile is implemented and has no model-quality claim. The selected real profile is `llama-cpp-cpu`; it defines the CPU-only runtime, localhost serving API, GGUF artifact policy, checksum and license requirements, startup/shutdown behavior, and CI expectations that must be satisfied before the image can claim real local inference support.

The image does not yet contain the `llama-cpp-cpu` runtime dependency or model weights. The loopback stub is an integration test endpoint for the local-model control path; real local inference remains gated on the profile contract being implemented with a pinned runtime dependency, model artifact provenance, license review, checksum verification, and an offline load/query smoke test.
