<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Heartwood CLI

The `heartwood` command-line interface is the primary interactive, scripting, and CI surface. Running `heartwood` or `heartwood chat` in a capable terminal opens a full-screen conversation with persisted replay, keyboard navigation, background task execution, action review, and session status. Use `heartwood chat --plain` for a line-oriented SSH or basic-terminal session and `heartwood chat --prompt "..."` for one task. Action allow or reject, the deployment-allowed OpenHands confirmation mode, pause, resume, replay, audit export, model catalog selection, reviewed artifact downloads, and web serving use the same gateway and session contract as the notebook and web interfaces.

The terminal interface uses Textual for presentation only. OpenHands remains responsible for the agent loop, coding tools, conversation persistence, action analysis, and confirmation behavior through the gateway adapter. The current gateway returns each turn as a completed event batch, so the terminal stays responsive while work runs but does not claim token streaming or mid-turn cancellation.

`heartwood models list`, `models refresh <connection-id>`, and `models connect <connection-id> <model-id>` expose the same normalized local, platform, cloud, and custom catalogs as the web UI. Model commands persist only provider identifiers, endpoints, capability tiers, and credential references. Secret values remain in environment variables, mounted files, or managed identity; the CLI has no token argument. Raw profile commands remain available for advanced deployment compatibility.
