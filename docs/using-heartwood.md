<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Using Heartwood

Heartwood provides one persisted coding-agent session through the terminal, web interface, and notebook bridge. All interfaces use the same gateway-owned OpenHands conversation, model route, action decisions, workspace, replay, and audit records.

## Start Or Resume

Run `heartwood` in an interactive terminal. A new installation opens setup when configuration is missing, reports recovery guidance when readiness checks fail, and otherwise resumes the selected conversation. Use `heartwood doctor` for a read-only readiness report and `heartwood --help` for automation and administration commands.

The terminal client opens full screen when the terminal supports it. The conversation is replayed from persistent state, the prompt composer remains available while the session is idle, and an elapsed-time status remains visible while the local model or agent is working. Use `Ctrl+Q` to exit and `Ctrl+L` to return focus to the prompt.

Use line mode for limited terminals, logs, or scripted demonstrations:

```bash
heartwood chat --plain
```

Line mode prints concise task, action, outcome, and error information without repeating the line just entered or routine allowed-route records. Complete route and event evidence remains available through replay and audit export.

## Submit Work

Describe the desired outcome and constraints in plain language. Heartwood sends the task to the selected OpenHands model with the repository-verified Skills and confines terminal and file tools to the managed session workspace. The workspace path is displayed during native launch and remains separate from the shell directory used to start Heartwood.

The model may answer directly, propose tool actions, or stop with an error. Local model turns and first inference startup can take several minutes; the active terminal status and launch stages report elapsed time rather than inventing a completion estimate.

## Review Actions

OpenHands may propose one or more actions in a single confirmation stop. Its public SDK approves or rejects that complete pending action set; it does not expose selective execution of individual members. Heartwood therefore lists every action, tool, summary, and upstream risk before presenting one truthful decision:

- **Allow all once** executes the displayed action set and continues until OpenHands finishes or reaches another confirmation stop.
- **Reject all** rejects the displayed action set without executing any member.

In the full-screen terminal, use the arrow keys to choose a decision and press Enter. In line mode, use `/allow` or `/reject`; no internal identifier is required. Explicit tool-call and legacy confirmation-request identifiers remain accepted for automation compatibility but are not part of the normal workflow.

**Ask Every Time** is the default. **Auto-Approve Low Risk** delegates risk classification and confirmation to OpenHands: low-risk actions run automatically, while medium-, high-, and unknown-risk action sets still require review. Heartwood does not expose unconditional automatic approval.

## Session Commands

The terminal clients support:

```text
/allow
/reject
/pause
/resume
/status
/replay
/audit-export
/help
/exit
```

`/pause` affects an idle or between-step conversation; the current gateway cannot interrupt an in-flight blocking model call. `/status` reports the active model, credential-reference availability, confirmation mode, and policy decision. `/replay` rebuilds the complete persisted conversation. `/audit-export` writes a scrubbed JSON Lines record that excludes prompts, responses, action summaries, paths, row values, and secrets.

## Web And Notebook Access

Run `heartwood serve` to serve the researcher web interface through the local gateway. In Jupyter environments, use the platform's authenticated proxy URL documented by the platform guide. The web interface presents the same conversation and groups every pending OpenHands action into one action-set review control.

The notebook package exposes the same session as Python view models and compact widgets. A pending OpenHands set appears as one approval control with every member listed; the first member identifier addresses the whole set in the programmatic `approve` and `deny` methods. The notebook bridge is intended for status, launch, and notebook integration rather than a separate agent implementation. Use one active writer per file-backed session and interact with the CLI, web UI, and notebook sequentially.

## Persistence And Audit

Session commands, OpenHands state, managed workspaces, model and action settings, installed Skills, and audit records live under the configured Heartwood state root. Reviewed model artifacts live under the configured model cache. Native installations set these paths automatically; containers require durable state and model-cache volumes as described in [Container Images](container-images.md). Until [Issue #22](https://github.com/SchmiedmayerLab/heartwood/issues/22) completes shared-writer coordination, use only one active CLI, web, or notebook writer for a file-backed session.

Replay is the operational session history. Audit export is a separate content-minimized evidence record. Neither a successful model route nor a successful tool action authorizes controlled-data use or export outside the workspace.
