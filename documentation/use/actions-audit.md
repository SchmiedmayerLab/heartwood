<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Actions and Audit History

The agent proposes commands and file operations; the gateway decides whether each proposal needs your decision, and the session records every decision and result.

## Review an Action Set

OpenHands groups related tool calls into one action set and pauses before running it.
When a set needs you, every interface lists all of its members: the exact command or file operation, the arguments, a risk label, and a summary.

1. Read the exact command or file operation first.
   The summary below it is labelled **Model-provided summary** because the model wrote it.
2. Allow or reject the whole set.
   Allowing runs every member once; rejecting runs none of them.
   - Terminal: arrow keys and Enter, or `/allow` and `/reject` in the plain interface.
   - Browser: **Allow All Once** or **Reject All** in the review panel.
   - Notebook: `session.approve(group_id=pending.group_id)` or `session.deny(group_id=pending.group_id)`.
3. Check the outcome under **Agent actions**, which shows each action's state, exit status, bounded output, and the project paths a typed file operation changed.

Each interface applies your decision to the set it displayed, so a set you have not seen is never decided.
The risk label comes from fixed threat checks on the action itself, not from the model.
An interrupted action whose result cannot be established is marked **Outcome Unknown** rather than treated as safe to repeat.
An allowed set is sent to run at most once.
If it appears to wait for review again after a restart, it may already have run: Heartwood marks it **Outcome Unknown** and refuses to allow or reject it again.

The command, file operation and path, and the exact arguments are shown exactly as the model proposed them and are labelled **Model-proposed command**, **Model-proposed file operation**, and **Exact arguments proposed by the model** in the terminal, browser, and notebook; the gateway does not verify that they do what the summary says.
A specialist task review is headed by the role from the specialist catalog; its task description, task prompt, and any specialist name that is not in the catalog were written by the model and are labelled **Model-provided**, and its exact arguments repeat them under the arguments label.
The specialists panel labels the same task text **Model-provided task** and names a specialist without a catalog role by its model-provided name.
An action heading that falls back to model-written text carries the same label.

Your approval never overrides deployment policy.
The sandbox policy, the deployment's model-route and egress rules, and an outside service's policy check apply to an action you allowed exactly as to any other, and an allowed action still cannot do what they forbid.

## Review Modes

The review mode decides which action sets the gateway allows on your behalf.
The gateway decides from the typed action alone: the tool, its validated arguments, and whether the session runs in an isolated worker.
Model-written summaries and risk ratings never cause an action to run without you.

| Mode | Heartwood allows without asking | Heartwood asks you about |
|---|---|---|
| **Review Every Action** (`review-all`) | Nothing | Every action set |
| **Review Changes** (`review-changes`) | Viewing a file inside the project, and plan updates in the task tracker | Every terminal command, every file change, specialist tasks, and anything else |
| **Review Outside Effects** (`review-outside-effects`) | Terminal commands, file operations, and plan updates inside the isolated worker | Specialist tasks, anything bound to an outside service, and anything else |

A lone thinking note with no effect is the only proposal OpenHands runs without pausing.

