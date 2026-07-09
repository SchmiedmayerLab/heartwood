<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# heartwood-core-adapter

Core harness orchestration for Heartwood sessions.

This package defines the stable event-streaming execution facade used by the session service. The first implementation is deterministic and offline so tests can exercise commands, event persistence, policy decisions, confirmation gates, and audit logging without a live model or network dependency.
