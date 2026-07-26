<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Use the Terminal

The terminal is Heartwood's primary interactive interface and the only interface supported on Stanford Carina.
It uses the same gateway and OpenHands session state as the browser and notebook bridge.

## Start a Session

```bash
cd /path/to/project
heartwood
```

The full-screen interface provides arrow-key navigation, colored status, elapsed-time activity, an action-review panel, and a command palette.
Heartwood automatically falls back to the plain interface when the terminal cannot support the full-screen application.

Use a named session when you maintain more than one conversation in a project:

```bash
heartwood --session-id cohort-review
```

## Submit a Request

Type the request at the prompt and press Enter.
While the model is working, Heartwood displays an animated status and elapsed time without inventing progress that the model service does not expose.
The full-screen interface can accept guidance for the active task and exposes pause as soon as OpenHands is running.
Task Tracker progress, model activity, and sequential specialist status appear when OpenHands supplies them.

For one non-interactive request:

```bash
heartwood --prompt "Inspect this project and summarize its test failures. Do not modify files."
```

## Review Actions

The default mode is **Review Every Action**.
When OpenHands proposes an action set, the interface lists every member and makes clear that allowing runs the complete set once while rejecting runs none of it.

Use arrow keys and Enter in the full-screen interface.
In the plain interface, use `/allow` or `/reject`; you do not need to copy an internal tool-call identifier.

Press `Ctrl-P` to open the command palette for action review, status, replay, and audit export.

## Conversation Commands

| Command | Result |
|---|---|
| `/help` | Show available conversation commands |
| `/status` | Show model, credential, policy, and action-review status |
| `/permissions` | Review or change when Heartwood pauses before actions |
| `/allow` | Allow the complete pending action set once |
| `/reject` | Reject the complete pending action set |
| `/pause` | Pause active OpenHands work |
| `/resume` | Resume a paused session |
| `/replay` | Render the persisted session events |
| `/audit-export` | Write a scrubbed JSON Lines audit export |
| `/exit` | Close the interface without deleting the session |

## Plain Terminal

```bash
heartwood --plain
```

The plain interface retains grouped action review, progress messages, replay, and audit export.
It omits the full-screen layout and keyboard-driven selection controls.

## Stop and Return

Use `/exit` or `Ctrl-C` to close the interface.
Run the same command from the same project to resume; select the same `--session-id` when you used a named session.
