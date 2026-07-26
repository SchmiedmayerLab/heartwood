<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Use the Browser

The browser interface presents conversations, action review, model setup, Skills, activity, and audit export without introducing a separate backend or project state.
It is available on workstations and in the generic container.
Terra and Stanford Carina do not expose a supported Heartwood browser route; use their terminal or notebook interfaces instead.

## Open the Interface

From the project directory, run:

```bash
heartwood --interface web
```

Keep the terminal process running and open the exact URL Heartwood prints.
On a workstation the default is `http://127.0.0.1:8767/`.

![Heartwood browser interface showing a project conversation](../assets/screenshots/browser-conversation.png)

## First Use

Opening the page is read-only until you select **Use this project**.
The setup panel then presents model sources available in the detected environment, models returned by the selected service, and credential handling supported by the platform.

The project, model selection, and action-review setting are shared with the terminal and notebook bridge.
Provider API keys and subscription tokens are never stored in browser storage.

For **Sign in with ChatGPT**, select the connection and choose **Sign in with ChatGPT**.
Open the displayed OpenAI page, enter the one-time code, and return to Heartwood.
The page updates when OpenHands has stored the account credential; you can then load the supported model list and make a selection.
Use **Sign out** in the same panel to remove the account from the OpenHands credential store.

If you download or import a model for Heartwood to run, wait for **Downloaded. Restart Heartwood to load this model.**
Stop the launching command with `Ctrl-C`, then run `heartwood --interface web` again from the same project.
Heartwood starts and supervises the selected model before reopening the page.
Hosted and Stanford AI API Gateway connections do not require this restart.

## Work With a Session

The first browser conversation is the same **Main session** used by the terminal and notebook defaults. Choose a named session explicitly when you want a separate conversation.

- Use **New analysis** to create another persistent session.
- Enter requests in the composer after model readiness is confirmed.
- Open **Activity & audit** to inspect route decisions, tool results, and errors.
- Open **Skills** to inspect repository-verified and installed Skills.
- Select the current **Action review** value in the session header to change when Heartwood pauses.
- Open **Settings** to change the selected model or action-review mode.
- Export the audit record from the session controls.

## Review an Action Set

![Heartwood action review showing grouped proposed operations](../assets/screenshots/browser-action-review.png)

The review panel lists all proposed members together with tool names, risk labels, summaries, and relevant arguments.
One decision resolves the complete OpenHands action set: allowing runs every listed action once, while rejecting runs none of them.

## Keep the Interface Reachable

Bind Heartwood to loopback unless a trusted authenticated proxy terminates access.
The development server and generic container do not add user authentication by themselves.

If the page loads but requests fail, keep the launching terminal open and run `heartwood doctor` in another terminal from the same project.
See [Diagnostics and Troubleshooting](../reference/troubleshooting.md#browser-access).
