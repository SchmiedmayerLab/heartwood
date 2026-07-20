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

The full-screen interface provides command history, arrow-key navigation, colored status, elapsed-time activity, and an action-review panel.
Heartwood automatically falls back to the plain interface when the terminal cannot support the full-screen application.

Use a named session when you maintain more than one conversation in a project:

```bash
heartwood --session-id cohort-review
```

## Submit a Request

Type the request at the prompt and press Enter.
While the model is working, Heartwood displays an animated status and elapsed time without inventing progress that the model service does not expose.

For one non-interactive request:

```bash
heartwood --prompt "Inspect this project and summarize its test failures. Do not modify files."
```

## Review Actions

The default policy is **Ask Every Time**.
When OpenHands proposes an action set, the interface lists every member and offers one **Allow all once** or **Reject all** decision because the current OpenHands confirmation callback resolves the group together.

Use arrow keys and Enter in the full-screen interface.
In the plain interface, use `/allow` or `/reject`; you do not need to copy an internal tool-call identifier.

## Conversation Commands

| Command | Result |
|---|---|
| `/help` | Show available conversation commands |
| `/status` | Show model, credential, policy, and action-confirmation status |
| `/allow` | Allow the complete pending action set once |
| `/reject` | Reject the complete pending action set |
| `/pause` | Pause the current session |
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
