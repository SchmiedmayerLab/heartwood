<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Product Boundaries

Heartwood is a research agent system built around an OpenHands coding agent.
A gateway owns project state, policy, action review, outside authorization, records, and audit; the OpenHands agent worker proposes and runs actions, either in the gateway process or inside an OpenShell sandbox; terminal, browser, and notebook interfaces render one gateway contract.
Heartwood reuses OpenHands for the conversation loop and coding tools and does not maintain a parallel agent loop or tool implementation.
[System Architecture](system.md) describes the components and the trust boundary between them.

## In Scope

- one current-directory research project with private state, locally in `.heartwood/` or in external state bound by an operator deployment manifest;
- terminal, browser, and notebook interfaces over one gateway contract;
- OpenHands conversations run locally or by a managed native worker in an OpenShell sandbox, with admitted, attempt-bound, budgeted worker traffic and revocation;
- complete-set action review with gateway-decided review modes under an operator ceiling;
- content-bound researcher authorization of outside operations and result releases;
- persistent sessions, replay, tamper-evident audit export, and signed, retained audit checkpoints;
- [reviewed research stages](../use/research-workflows.md) with independent artifact checks, [experiment records](experiments.md), task tracking, and bounded research specialists;
- verified and explicitly installed [Skills](skills.md);
- platform-aware model discovery and Heartwood-managed model planning;
- generic, Terra, and Stanford Carina platform adapters and a versioned Heartwood–platform contract with offline conformance checks; and
- container, native, worker, and qualification artifacts built from pinned inputs, with [native qualification](testing.md#native-qualification) evidence bound to a commit.

## Outside the Product Boundary

Heartwood does not:

- discover, authorize, or validate a real biomedical dataset;
- determine whether a provider or platform may process a particular data class, or claim readiness for sensitive data;
- replace operating-system, container, scheduler, or cloud isolation; the local backend runs tools with the researcher's identity and isolates nothing;
- ship a production OpenShell gateway or outside-service adapter;
- guarantee model output, code correctness, statistical validity, or reproducibility;
- approve third-party Skills or model licenses on behalf of an institution;
- provide a multi-user authentication service for the browser; or
- approve individual tool calls separately when OpenHands supplies them as one action set.

## Design Principles

1. **Reuse the agent platform.** OpenHands owns agent reasoning, conversation persistence, and coding tools.
2. **One product contract.** Every interface uses the same gateway commands, events, configuration, and project state.
3. **The gateway decides.** Worker output is data; approvals, outside effects, and records require the gateway's own admission and decisions.
4. **Keep platform differences at the boundary.** Adapters define capabilities and policy without forking the workflow.
5. **Prefer clean pre-1.0 contracts.** Superseded commands and state layouts are removed rather than maintained as parallel paths.
