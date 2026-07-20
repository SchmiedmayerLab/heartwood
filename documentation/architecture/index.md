<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Product Boundaries

Heartwood adds a researcher-oriented control plane around an OpenHands coding agent.
It owns project setup, platform capabilities, model routing, action-confirmation policy, Skills, session presentation, and audit evidence while reusing OpenHands for the conversation loop and coding tools.
Heartwood does not fork the OpenHands agent loop or maintain a parallel coding-tool implementation.

## In Scope

- one current-directory project and private project-scoped state;
- terminal, browser, and notebook presentations over one gateway contract;
- platform-aware model discovery and Heartwood-managed model planning;
- OpenHands SDK conversations and tool execution;
- grouped action review and deployment-constrained auto-approval;
- repository-verified and explicitly installed Skills;
- persistent sessions, replay, and tamper-evident audit export;
- generic, Terra, and Stanford Carina platform adapters; and
- container, native, CI, and release artifacts.

## Outside the Product Boundary

Heartwood does not:

- discover, authorize, or validate a real biomedical dataset automatically;
- determine whether a provider or platform may process a particular data class;
- replace operating-system, container, scheduler, or cloud isolation;
- guarantee model output, code correctness, statistical validity, or reproducibility;
- approve third-party Skills or model licenses on behalf of an institution;
- provide a multi-user authentication service for the browser; or
- independently approve individual tool calls when OpenHands supplies them as one confirmation callback.

## Design Principles

1. **Reuse the agent platform.** OpenHands owns agent reasoning, conversation persistence hooks, and coding tools.
2. **One product contract.** Every interface uses the same gateway commands, events, configuration, and project state.
3. **Make safe behavior visible.** Project, route, credential lifetime, action grouping, compute request, and recovery are explicit.
4. **Keep platform differences at the boundary.** Adapters define capabilities and policy without forking the workflow.
5. **Prefer clean pre-1.0 contracts.** Superseded commands and state layouts are removed rather than maintained as parallel paths.
