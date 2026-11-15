<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Platform Contract Examples

All workloads, identities, prices, requests, and observations here are synthetic.
These files demonstrate draft message relationships; they never authorize or submit work.

| Example | Outcome |
|---|---|
| `exploration.overview.json` | Researcher-approved bounded work receives a platform authorization and reservation; the backend is running. |
| `batch.overview.json` | Researcher approval remains visible, but the platform denies submission after other work consumes the budget. |
| `inference.overview.json` | Price and available budget remain unknown, with explicit explanations; the platform denies authorization. |

Read the prediction, estimates, reviewed operation set, researcher decision, platform authorization, and budget/backend observations in that order.
The proposal includes its reviewed estimate snapshot.
The component estimate is already included in the plan total, so it is not another charge.
The native request files belong to a synthetic test service; real integrations retain their existing provider or scheduler payloads.

Each `*.proposal.json` and `*.native-request.json` is the exact document referenced by its overview.
Tests verify the SHA-256 bindings against these files, including rejection when reviewed bytes change.
Formatting changes to a proposal change its hash and require updating its review envelope and decisions.

From the repository root, check an overview without contacting a service:

```bash
uv run python packages/adapters/scripts/check_platform_response.py planning-overview.schema.json fixtures/synthetic/platform-contract/batch.overview.json
```

This checks structure and linked-record consistency.
Authentication, freshness, policy enforcement, and budget enforcement remain platform/runtime responsibilities.

## Immediate and Pending Results

`mcp-tasks.json` contains captured exchanges for the published MCP Tasks extension (2026-07-28).
The immediate and completed asynchronous estimates contain the same estimate as `exploration.overview.json`.
A separate operation demonstrates a cancellation acknowledgement followed by a working task and an independently running backend with additional observed spending.
Task status, backend status, and budget observations are separate facts.

```bash
uv run python packages/adapters/scripts/check_platform_response.py mcp-tasks fixtures/synthetic/platform-contract/mcp-tasks.json
```

The checker validates individual exchanges against the pinned upstream definitions and Heartwood profile checks.
It does not contact the server, establish transition history, or authenticate the captured declarations.
