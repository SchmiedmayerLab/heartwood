<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Use Heartwood

Heartwood works on one project at a time. The project is exactly the directory where you start the command, and approved agent actions may modify files in that directory or its subdirectories.

## Start a Project

Change into the analysis directory before starting Heartwood:

```bash
cd /path/to/analysis-project
heartwood
```

Heartwood does not search parent directories or require a workspace option. Starting it from a nested directory creates a separate project there. Run `pwd` first when the boundary matters.

On the first run, Heartwood guides you to a model connection. Local setup offers a small recommendation list and an **Other Hugging Face model** choice; Heartwood determines the supported runtime and shows resource guidance. A configured project opens the interactive conversation directly. A downloaded local model that needs managed compute directs you to `heartwood launch`.

Use the read-only diagnostic at any time:

```bash
heartwood doctor
```

`ready`, `setup-required`, and `compute-required` describe normal next steps. `recovery-required` identifies configuration or runtime evidence that must be corrected before work continues.

## Ask for Work

Enter a specific task at the `heartwood>` prompt. State the inputs, expected outputs, and constraints that matter for review. For example:

```text
Inspect the CSV files in input, summarize missing values by column, and write the aggregate result to missingness-summary.csv. Do not include row-level values.
```

Heartwood passes the request to OpenHands together with the reviewed Skills available to the project. OpenHands decides which coding tools to use. Heartwood records the resulting messages, proposed actions, decisions, and tool outcomes in the project session.

For automation or a basic terminal, submit one task without opening the full-screen interface:

```bash
heartwood chat --plain --prompt "Summarize the analysis scripts and their outputs."
```

## Review Actions

Heartwood defaults to **Ask Every Time**. When OpenHands reaches a confirmation stop, Heartwood shows every member of the pending action set with its tool, summary, arguments, and risk classification.

- Choose **Allow all once** only when every action in the set is appropriate.
- Choose **Reject all** when any member is unnecessary, unsafe, out of scope, or unclear.

The OpenHands SDK approves or rejects one confirmation stop as a group. Heartwood does not imply that individual actions can be executed independently when the upstream runtime cannot support that behavior.

The optional **Auto-Approve Low Risk** mode lets actions classified by OpenHands as low risk execute automatically. Medium-, high-, and unknown-risk action sets still stop for review. The selected platform policy determines whether this mode is available.

## Control the Session

The terminal interface supports arrow-key navigation and displays available actions at each confirmation stop. Type `/help` for the current command list. Common commands include:

| Command | Result |
|---|---|
| `/status` | Show the selected model, credential status, action mode, and policy decision. |
| `/allow` | Allow the complete pending action set once. |
| `/reject` | Reject the complete pending action set. |
| `/pause` and `/resume` | Pause or resume the current session. |
| `/replay` | Reprint the persisted session history. |
| `/audit-export` | Create a scrubbed JSON Lines audit export. |
| `/exit` | Close the interface without deleting project state. |

Use a session identifier when separate conversations should share the same project files:

```bash
heartwood --session-id cohort-review
heartwood --session-id manuscript-review
```

Return to a session by starting Heartwood with the same identifier. The web interface lists sessions recorded in the current project.

## Use the Web Interface

Start the shared gateway and web application from the project directory:

```bash
heartwood serve
```

Open `http://127.0.0.1:8767/`. The browser uses the same project configuration, sessions, action settings, model profiles, Skills, and audit store as the terminal. See [Work with Heartwood in a Browser](web-interface.md) for model setup and notebook-proxy use.

An unconfigured project opens the shared setup view automatically. Changes made through the browser are visible to the next terminal or notebook command, and opening **Settings** refreshes changes made by another interface. A downloaded local model still needs the terminal-owned `heartwood launch --web` lifecycle before the browser can submit a task.

## Use a Notebook

The notebook bridge also binds to the notebook process's current directory:

```python
from heartwood.notebook import NotebookSession

session = NotebookSession(session_id="cohort-review")
view = session.replay()
print(view.event_count)
```

Run terminal, web, and notebook writes to the same session sequentially. File-backed sessions protect one process at a time; independently running writers are not a supported coordination mechanism.

## Understand Project State

Heartwood creates this private directory inside the project:

```text
.heartwood/
├── config.toml
├── state.json
├── sessions/
├── models/
├── skills/
├── audit/
├── runtime/
├── logs/
└── cache/
```

The directory is ignored as a unit by the Git rule Heartwood places inside it. It contains no inline provider tokens, but it may contain in-boundary conversation content and operational metadata. Keep it on storage appropriate for the project, do not commit it, and do not ask the agent to inspect or modify it.

To move a project, move its files and `.heartwood/` together while Heartwood is stopped. To start cleanly, use a new empty directory. Heartwood intentionally rejects an unknown or obsolete `.heartwood/` layout instead of guessing how to reinterpret it.

## Export an Audit Record

The in-boundary session contains conversation content needed for resume. Audit export is a separate, content-minimized record of route decisions, action classifications, approvals or rejections, tool outcomes, and Skill activation.

```bash
heartwood --session-id cohort-review audit export
```

Review the export before moving it outside the deployment boundary. A scrubbed export is evidence about Heartwood activity, not automatic authorization to disclose data or results.
