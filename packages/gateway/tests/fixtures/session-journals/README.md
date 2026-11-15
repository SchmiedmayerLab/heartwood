<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Session Journal Replay Fixtures

Each file holds a synthetic session journal, as the session store records its events, and the state replaying it must give: the interface projection (`project_session`) and the service reduction (`session_activity`).
`test_session_journal_replay.py` replays every journal and compares both with `expected`.
The journals contain no project content; the outside operation's identifiers and digests are fixed placeholders.

Releases up to 0.5.0 reduced the projection separately from the service.
One journal replays to the same state as in 0.5.0; every other journal records one intended correction.
The 0.5.0 service reduction had no `open_reviews` or `retained_approvals`; the table compares only what both report.

| Journal | 0.5.0 replay | Expected replay and why |
|---|---|---|
| `pause-then-resume` | Running; `chat`, `pause` | Unchanged: a recorded resume ends the pause |
| `backend-report-after-pause` | Projection `finished`, not paused, only `chat`; the service still paused | Paused with `chat`, `resume` in every interface: a backend report never ends a researcher pause |
| `review-rejected-during-pause` | Projection `idle`, not paused, only `chat`; the service still paused | Paused with `chat`, `resume`: a rejection does not continue the agent, so the earlier pause stays in effect |
| `refused-chat-during-pause` | Projection paused; the service no longer paused | The service stays paused too: a refused command does not continue the agent, so automatic review stays held and replacement blocked |
| `policy-change-before-refused-turn` | Projection `error` with only `chat`; the service no longer paused | Paused with `chat`, `resume` in the projection and the service: a policy change recorded before a refused turn does not continue the agent |
| `unrecoverable-open-review` | Waiting for review with `approve`, `deny` | `error` with no command: the gateway refuses every decision once the session cannot continue |
| `retained-outside-approval` | Running with `chat`, `pause` and no review | Waiting for review with the retained approval (`decision: "approved"`) and only `deny`, whatever the backend reports: the approval was recorded but not sent, and can only be resent or rejected |
