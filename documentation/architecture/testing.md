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
| OpenHands conformance tests | Public typed events, explicit settings, background control, grouped approval, restart recovery, real Task Tracker execution, usage, and sequential specialists with deterministic `TestLLM` |
| Gateway contract tests | Shared command/event behavior, projection replay, coherent REST, WebSocket, and server-sent-events snapshots, transient ordering, credentials, sessions, and imports |
| Interface tests | Terminal, browser, and notebook rendering of the gateway-owned projection |
| Container smoke tests | Entrypoint, filesystem, architecture, no-secret image layers, and deterministic OpenHands integration |
| No-network smoke tests | Gateway, OpenHands, grouped action, tool, replay, and audit operation without outbound network |
| Capable-model evaluation | Real Heartwood-managed inference, OpenHands-compatible tool proposal, bounded execution, and exact synthetic output |
| Platform-derived CI | Terra Jupyter inheritance, prefixed internal gateway routing, persistence, image media type, CI-only model rejection as an agent profile, and separate real inference |
| Live synthetic validation | Exact published artifact in Terra or Carina without protected data |

GPU image CI verifies the fully hashed CUDA 12.9 environment, exact vLLM and PyTorch versions, compatibility guards, available tool parsers, launcher, and absence of bundled model weights on standard runners.
Each immutable GPU candidate embeds the complete compatibility matrix for its commit.
Qualification profiles select external model weights and runtime arguments against that candidate; they do not produce profile-specific images.
An optional protected self-hosted GPU job runs the same model qualification used on managed platforms when an eligible runner is configured.
Without GPU hardware, CI does not claim successful CUDA initialization or GPU model loading.

The shared coding-agent acceptance test performs direct model inference and then drives the real Heartwood gateway and OpenHands adapter through structured terminal proposals, grouped approval and rejection, synthetic file modification, byte-exact independent verification, proof that the rejected action did not execute, fresh-process replay, and hash-chain-verified audit export.
It emits a machine-readable qualification record containing the exact runtime, model revision, GPU, driver, context, tensor parallelism, server parser, and agent tool mode.
The CPU capable-model job and GPU qualification wrapper use this same acceptance contract instead of maintaining separate agent scenarios.

OpenHands SDK conformance tests use the real conversation persistence layer and deterministic `TestLLM`.
They verify that pending actions and completed tool turns survive restart without repeated model or tool work, grouped approval executes each action once, grouped rejection executes none, active work can be steered and paused, a stale running state fails closed as an unknown outcome, persisted progress appears before completion, Task Tracker updates are translated, and one tool-free research-planning specialist returns to its parent conversation.
The browser reference analysis stops its gateway, replays and mutates the same session through the CLI, restarts the gateway, and verifies that the browser receives the CLI update on one contiguous authoritative sequence.

Native packaging CI uses deterministic dependency-tool substitutes to verify failure paths and reproducibility, then installs the release archive in an empty Ubuntu 24.04 AMD64 container and runs the real CPU inference and browser paths.
Actual Terra and Carina qualification still requires the exact published artifact and synthetic task on those platforms because public CI cannot provision their managed workspaces.
That qualification promotes one precise row in the [GPU compatibility matrix](../reference/gpu-compatibility.md); it does not qualify other drivers, model revisions, precisions, parsers, context sizes, or tensor-parallel layouts.

Pull-request validation, main-branch image publication, capable-model acceptance, and protected GPU qualification are separate workflow entry points.
Every pull request runs the capable-model workflow before merge so changes cannot bypass the real model, OpenHands, approval, replay, and audit contract through an incomplete path filter.
Registry writes, multi-platform manifest assembly, and moving-tag promotion remain main-only; pull requests build the same image stages and validate the promotion scripts without receiving package-write access.
Release creation also requires the repository-managed Python and JavaScript/TypeScript CodeQL analyses for the exact commit.
Compute-intensive container builds run on architecture-matched Blacksmith runners and reuse bounded GitHub Actions BuildKit caches; short policy and documentation checks remain on standard GitHub runners.

## Synthetic Data Rule

Source control, public examples, CI, screenshots, and replay fixtures use synthetic data only.
Protected health information must never enter a test fixture, public log, screenshot, pull request, or model-evaluation artifact.

## Claims

- **Implemented** means code and automated contract tests exist.
- **CI validated** means the behavior ran in the documented automated environment.
- **Live synthetic validated** means the published artifact ran in the named platform with synthetic data.
- **Institution approved** requires separate institutional evidence and is never inferred from the previous levels.

Release documentation states the supported contract rather than preserving individual validation transcripts.