![Heartwood Action Review settings listing the review modes and the reason one is unavailable](../assets/screenshots/browser-action-settings-light.png#gh-light-mode-only){ .theme-screenshot-light }
![Heartwood Action Review settings listing the review modes and the reason one is unavailable](../assets/screenshots/browser-action-settings-dark.png#gh-dark-mode-only){ .theme-screenshot-dark }

### Default and Available Modes

When you run `heartwood`, `heartwood --interface web`, or a notebook, the session runs on your own machine without an isolated worker:

- **Review Every Action** is the default.
- **Review Changes** can be selected when the selected model route needs no credential, such as a Heartwood-managed model; a platform-isolated route would also qualify, but no built-in platform declares one.
  With an API key or a sign-in token, the mode is listed as unavailable with that reason, because the credential is not isolated from commands the agent runs.
- **Review Outside Effects** is never available.

When an operator starts the gateway with a deployment manifest that defines an isolated worker, **Review Outside Effects** is the default if the manifest permits it, and **Review Every Action** otherwise.
A manifest without an isolated worker follows the rules above.
The operator's manifest sets which modes a deployment permits, as described in [Security](../operate/security.md#action-review); a project can narrow that list, never widen it.

Each interface shows the mode in use, the default, and every unavailable mode with its reason.

### Actions That Always Require Review

No review mode decides these on your behalf:

- an action that the fixed threat checks rate as high risk or cannot classify;
- an action set that mixes members under different rules, or contains any member no rule covers;
- a file view of the project root, of `.heartwood/` or `.git/`, of a path the gateway cannot resolve inside the project, or whose reviewed arguments differ from the typed action;
- every terminal command outside an isolated worker, because a persistent shell can change what the same command text does;
- [outside operations and outside result releases](#outside-operations-and-result-releases);
- Skill installation, model changes, and workflow stage review.

Automatic decisions also stop when you pause the session, reject an action set, or send a message that Heartwood refuses: whatever the agent proposes next waits for you.

### Change the Mode

=== "Terminal"

    Enter `/permissions` to see the modes, or `/permissions review-changes` to select one.
    In the full-screen interface, `Ctrl-P` and **Action Review** open the same choice.

=== "Browser"

    Select the current mode in the session header, or open **Settings** and then **Action Review**.

=== "Notebook"

    ```python
    session.action_settings()
    session.select_action_review_mode("review-changes")
    ```

To start with a mode selected, pass it when you launch:

```bash
heartwood --review-mode review-changes
```

The same permission checks apply as for a selection made later; when they do not allow the mode, Heartwood stops with the reason instead of starting in another mode.
`heartwood actions` prints the modes the current project offers without starting a session.

Without a deployment manifest, project state lives inside the research directory, so Heartwood does not store a review mode there.
A selection lasts until that Heartwood process or notebook gateway exits, and the next launch starts at the default.
With a deployment manifest, the selection is stored in protected project state and shared by every browser session of that project.

A stricter mode applies to the next action set at any time.
A less strict mode can be selected only while no session is running, paused, or waiting for review.
Changing the mode never decides a set that is already waiting for you.

### Exposure Under Reduced Review

Under **Review Changes**, file content the agent views goes to the selected model route without a per-file decision.
The first step that could change a file or send data anywhere else is a command or a file change, and it waits for you.

Under **Review Outside Effects**, the sandbox confines commands, not review: they can change or delete files in the worker workspace and send content to the admitted model endpoint without asking.
Review applies to proposed tool calls; it does not intercept every operation of code already running inside the sandbox.
Commands and file operations inside a worker run in the worker itself, so no review mode requires an outside, digest-bound approval before every local operation.
Such a profile is unavailable until an executor that mediates each operation separately from the worker is qualified.

## Automatic Decisions in the Record

The gateway records an automatic decision exactly like yours, under the actor `gateway`.
The session journal holds a `command.received` event and an `approval.recorded` event with `actor_id` `gateway`, the decision, the content-bound action-set identifier, and `review` naming the `mode` and the `rule`:

| Rule | Applies when |
|---|---|
| `read.file-view` | The file editor views a project file under **Review Changes** |
| `plan.task-tracker` | The task tracker reads or updates the plan under **Review Changes** |
| `sandbox.contained` | A terminal, file-editor, or task-tracker action runs in an admitted, active worker under **Review Outside Effects** |

The audit export keeps the actor, mode, rule, action-set identifier, and action count.
The decision binds the exact set: if the pending set differs when the decision is applied, nothing runs.
Checkpoint evidence, model-route authorization, and workflow limits apply as they do to your decisions.
If Heartwood cannot apply an automatic decision, for example because required audit evidence is unavailable, the set returns to you with a note that automatic review was not applied.
Once an automatic decision has been sent to run, it is never returned to you; if Heartwood stops before recording the result, the set is marked **Outcome Unknown**.

Every interface shows these actions as allowed automatically, with the rule, and never as approved by you.
No interface command can record a decision as `gateway`.

## Outside Operations and Result Releases

An outside operation sends a request from the worker to a deployment service; an outside result release passes that service's result to the worker and its model.
Heartwood ships no outside-service adapter, and `heartwood gateway serve` installs none; outside operations exist only where a trusted launcher that embeds the gateway supplies one.
See the [Platform Contract](../operate/platform-contract.md#outside-operation-admission).

Where a deployment provides outside operations, each one is reviewed separately from the tool call that proposed it, under the title **Outside Operation** or **Outside Result Release**.
Without expanding anything, the review shows:

- that the content leaves the sandbox, or leaves Heartwood for the model route;
- the service, destination, and operation, or the model route;
- the size and SHA-256 digest of the canonical JSON that would be sent;
- each submitted input by reference, size, and digest, without its bytes;
- the time until which the review is valid.

The browser shows the request or result content directly.
When it is longer than the 2,000-character preview, the browser marks the preview as truncated and offers all of it under **Complete reviewed proposal prepared by the gateway**; the terminal and notebook print the complete proposal.
The summary of these reviews comes from the outside-service adapter or the gateway and is labelled **Gateway-provided summary**.
A released result reaches the model as message content; it is data from the service, not instructions Heartwood has checked.

If an approval is recorded but cannot be sent, for example while the records service is unavailable, nothing is sent until you choose.
Every interface then shows the review as **Approval not sent** and offers the same two choices: retry that approval, or reject the review to withdraw it.
The window, terminal, or notebook that made the approval keeps it and resends the same command: **Retry request** in the browser, `/retry` or **Retry the Recorded Approval** in the terminal, and `session.retry()` in a notebook.
A retry sends the approval at most once; after it has been sent, withdrawn, or expired, nothing is offered for a retry, and `/retry` and `session.retry()` say why.
A new approval from any interface is refused while the recorded one waits, and an interface that did not make the approval, or was restarted since, can only reject the review or wait for it to expire.
Rejection stays available while the approval waits, even after an unrecoverable session error or a restart, because it sends nothing and needs no worker.
After an unrecoverable session error the approval can no longer be sent, so no interface offers its retry, and each says that only rejection remains.
Opening or refreshing the session, other commands, and automatic decisions never send it.
An approval not sent before the review's validity ends is recorded as expired.

## Session History

The session records your requests, model-route decisions, final responses, proposed tools, decisions, tool outcomes, task status, model usage, specialist lineage, and stable errors.
Every interface reads the same gateway projection of that history.
Response tokens are shown while the model works but are not persisted.

Each proposed tool has one action record with its OpenHands identifiers, decision, typed arguments, state, bounded outcome, and affected paths where a typed file operation reported them.
States are proposed, awaiting review, approved, rejected, running, succeeded, failed, and outcome unknown.
Heartwood does not infer file changes from shell command text.

Use `/replay` in the terminal or **Activity & audit** in the browser to inspect the history.
Replay verifies the audit chain and its hash binding to each session event before returning anything.

## Audit Export

Use `/audit-export`, **Export audit** in the browser, or `session.audit_export()` in a notebook to create a JSON Lines file for review.
The export is scrubbed and content-minimized: task counts instead of task text, usage totals instead of completions, and stable error codes instead of provider messages.
Commands, arguments, paths, file content, diffs, tool output, and specialist instructions and results stay in private session state.
Identifiers, decisions, counts, and timestamps remain and may still be sensitive in context.

From the command line, `heartwood audit verify` fully verifies the session and audit history, and `heartwood audit export --output PATH` writes an export.

An audit record supports review and reproducibility; it does not show that a scientific result is correct or that a deployment meets a regulatory requirement.
A normal export is not an authoritative record, because the project owner can replace or delete it.
Operators can create an externally retained, signed record by following [Audit Checkpoints and Retention](../operate/audit-checkpoints.md).
