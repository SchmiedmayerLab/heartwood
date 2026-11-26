<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Use the Browser

The browser interface is available on workstations and in the generic container.
Terra and Stanford Carina do not expose a supported browser route; use the terminal or notebook on Terra and the terminal on Stanford Carina.

## Open the Interface

From the project directory, run:

```bash
heartwood --interface web
```

Keep the terminal process running and open the launch link Heartwood prints.
The link works once: it authorizes that browser tab for the running gateway, and the page continues at `http://127.0.0.1:8767/` on a workstation.
Reloading the tab keeps access; after restarting Heartwood, open the new link it prints.

The page is read-only until you select **Use this project**.
Setup then offers the model sources available in this environment, the models the selected service returns, and the credential handling the platform supports.
See [Choose a Model](../models/index.md) for each source.

![Heartwood browser interface showing a completed request with its approved actions](../assets/screenshots/browser-conversation-light.png#gh-light-mode-only){ .theme-screenshot-light }
![Heartwood browser interface showing a completed request with its approved actions](../assets/screenshots/browser-conversation-dark.png#gh-dark-mode-only){ .theme-screenshot-dark }

## Send a Request and Review Its Actions

Type a request in the composer and send it.
The first browser conversation is the same **Main session** the terminal and notebook use by default; **New analysis** creates another persistent session.

When the agent proposes actions, the review panel lists every member with its exact command or file operation, risk label, and summary.
**Allow All Once** runs every listed action once; **Reject All** runs none of them.
Keyboard focus moves to a new review and returns to the composer after your decision.

![Heartwood action review showing two proposed terminal commands and the Allow All Once and Reject All controls](../assets/screenshots/browser-action-review-light.png#gh-light-mode-only){ .theme-screenshot-light }
![Heartwood action review showing two proposed terminal commands and the Allow All Once and Reject All controls](../assets/screenshots/browser-action-review-dark.png#gh-dark-mode-only){ .theme-screenshot-dark }

Completed actions appear under **Agent actions** with their state, exit status, bounded result, and the project paths a typed file operation changed.
An action that Heartwood allowed under a review rule is marked as allowed automatically, with the rule.

The current review mode is shown in the session header; select it, or open **Settings** and then **Action Review**, to change which actions Heartwood allows without asking.
Unavailable modes are listed with the reason.
See [Review Modes](actions-audit.md#review-modes).

Where a deployment provides [outside operations](actions-audit.md#outside-operations-and-result-releases), each review of an operation or result release has its own heading and a marked panel that shows what would be sent and where, without opening a disclosure.

## Work With a Session

- Send further guidance or pause while OpenHands is working.
- Select one of up to two suggested next steps to place its full request in the composer for editing; suggestions come from the session state, not from project data.
- Inspect task progress, model-call totals, and specialist status below the conversation.
- Open **Research** to run a [research workflow](research-workflows.md); **Project Experiment Records** there lists recorded workflow stages and scripts across the project, and **Research Planning** shows [plans against platform budgets](research-workflows.md#plan-against-platform-budgets) where the deployment provides them.
- Open **Specialists** to see the [research specialists](specialists.md) and **Skills** to see [Skills](skills.md).
- Open **Research services** to add, test, enable, disable, or disconnect [research MCP servers](../operate/platform-contract.md#research-mcp-servers); each server shows its destination, token state, reviewed tools and the tools enabled for new sessions.
- Open **Activity & audit** for route decisions, tool results, and errors, and use **Export audit** to download the scrubbed [audit export](actions-audit.md#audit-export).
- Use the appearance button in the session header to switch between light and dark mode.

Status labels are the same as in the other interfaces: ready, working, waiting for review, paused, complete, rejected, and recovery required.
When a request cannot be applied, the browser shows a separate notice without changing the session state.
OpenHands identifiers stay under **Technical details**.

Responses support headings, lists, tables, quotations, links, and code.
Heartwood treats every response as untrusted: raw HTML and unsafe links are removed, remote images are not loaded, invisible control characters are made visible, and very large responses are truncated for display.

## Inspect the Project

**Files** and **Changes** are read-only and never offer a way to edit around action review.

![Heartwood Files view showing the project tree and a syntax-highlighted file](../assets/screenshots/browser-files-light.png#gh-light-mode-only){ .theme-screenshot-light }
![Heartwood Files view showing the project tree and a syntax-highlighted file](../assets/screenshots/browser-files-dark.png#gh-dark-mode-only){ .theme-screenshot-dark }

Select a changed path to compare it with the Git baseline.
In a project without Git, **Changes** shows only successful typed file-editor actions from the selected session; terminal command text is not treated as evidence of a change.

![Heartwood Changes view showing a Git-backed per-file diff](../assets/screenshots/browser-changes-light.png#gh-light-mode-only){ .theme-screenshot-light }
![Heartwood Changes view showing a Git-backed per-file diff](../assets/screenshots/browser-changes-dark.png#gh-dark-mode-only){ .theme-screenshot-dark }

Both views exclude `.heartwood/` and `.git/` at every depth, refuse path traversal, symbolic links, special files, and non-UTF-8 text, and apply fixed depth, count, line, and byte limits.
Truncated or unavailable content is labelled.

## Models That Need a Restart

**Sign in with ChatGPT** shows a one-time code; enter it on the OpenAI page, return to Heartwood, and the page updates when the account credential is stored.
**Sign out** in the same panel removes it.

After downloading or importing a model for Heartwood to run, wait for **Downloaded. Restart Heartwood to load this model.**, stop the launching command with `Ctrl-C`, and run `heartwood --interface web` again.
Hosted and Stanford AI API Gateway connections need no restart.
For an offline destination, **Move a model between environments** in model settings creates or inspects a verified model bundle at a path the gateway can read; follow [Move a Model Into an Offline Environment](../models/offline.md).

Provider API keys and subscription tokens are never stored in browser storage.
The browser stores only an explicit light or dark preference.

## Keep the Interface Reachable

Bind Heartwood to loopback unless a trusted authenticated proxy terminates access.
The launch link authorizes one browser for the running gateway; other users on the machine cannot send commands or approve actions without it.
It does not separate the gateway from commands that run as your own user, including approved agent commands, and your browser also sends it to other local servers on the same address, so open only local links you trust.
Operators configure the ingress mode, exact origin, base path, and trusted source boundary as described in [Security and Controlled Data](../operate/security.md#gateway-ingress).

| Problem | Next Step |
|---|---|
| The browser reports **HW-INGRESS-003** | Stop Heartwood with `Ctrl-C`, run it again, and open the new link |
| The page loads but requests fail | Keep the launching terminal open and run `heartwood doctor` from the same project in another terminal |
| Live updates stop | The page first falls back to another live transport; if both stop, the saved conversation stays visible and **Reconnect** appears |

See [Diagnostics and Troubleshooting](../reference/troubleshooting.md#browser-access).
