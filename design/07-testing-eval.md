<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# 07 — Testing And Evaluation

Verification runs only on synthetic data in public development and continuous integration. Merge and release gates must not add latency or hidden model calls to a researcher’s live session.

## Implemented Layers

1. **Pure-code tests.** Detector evidence, endpoint normalization, deployment policy, credentials, action settings, upstream analyzer composition, audit chaining and scrubbing, adapters, model settings, artifact integrity, and Skill metadata are tested without a model call.
2. **Deterministic replay.** Synthetic command and event fixtures verify session reconstruction, approval state, audit export, and bundled Skill outputs. Prompt or response content from controlled-data sessions must never become a fixture.
3. **Skill tests.** Every bundled Skill has metadata validation, root-confinement checks, deterministic script tests, and synthetic failure cases. The `verified` tier means repository verification; it is not cryptographic, clinical, statistical, security, or institutional certification.
4. **Interface contracts.** CLI, notebook, REST, WebSocket, Server-Sent Events, and web view models consume the same session events. Vitest, Testing Library, Playwright, packaged-gateway, and Jupyter-proxy tests cover the researcher-facing paths.
5. **Runtime integration.** Native AMD64 and ARM64 jobs build the no-weight runtime. With runtime networking disabled, a deterministic OpenAI-compatible fixture drives a real OpenHands conversation, terminal proposal, allow, reject, low-risk automatic execution, medium-risk confirmation, audit export, native Skill load, REST path, and notebook proxy check. A separate job downloads and verifies a tiny CI-only artifact on the runner, mounts it read-only, and performs a real llama.cpp load and completion with runtime networking disabled.

## Capability Tiers

Model profiles carry `autonomous`, `supervised`, or `experimental` as deployment-reviewed capability metadata. `PolicyProfile.allowed_capability_tiers` determines which labels may submit a turn and defaults to `supervised`. Capability tiers do not select action behavior. `PolicyProfile.allowed_action_confirmation_modes` independently defaults to `always-confirm`; a deployment must explicitly permit `confirm-risky` before a researcher can enable low-risk automatic execution.

No model has a repository-backed production capability claim yet. Assigning a tier beyond supervised requires pinned model and harness identities, reproducible coding and biomedical benchmarks, documented hardware, failure analysis, and independent review. Configuration alone is not benchmark evidence.

## Required Evaluation Evidence

- **OpenHands compatibility.** Every SDK or tools upgrade must pass real event translation, action confirmation, rejection, automatic low-risk execution, persistence and resume, native Skill loading, terminal and file execution, and offline container integration.
- **Model capability.** A capability tier requires a pinned model and quantization, runtime and hardware identity, coding and biomedical benchmark results, policy-adherence results, and documented failure analysis. Endpoint validation or artifact integrity is not capability evidence.
- **Action-risk behavior.** Managed use of `confirm-risky` requires representative benign, ambiguous, destructive, encoded, prompt-injected, and network-capable action cases with a documented acceptance threshold. The evaluation exercises OpenHands analyzers; it does not introduce a second runtime classifier.
- **Credential exposure.** Environment-referenced model keys must be absent from OpenHands terminal subprocess values. Mounted credential files and managed identities require deployment tests proving the intended least-privilege and tool-access boundary; a shared interactive user is not evidence of isolation.
- **Data-source detection.** A real adapter requires positive, negative, partial-schema, permission-denied, and ambiguous-data cases. It must report no match rather than reuse a synthetic fingerprint when evidence is absent.
- **Biomedical Skills.** Controlled-data readiness requires deterministic tests, malformed-input and boundary cases, output-schema checks, independent clinical or statistical review, and deployment-specific export-policy verification.
- **Platform support.** CI evidence verifies software contracts; live-platform evidence verifies an immutable published artifact in the actual control plane. Institution approval remains a separate deployment decision.
- **Model-graded evaluation.** A model judge may be used only when its identity, prompts, retention, reproducibility, failure modes, and data boundary are approved and recorded.

The [Delivery Roadmap](09-implementation-plan.md) orders implementation of these evidence categories and defines their release gates.

## Continuous Integration Sequence

Repository validation, Python lint and types, unit and replay tests, web checks, native no-weight image builds, OpenHands offline integration, mounted llama.cpp integration, Terra Jupyter contracts, and publication manifest checks form the current sequence. BuildKit produces software bill of materials and provenance attestations for the generic published image. Cryptographic release and Skill signing remain future release-hardening work and must not be claimed until verification is automated.
