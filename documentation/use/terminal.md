<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Use the Terminal

The terminal works on every supported platform and is the only interface on Stanford Carina.

## Start a Session

```bash
cd /path/to/project
heartwood
```

Type a request at the prompt and press Enter.
While the model works, Heartwood shows an animated status and the elapsed time; it does not invent progress the model service does not report.
Task Tracker progress, model activity, and specialist status appear when OpenHands supplies them.

When the agent proposes an action set, the interface lists every member and waits for your decision.
Use the arrow keys and Enter to allow or reject the complete set; see [Review an Action Set](actions-audit.md#review-an-action-set).
After the work settles, Heartwood may offer up to two suggested next requests.

Heartwood falls back to the plain interface when the terminal cannot run the full-screen application.

## Launch Options

| Command | Result |
|---|---|
| `heartwood --session-id cohort-review` | Open or resume a named session instead of the main session |
| `heartwood --plain` | Use the line-oriented interface, for logs, basic terminals, and accessibility tools |
| `heartwood --review-mode review-changes` | Start with a [review mode](actions-audit.md#review-modes) selected |
| `heartwood --prompt "..."` | Submit one request, wait for it, print the result, and exit |

For example:

```bash
heartwood --prompt "Inspect this project and summarize its test failures. Do not modify files."
```

## Steer Active Work

While OpenHands runs, you can type further guidance for the active task or pause it.
`/pause` pauses the run and `/resume` continues it.
Pausing also stops automatic review decisions until you continue.

Enter `/permissions` to see which actions Heartwood allows without asking, or `/permissions MODE` to change it.
Actions allowed under a review rule are shown as allowed automatically, with the rule.

## Inspect Files and Changes

The full-screen interface has three views:

| Shortcut | View |
|---|---|
| `Ctrl-1` | Conversation and action review |
| `Ctrl-2` | Project tree and read-only text files |
| `Ctrl-3` | Changed files and read-only per-file diffs |

`Ctrl-P` opens the command palette for these views, action review settings, status, replay, and audit export.

The Files view excludes `.heartwood/` and `.git/`, does not follow symbolic links, and labels binary, unsupported, or truncated files.
In a Git repository, the Changes view shows the Git changes.
In a project without Git, it shows only successful file changes from typed file-editor actions in the selected session; it does not infer changes from terminal commands.

## Conversation Commands

| Command | Result |
|---|---|
| `/help` | Show the available commands |
| `/status` | Show model, credential, policy, and action-review status |
| `/permissions [MODE]` | Show or select the review mode: `review-all`, `review-changes`, or `review-outside-effects` |
| `/allow` | Allow the complete pending action set once |
| `/reject` | Reject the complete pending action set, or withdraw an approval that was recorded but not sent |
| `/retry` | Resend the approval this terminal recorded but could not send, as the same command |
| `/pause` | Pause active work |
| `/resume` | Resume a paused session |
| `/specialists` | Show the [research specialists](specialists.md) available to the agent |
| `/workflows` | List the [research workflows](research-workflows.md) |
| `/workflow` | Set up a research workflow or show its current stage and next commands |
| `/experiments` | Show the experiment records of this session |
| `/files [DIRECTORY]` | List the project tree |
| `/show FILE` | Show one UTF-8 project file |
| `/changes [FILE]` | List changed paths or show one read-only diff |
| `/replay` | Show the persisted session history |
| `/audit-export` | Write a scrubbed JSON Lines audit export |
| `/exit` | Close the interface without deleting the session |

In the plain interface, use these commands in place of keyboard selection; `/allow` and `/reject` apply to the set shown, without a tool-call identifier.

## Stop and Return

Use `/exit` to close the interface, or `Ctrl-Q` in the full-screen interface and `Ctrl-C` in the plain one.
Run the same command in the same project to resume, with the same `--session-id` if you used one.
A review mode selected during the session is not kept; the next launch starts at the default.

Only one process writes a session at a time.
Close the terminal before continuing the same session in the browser or a notebook, or use a different session identifier there.
