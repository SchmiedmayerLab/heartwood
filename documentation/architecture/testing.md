<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Testing and Evidence

Heartwood separates deterministic contract tests from resource-qualified model evaluations and live platform validation.
No single layer establishes every property of a deployment.

## Test Layers

| Layer | Establishes |
|---|---|
| Unit and schema tests | Validation, state boundaries, policy, diagnostics, model planning, and serialization |
| Gateway contract tests | Shared command/event behavior, REST and stream parity, credentials, sessions, and imports |
| Interface tests | Terminal, browser, and notebook projections over the same state |
| Container smoke tests | Entrypoint, filesystem, architecture, no-secret image layers, and deterministic OpenHands integration |
| No-network smoke tests | Gateway, OpenHands, grouped action, tool, replay, and audit operation without outbound network |
| Capable-model evaluation | Real Heartwood-managed inference, native tool proposal, bounded execution, and exact synthetic output |
| Platform-derived CI | Terra Jupyter inheritance, prefixed internal gateway routing, persistence, image media type, CI-only model rejection as an agent profile, and separate real inference |
| Live synthetic validation | Exact published artifact in Terra or Carina without protected data |

GPU image CI verifies the locked CUDA-enabled runtime, compatibility guards, launcher, and absence of bundled model weights on standard runners. It does not establish successful GPU initialization or model inference without GPU hardware. Native packaging CI uses deterministic dependency-tool substitutes to verify failure paths and reproducibility, then installs the release archive in an empty Ubuntu 24.04 AMD64 container and runs the real CPU inference and browser paths. Actual GPU model load, inference, and Carina dependency resolution require live synthetic validation in the target environment.
Live platform validation supplements the automated release evidence and should be recorded before a deployment is promoted for operational use; it is not an automated release gate because CI cannot provision institutional workspaces.

## Synthetic Data Rule

Source control, public examples, CI, screenshots, and replay fixtures use synthetic data only.
Protected health information must never enter a test fixture, public log, screenshot, pull request, or model-evaluation artifact.

## Claims

- **Implemented** means code and automated contract tests exist.
- **CI validated** means the behavior ran in the documented automated environment.
- **Live synthetic validated** means the published artifact ran in the named platform with synthetic data.
- **Institution approved** requires separate institutional evidence and is never inferred from the previous levels.

Release documentation states the supported contract rather than preserving individual validation transcripts.
