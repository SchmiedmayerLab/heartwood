<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Actions, Sessions, and Audit History

Heartwood separates a model suggestion from an executed action.
OpenHands proposes tools, the gateway decides whether each proposal needs your review, and the session records the decision and result.

## Review Modes

The agent pauses before every registered OpenHands action set in every mode.
The review mode selects which paused sets the gateway allows on your behalf and which wait for you.
The gateway decides from the typed action alone: the tool, its validated arguments, and where the session runs.
Model-written summaries and risk ratings never influence whether you are asked.

| Mode | Heartwood allows automatically | You decide |
|---|---|---|
| `review-all` · **Review Every Action** | Nothing | Every action set |
| `review-changes` · **Review Changes** | Viewing a file inside the project, and plan updates in the task tracker | Every terminal command, every file change, specialist tasks, and anything else |
| `review-outside-effects` · **Review Outside Effects** | Terminal commands, file operations, and plan updates that run inside the isolated worker | Anything that leaves the worker, specialist tasks, flagged actions, and anything else |

**Review Every Action** is the default when a session runs on your own machine without an isolated worker.
**Review Changes** is available there as an opt-in when the selected model route is credential-free or platform-isolated.
With any other model credential, a session outside an isolated worker reviews every action, and each interface lists **Review Changes** as unavailable with that reason.
**Review Outside Effects** is the default when sessions run in an isolated worker, and it cannot be selected or applied anywhere else.

Each automatic decision applies one named rule to the complete action set.

| Rule | Applies when |
|---|---|
| `read.file-view` | The file editor views a path that the gateway resolved, through symbolic links, to an entry inside the project and outside `.heartwood/` and `.git/`. |
| `plan.task-tracker` | The task tracker reads or updates the plan. |
| `sandbox.contained` | A terminal, file-editor, or task-tracker action runs in a session whose worker attempt is admitted and active. |

Heartwood never treats a terminal command as read-only from its text.
A persistent shell can change what the same command does, so outside an isolated worker every command waits for you.
A file view of the project root, of a path the gateway cannot resolve, or of a path whose reviewed arguments differ from the typed action also waits for you.
An action set with members under different rules, or with any member under no rule, waits for you as a whole.
In every mode, an action that the fixed threat checks flag as high risk or cannot assess waits for you.
Those checks read the action itself; they can send an action to you but never allow one.

