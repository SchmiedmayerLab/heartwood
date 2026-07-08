<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# heartwood-audit

Hash-chained audit logging for Heartwood sessions.

The package persists versioned `AuditEvent` records as newline-delimited JSON, computes deterministic event hashes, and verifies existing logs before resume or export. It stores only structured event metadata; callers decide which payload fields are safe to record.
