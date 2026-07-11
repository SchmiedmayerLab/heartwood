<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Heartwood CLI

The `heartwood` command-line interface is the primary scripting and CI surface. Running `heartwood` or `heartwood chat` opens the coding-agent conversation; one-shot prompts, action allow or reject, the deployment-allowed OpenHands confirmation mode, pause, resume, replay, audit export, model catalog selection, reviewed artifact downloads, and web serving use the same gateway and session contract as the notebook and web interfaces.

`heartwood models list`, `models refresh <connection-id>`, and `models connect <connection-id> <model-id>` expose the same normalized local, platform, cloud, and custom catalogs as the web UI. Model commands persist only provider identifiers, endpoints, capability tiers, and credential references. Secret values remain in environment variables, mounted files, or managed identity; the CLI has no token argument. Raw profile commands remain available for advanced deployment compatibility.
