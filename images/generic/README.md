<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Generic Heartwood Image

The generic image packages the Python workspace, CLI, gateway, notebook bridge, synthetic fixtures, verified skills, Jupyter/widget runtime dependencies, and a tiny loopback model stub for Phase 0 smoke tests. It does not bundle or launch a production OpenHands agent-server; current agent-server coverage remains at the gateway-owned localhost boundary and fake OpenHands-style event translation.

The Compose service disables runtime network access and runs the offline stack smoke test against an isolated temporary session workspace. The smoke test starts the local model stub on `127.0.0.1`, routes `heartwood run --local-model` through the policy-gated session path, exports the scrubbed audit log, and generates a reviewer packet.

The image does not contain an LLM inference runtime or model weights. The loopback stub is an integration test endpoint for the local-model control path; real local inference remains a separate profile that must choose a runtime, model artifact source, license posture, and offline verification strategy.
