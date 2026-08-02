<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Heartwood Audit

Hash-chained audit logging for Heartwood sessions.

The package persists versioned `AuditEvent` records as recoverable JSON Lines, computes deterministic event hashes, scrubs sensitive payload fields, and fully verifies logs before replay or export.
It also creates and verifies canonical Ed25519-signed checkpoints for deployment-owned retention outside an agent project.

Domain callers remain responsible for emitting only the minimum operational metadata needed for review.
