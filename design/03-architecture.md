<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# 03 — Architecture

## Principles

1. Reuse the agent loop; own the medical and compliance layer.
2. In-boundary by default — reaching a model is an explicit, policy-gated, audited action.
3. Platform-agnostic core; all platform-specific code lives behind an adapter.
4. Detection proposes, a human confirms — nothing loads or runs silently.
5. Standards-based, portable extension (`SKILL.md`, MCP).
6. Offline/air-gapped is the primary path.
7. The CLI is the primary interaction contract; notebook UI is a presentation adapter.

## Core and adapters

```
        ┌──────────────── platform-agnostic CORE ────────────────┐
 UI     │ CLI session (primary) · notebook API/widgets (secondary)│
        │ shared commands · approvals · event stream · replay     │
 Detect │ environment + dataset detector → skill/sub-agent        │
        │ activation (propose → confirm)                          │
 Skills │ SKILL.md engine (OpenHands) + heartwood.* metadata     │
 Agent  │ OpenHands SDK: event-sourced loop · sandbox · MCP ·     │
        │ sub-agents · HITL · SKILL.md loading  [adapter facade]  │
 Policy │ model policy layer: deny-egress · capability tier ·     │
        │ egress attestation                                      │
 Audit  │ append-only, hash-chained event + audit log; export     │
        └───────────────────────────┬────────────────────────────┘
                                    │  adapter SPI
      Platform ──── ModelProvider ──── DataSource ──── Registry
   terra·dnanexus·   vertex·azure·    omop-bq·fhir·    local·git·
   seven-bridges·    bedrock·local    drs·genomics     oci·index
   generic
```

## The foundation: OpenHands Software Agent SDK

The core builds on `openhands-sdk`/`openhands-tools` (MIT), consumed as a **pinned dependency behind a stable facade** (`Agent`/`Tool`/`Conversation`/`EventLog`). The SDK provides, out of the box: an event-sourced agent loop (deterministic replay, pause/resume/fork), sandboxed execution, a native MCP client, sub-agent delegation, two-layer human-in-the-loop control (confirmation policies + a risk-scored analyzer), **native `SKILL.md` loading** (progressive disclosure, keyword/always triggers, MCP-tools-per-skill, a public-skills marketplace), model routing via LiteLLM, and conversation export.

The SDK is Apache/MIT-permissive and reused rather than forked; the facade absorbs its rapid release cadence and keeps the core swappable. This choice keeps first-party code MIT and avoids reinventing the agent loop, sandbox, and skills machinery.

## The adapter SPI

Four interfaces are the entire platform-specific surface. Reference implementations ship for the priority platforms; the `generic` adapter is the baseline. Adding a platform means writing an adapter, not changing the core.

- **`PlatformAdapter`** — detects the platform from the environment; provides data mount paths, the credential allowlist, the Docker base image, and the default egress policy.
- **`ModelProviderAdapter`** — configures an in-perimeter endpoint through LiteLLM, reports its capability tier, and emits egress-attestation records.
- **`DataSourceAdapter`** — scoped read plus the schema/format fingerprint the detector uses.
- **`RegistryAdapter`** — resolves and verifies skills from a source.

## What the platform adds on top of the SDK

The SDK is the engine; heartwood contributes: (1) the **model policy layer**; (2) the **environment/dataset detector** that decides which skills activate; (3) the **`heartwood.*` metadata** semantics and a **verification gate** before skills reach the SDK's skill directory; (4) the **compliance kit** and **egress attestation**; (5) **hash-chained audit + scrubbed export**; (6) the **adapters**; (7) the **analyst interaction surfaces**.

## Interaction model

The CLI is the main product interface, the development harness, and the stable target for CI. It supports both scripted commands and an agentic interactive session: chat turns, proposed actions, visible tool/code events, approve/deny prompts, pause/resume, replay, and audit export.

Notebook interfaces do not own separate behavior. They attach to the same session API and event stream, rendering a friendlier view for Terra/Jupyter users: chat, detected dataset cards, proposed skills, approval controls, policy status, activity trace, and export buttons. This keeps the non-technical experience approachable without creating a second execution path.

## Model policy layer

LiteLLM handles provider routing; on top, a per-platform **policy profile** denies egress by default, allows only the configured in-perimeter endpoint, enforces the model's **capability tier** (caps autonomous tool-loop depth for weaker models), and records every call to the audit log for the egress attestation.

## Tools

Platform and data access are exposed as Python MCP servers: a data gateway, OMOP/BigQuery, FHIR, DRS, and a headless notebook driver. JavaScript is deferred until a custom notebook or web frontend needs it.

## Durability

The SDK event log is persisted to the **workspace disk** (survives autopause) via its `FileStore`; heartwood adds hash-chaining for tamper-evidence and a `resume` command. No external database.

## Data flow

1. Launch the image and start a CLI or notebook-backed session.
2. Detector: `PlatformAdapter` → platform; `DataSourceAdapter` fingerprint → dataset + confidence; manifest lookup → candidate skills.
3. Detector proposes with evidence; the researcher confirms.
4. NL question → the agent plans, writes code, runs it in the sandbox via a data MCP tool.
5. Model calls pass the policy layer → the in-perimeter endpoint; each is audited.
6. Aggregate results returned; event log checkpointed; egress attestation available.
