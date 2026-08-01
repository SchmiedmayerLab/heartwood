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

The full-screen interface provides arrow-key navigation, colored status, elapsed-time activity, grouped action review, and read-only project inspection.
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
When the session reaches an interactive boundary, Heartwood may show a small set of gateway-owned next-step suggestions that can be reviewed before use.
Status, task, usage, specialist, and suggestion labels come from the same projection used by the browser and notebook bridge.

For one non-interactive request:

```bash
heartwood --prompt "Inspect this project and summarize its test failures. Do not modify files."
```

## Review Actions

The default mode is **Review Every Action**.
When OpenHands proposes an action set, the interface lists every member and makes clear that allowing runs the complete set once while rejecting runs none of it.

Use arrow keys and Enter in the full-screen interface.
In the plain interface, use `/allow` or `/reject`; you do not need to copy an internal tool-call identifier.

After execution, **Agent actions** correlates each proposal with its decision, state, exit status, bounded output, and explicitly attributed project paths.
An interrupted action whose result cannot be established is marked **outcome unknown** rather than treated as safe to repeat.

## Inspect Files and Changes

Use the three full-screen views without leaving the session:

| Shortcut | View |
|---|---|
| `Ctrl-1` | Conversation and grouped action review |
| `Ctrl-2` | Bounded project tree and read-only text files |
| `Ctrl-3` | Changed files and read-only per-file diffs |

The Files view excludes `.heartwood/` and `.git/`, does not follow symbolic links, and reports binary, unsupported, or truncated files explicitly.
The Changes view uses the OpenHands Git workspace when the project is a Git repository.
For a non-Git project, it shows only successful file changes that came from typed OpenHands file-editor actions in the selected session; it does not infer changes from terminal commands.

Press `Ctrl-P` to open the command palette for these views, action review, status, replay, and audit export.

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
| `/files [DIRECTORY]` | List the bounded project tree |
| `/show FILE` | Show one bounded UTF-8 project file |
| `/changes [FILE]` | List changed paths or show one read-only diff |
| `/replay` | Render the persisted session events |
| `/audit-export` | Write a scrubbed JSON Lines audit export |
| `/exit` | Close the interface without deleting the session |

## Plain Terminal

```bash
heartwood --plain
```

The plain interface retains grouped action review, progress messages, replay, and audit export.
It omits the full-screen layout and keyboard-driven selection controls.
Use `/files`, `/show`, and `/changes` for the same gateway-owned workspace evidence.

## Stop and Return

Use `/exit` or `Ctrl-C` to close the interface.
Run the same command from the same project to resume; select the same `--session-id` when you used a named session.
