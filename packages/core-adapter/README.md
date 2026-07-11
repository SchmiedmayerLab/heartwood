<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Heartwood Core Adapter

Core harness orchestration for Heartwood sessions.

This package defines the stable event-streaming execution facade used by the session service. The deterministic backend keeps tests and replay offline, while runtime packages can inject local or OpenHands-backed implementations behind the same assistant-message, tool-proposal, confirmation, and tool-execution event contract.
