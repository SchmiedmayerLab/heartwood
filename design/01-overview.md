<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Product Scope

This document defines what Heartwood is responsible for, who it serves, and which claims remain outside the product boundary. Operational instructions are maintained in [Get Started with Heartwood](../docs/getting-started.md), while implementation details begin in [System Architecture](03-architecture.md).

## Mission

Heartwood is an auditable coding-agent environment for biomedical research in controlled computing platforms. It brings a familiar conversational coding workflow to the environment where data already resides, while adding biomedical Skills, deployment policy, content-minimized audit records, and explicit export controls around OpenHands.

Platform network, identity, storage, and data-access controls remain authoritative. Heartwood evaluates application routes and records decisions, but it does not claim to replace a platform firewall, workspace sandbox, institutional approval, or clinical and statistical review.

## Product Goals

1. **Run in boundary.** Execute the agent, tools, biomedical Skills, and analysis code inside the researcher workspace; use platform controls as the authoritative data and network boundary.
2. **Remain approachable.** Present one conversation-first workflow through equivalent CLI and web interfaces, with the notebook acting as a launch and status bridge rather than a separate product.
3. **Make decisions inspectable.** Record model-route authorization, action confirmation, Skill identity, tool outcomes, and exports in a content-minimized, tamper-evident audit trail.
4. **Make deployment-owned models practical.** Connect to local or institution-authorized endpoints, offer a small centrally maintained set of local recommendations, and prepare a user-selected Hugging Face model from its `owner/model` identifier when the available runtime supports its format. Images and Heartwood settings contain neither model weights nor credential values.
5. **Make biomedical practice reusable.** Bundle repository-verified biomedical Skills, load them through OpenHands native Skill support, and keep extension installation explicit and auditable.
6. **Stay portable.** Isolate platform, data-source, policy, and image differences behind tested adapters and declarative platform-image contracts.
7. **Minimize owned infrastructure.** Reuse OpenHands for the agent loop, tools, confirmation, risk analysis, conversation persistence, and native Skills; reuse LiteLLM for providers; reuse platform proxies, identity, storage, and workflow engines.

## Success Criteria

- A researcher can configure a local or institution-authorized model route without storing a secret in Heartwood state, choose a compatible local recommendation or provide a Hugging Face identifier, and then use the same persisted selection and session through the CLI, web UI, and notebook bridge.
- The gateway resolves a user-selected Hugging Face repository to an immutable revision, chooses a supported CPU or NVIDIA GPU representation for the deployment, reports download and resource estimates before transfer, verifies downloaded content, and rejects unsupported or ambiguous repositories with a direct issue-report path.
- Deployment policy denies an unauthorized endpoint, capability tier, credential reference, or action-confirmation mode before initial task submission and before an approved or resumed continuation that may call the model; each decision is recorded without prompt, response, row, or secret content.
- OpenHands proposes and executes terminal and file actions under the selected upstream confirmation policy; Heartwood projects those events consistently into every interface and the audit record.
- Repository-verified Skills load through the OpenHands native loader, and an external Skill cannot enter persistent runtime storage without validation and an explicit installation decision.
- Generic images pass native AMD64 and ARM64 integration tests; each platform-derived image passes its declared image, startup, proxy, persistence, and registry contracts before publication.
- A platform is described as live-validated only after an immutable published image completes the documented synthetic workflow in that platform's control plane.
- A controlled-data workflow is described as supported only after the platform adapter, data-source adapter, export policy, biomedical Skill outputs, and institutional evidence have been independently validated.

## Scope Boundaries

- Heartwood does not implement another agent framework, provider client, tool protocol, confirmation engine, risk classifier, or conversation store.
- Heartwood does not provide a general-purpose Skill registry. It curates a bounded biomedical bundle and supports validated installation from mounted sources.
- Heartwood does not authorize external processing of controlled participant-level data. Provider use is governed by deployment policy, platform controls, dataset terms, and institutional approval.
- A recommended or successfully prepared local model is not a capability, biomedical-suitability, license-approval, production-readiness, or institutional-approval claim. User-selected repositories remain the researcher's and deployment's responsibility.
- Heartwood does not implement a workflow engine. Any batch export belongs in established CWL, WDL, or Nextflow execution infrastructure rather than the interactive agent runtime.
- Heartwood's web UI is a thin researcher-facing projection of the shared session contract, not a parallel implementation of OpenHands agent behavior.

## Users

- **Analyst-researcher.** Uses a guided, low-configuration conversation in the web UI or CLI. Repository-verified Skills are available without repeated activation prompts, while concrete tool actions follow the selected plain-language confirmation mode.
- **Methods developer.** Uses the CLI, event log, Skill authoring tools, deterministic replay, and test harnesses for reproducible development within deployment policy.
- **Platform or institutional reviewer.** Reviews the image, platform policy, model and credential route, synthetic evidence, audit behavior, export controls, and documented limitations before institutional use.

## Current Product Boundary

The repository implements the OpenHands conversation and workspace tools, equivalent CLI and web interaction over one session contract, notebook projection, configurable local or institution-authorized model profiles, centrally configured local recommendations, best-effort Hugging Face model preparation, platform detection, repository-verified Skill loading, explicit extension installation, two OpenHands confirmation modes, content-minimized audit records, generic images, and a Terra-derived image. Public tests, examples, and evidence use synthetic data only.

The runtime selects the detected generic, Terra, or Carina platform policy, but it does not include a real biomedical data-source adapter. Its default `SessionService` uses a synthetic OMOP fingerprint, so detection and reference-workflow results are integration fixtures rather than claims about project data. [Platform Support and Validation](../docs/platform-support.md) records the precise validation status.

## Reference Workflow

The first supported biomedical workflow is natural language to in-boundary code execution to aggregate results over OMOP on BigQuery: cohort definition, data-quality checks, a baseline model, aggregate output, count-floor enforcement, and an egress attestation for review. The same session must remain usable from the CLI, web UI, and notebook bridge and must survive the platform's normal persistence and autopause behavior.

The repository demonstrates this sequence with a localized 24-person synthetic CSV analogue of the required OMOP tables. The reference Skills define an adult target-condition cohort, report aggregate quality checks, fit an explicitly training-only age baseline, and apply an aggregate count floor; browser, CLI, replay, and container tests require the aggregate outputs to agree. This fixture is not evidence of BigQuery integration, live-platform behavior, or biomedical review.

## Continue Through the Architecture

Read the remaining documents according to the question being reviewed:

| Question | Document |
|---|---|
| Where does Heartwood run, and why are some artifacts platform-specific? | [Platform Architecture](02-platforms.md) |
| Which component owns projects, sessions, models, approvals, and interfaces? | [System Architecture](03-architecture.md) |
| How are biomedical procedures packaged and trusted? | [Skills and Extensions](04-skills.md) |
| What are the security, privacy, and institutional boundaries? | [Security and Compliance](05-security-compliance.md) |
| What is persisted, replayed, and exported? | [Audit and Reproducibility](06-observability-audit.md) |
| Which claims require deterministic, capable-model, container, or live-platform evidence? | [Testing and Evaluation](07-testing-eval.md) |
| How is the repository built, tested, licensed, and released? | [Engineering and Releases](08-development.md) |

Operational readers should return to [Get Started](../docs/getting-started.md), [Deploy Heartwood](../docs/deployment.md), or [Platform Support and Validation](../docs/platform-support.md) rather than treating architecture documents as setup instructions.
