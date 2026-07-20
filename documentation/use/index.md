<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Work With Heartwood

Heartwood turns a request into an OpenHands coding-agent session with project boundaries, model-route policy, action review, persistent history, and audit records around it.
The normal workflow is the same in every supported interface.

## The Core Workflow

1. **Open the project.** Enter the folder containing only the files Heartwood may use.
2. **Start Heartwood.** Run `heartwood` for the terminal or `heartwood --interface web` for the browser.
3. **Describe an outcome.** State what should change, what must remain untouched, and how success can be verified.
4. **Watch progress.** Heartwood reports active work and elapsed time for longer model, download, and startup operations.
5. **Review the action set.** Inspect every proposed command or file operation before allowing the group.
6. **Verify the result.** Ask the agent to run bounded checks and inspect the resulting files or diff yourself.
7. **Continue or export.** Resume the session later, replay its events, or export the scrubbed audit record.

## Write Effective Requests

A useful request names the desired artifact, relevant input paths, constraints, and verification.

```text
Read analysis/cohort.py and tests/test_cohort.py. Correct the date-window logic without changing the public function signature. Run only the cohort tests and summarize the diff and test result.
```

Avoid granting access through prose that the platform should deny.
If a task should not use the network, parent directories, or particular files, state that constraint and use a deployment that enforces it.

## Choose an Interface

| Interface | Best For | Start With |
|---|---|---|
| Full-screen terminal | Daily interactive work and SSH sessions | `heartwood` |
| Plain terminal | Logs, basic terminals, and accessibility fallbacks | `heartwood --plain` |
| Browser | Visual session history, setup, action review, Skills, and audit export | `heartwood --interface web` |
| Notebook bridge | Programmatic interaction beside exploratory analysis | `NotebookSession()` |

All interfaces use the project selected by the process current directory.
They do not maintain separate browser, terminal, or notebook workspaces.
They can continue the same session sequentially, but one process should write a session at a time.
Stop the active writer before moving a session to another interface, or choose distinct session identifiers for simultaneous work.

## Verify Before Trusting the Result

An agent response is not evidence that an analysis is correct.
Inspect code and outputs, run appropriate tests, preserve the original data, and apply the same scientific and statistical review required for manually written work.

Heartwood records actions and route decisions, but it does not validate a scientific conclusion or replace peer review.
