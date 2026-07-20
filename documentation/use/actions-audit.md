<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Actions, Sessions, and Audit History

Heartwood separates a model suggestion from an executed action.
OpenHands proposes tools, Heartwood applies the selected confirmation policy, and the session records the decision and result.

## Action Confirmation Modes

| Mode | Behavior |
|---|---|
| **Ask Every Time** | Present every OpenHands action set for one allow-or-reject decision |
| **Auto-Approve Low Risk** | Automatically allow actions classified as low risk and request confirmation for riskier sets |

The detected platform policy determines which modes are available.
**Ask Every Time** is the default and the recommended mode while learning the system or working with sensitive projects.

Change the setting interactively in the browser or with:

```bash
heartwood actions set ask-every-time
heartwood actions set auto-approve-low-risk
```

## Grouped Decisions

The current OpenHands confirmation callback may propose more than one tool call in one callback.
Heartwood displays those calls as one action set and resolves them together instead of presenting misleading per-item controls.

A set is allowed only after the user or policy resolves the pending callback.
Rejecting the set prevents all of its members from executing.

## Session History

Session events include user requests, model-route decisions, assistant responses, proposed tools, confirmation decisions, tool outcomes, pause state, and errors.
Terminal, browser, and notebook views derive their presentation from that same event stream.

Use `/replay` in the terminal or the browser activity view to inspect it.
Replay verifies the audit chain and the one-to-one hash binding between each audit record and the complete session event before returning persisted history.

## Audit Export

Use `/audit-export` or the browser export control to create a JSON Lines file for review.
The export is scrubbed and content-minimized, but it still contains operational identifiers, decisions, classifications, counts, and timestamps that may be sensitive in context.

An audit record supports review and reproducibility; it is not proof that a scientific result is correct or that a deployment meets a regulatory requirement.
