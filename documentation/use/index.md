<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Work With Heartwood

Heartwood runs an OpenHands coding agent on your project.
The gateway decides which proposed actions need your review and records every request, decision, and result.

## The Core Workflow

1. Open the folder that contains only the files Heartwood may use.
2. Run `heartwood` for the terminal or `heartwood --interface web` for the browser.
3. Describe the outcome: what should change, what must stay untouched, and how to check the result.
4. Review each proposed action set and allow or reject it; see [Actions and Audit History](actions-audit.md).
5. Ask the agent to run bounded checks, then inspect the resulting files or diff yourself.
6. Resume the session later, replay its history, or export the scrubbed audit record.

Started this way, Heartwood runs on your machine without an isolated worker: allowed commands run as your user, with that user's file and network access unless the platform restricts it.
In an operator-managed deployment, the agent can run in an isolated worker and protected state lives outside the project; see [Security and Controlled Data](../operate/security.md).
Heartwood is qualified with synthetic data only and does not claim readiness for sensitive data; that decision belongs to your institution and deployment.

## Write Effective Requests

Name the artifact you want, the input paths, the constraints, and how to verify the result:

```text
Read analysis/cohort.py and tests/test_cohort.py. Correct the date-window logic without changing the public function signature. Run only the cohort tests and summarize the diff and test result.
```

For a staged tabular analysis with fixed inputs and completion checks, use a [research workflow](research-workflows.md).
To get a focused second opinion on a plan or result, ask the agent to involve a [research specialist](specialists.md).

A request is not a security control.
If a task must not use the network, parent directories, or particular files, say so and use a deployment that enforces it.

## Choose an Interface

| Interface | Use For | Start With |
|---|---|---|
| [Full-screen terminal](terminal.md) | Daily work and SSH sessions | `heartwood` |
| [Plain terminal](terminal.md#choose-how-heartwood-starts) | Logs, basic terminals, and accessibility tools | `heartwood --plain` |
| [Browser](browser.md) | Visual history, setup, review, Skills, and audit export | `heartwood --interface web` |
| [Notebook bridge](notebooks.md) | Driving a session beside exploratory analysis | `NotebookSession()` |

Every interface uses the project in the process's current directory and the same sessions.
Only one process writes a session at a time: close one interface before continuing the same session in another, or use separate session identifiers for simultaneous work.

## Verify Before Trusting the Result

An agent response is not evidence that an analysis is correct.
Inspect code and outputs, run appropriate tests, keep the original data, and apply the same scientific and statistical review as for work you wrote yourself.
Heartwood records actions and route decisions; it does not validate a scientific conclusion.