Outside operations, release of an outside result to the worker, Skill installation, model changes, and workflow stage review use their own approvals and are never automatic.
See the [Platform Contract](../operate/platform-contract.md#outside-operation-admission).

The risk badge next to a pending action is the result of the fixed threat checks.
Review the exact command or file operation first, then the separately labelled model-provided summary.

### Select a Mode

In the full-screen terminal, enter `/permissions` or press `Ctrl-P` and choose **Action Review**.
In the browser, open **Action Review** settings.
Each interface shows the mode in use, the default, every mode the deployment does not permit, and why.

With a deployment manifest, the selection is stored in protected project state and shared by every interface:

```bash
heartwood actions
heartwood actions set review-changes
```

Without a deployment manifest, project state is inside the research directory, so Heartwood never stores or reads a review mode there.
A selection made in the terminal or browser lasts until that Heartwood process exits, and the next launch starts with **Review Every Action**.
To select a mode when starting, pass `--review-mode`:

```bash
heartwood --review-mode review-changes
```

The flag is the same selection made at start, so the deployment ceiling, the isolated-worker requirement, and the credential check still apply.
Heartwood stops with the reason instead of starting in a different mode when they do not permit the value.

A stricter mode can be selected at any time and applies to the next action set.
A looser mode can be selected only while no session is running, paused, or waiting for review.
Changing the mode never decides an action set that is already waiting for you.

An operator limits the available modes for a deployment, as described in [Security](../operate/security.md#action-review).
A project or researcher can select a stricter mode than the deployment permits, never a looser one.

### What an Automatic Decision Means

Under **Review Changes**, file content that the agent views goes to the selected model route without a per-file decision.
The first step that could change a file or move data anywhere else is a command or file change, and it waits for you.

Under **Review Outside Effects**, the sandbox, not review, confines commands: they can change or delete files in the worker workspace and send content to the admitted model endpoint without asking.
Review controls apply to registered tool proposals; they do not intercept every operation of code already running inside the sandbox.

## Automatic Decisions in the Record

Every automatic decision is an ordinary recorded decision made by the actor `gateway`.
The session journal contains a `command.received` event and an `approval.recorded` event with `actor_id` `gateway`, the decision, the content-bound action-set identifier, and `review` with the `mode` and `rule` that applied.
The audit export keeps the actor, mode, rule, identifier, and action count.
The decision binds the exact action set: if the pending set differs when the decision is applied, nothing runs.
Checkpoint evidence, model-route authorization, and workflow limits apply to automatic decisions exactly as they do to yours.

Terminal, browser, and notebook clients show these actions as allowed automatically, with the rule, and never as approved by you.
No interface command can record a decision as `gateway`.
An executed action without a recorded decision is shown without a decision, and a worker that reports one is fenced.

## Outside Operations and Result Releases

Where a deployment enables outside operations, their reviews are separate from the review of the tool call that proposed them.
Every interface titles the review **Outside Operation** or **Outside Result Release** and marks it as leaving the sandbox or leaving Heartwood for the model route.
Without expanding anything, the review states the service, destination, and operation, or the model route, together with the size and SHA-256 digest of the canonical JSON that would be sent and the time until which the review is valid.
The browser shows the request or result content directly.
When that content is longer than the 2,000-character preview, the browser says that the preview is truncated and offers all of it under **Complete reviewed proposal**; the terminal and notebook print the complete proposal.
Inputs submitted with an operation are listed by reference, size, and digest; their bytes are not displayed.
The summary of these reviews comes from the trusted service adapter or the gateway and is labelled **Gateway-provided summary**, while summaries written by the model keep the **Model-provided summary** label.

If an approval is recorded but cannot be sent, for example while the records service is unavailable, nothing is sent until you choose.
Retry the same approval to send it, or reject the review to withdraw it; opening or refreshing the session, other commands, and automatic decisions never send it.
An approval that is not sent before the review's validity ends is recorded as expired.

## Grouped Decisions

OpenHands may propose several related tool calls as one action set.
Heartwood displays those calls as one action set and resolves them together instead of presenting misleading per-item controls.

A set runs only after a recorded decision allows it: yours, or the gateway's under a review rule.
Allowing the set runs every member once.
Rejecting the set prevents all of its members from executing.

## Session History

Session events include user requests, model-route decisions, final assistant responses, proposed tools, confirmation decisions, tool outcomes, lifecycle, task status, model usage, specialist lineage, and stable errors.
The gateway turns that stream into one projection for terminal, browser, and notebook clients.
Incremental response tokens are visible while the model works but are not persisted.

Each proposed tool has one versioned action record.
It correlates the OpenHands action and tool-call identifiers, grouped decision, typed terminal, file-editor, Task, outside-operation, outside-result, or other arguments, execution state, bounded outcome, and explicitly supported affected paths.
States distinguish proposed, awaiting review, approved, rejected, running, succeeded, failed, and outcome unknown.
A record also names the review rule when the gateway allowed the action.
Heartwood does not infer authoritative file changes from shell command text.

Use `/replay` in the terminal or the browser activity view to inspect it.
Replay verifies the audit chain and the one-to-one hash binding between each audit record and the complete session event before returning persisted history.

## Audit Export

Use `/audit-export` or the browser export control to create a JSON Lines file for review.
The export is scrubbed and content-minimized.
It records task counts and statuses instead of task titles or notes, usage totals instead of completion content, and stable error codes instead of provider details.
Specialist delegation instructions and returned review prose remain out of the audit export; only minimized lifecycle, action, and usage evidence is retained there.
Exact action arguments, commands, affected paths, file content, diffs, tool output, and failure text remain in private session state and are not copied into the audit export.
Operational identifiers, decisions, classifications, counts, and timestamps may still be sensitive in context.

Run `heartwood audit verify` to fully verify the paired session and audit history without creating a new export.
Use `heartwood audit export --output PATH` when a reviewed copy is needed outside private project state.

An audit record supports review and reproducibility; it is not proof that a scientific result is correct or that a deployment meets a regulatory requirement.
A normal export is not an authoritative records artifact because the project owner can replace or delete it.
Platform operators can create an externally retained, signed artifact by following [Audit Checkpoints and Retention](../operate/audit-checkpoints.md).
