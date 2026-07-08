<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Heartwood Fixtures

Synthetic fixture linting for Heartwood test data and replay artifacts.

The linter is intentionally narrow in Phase 0B: it catches direct identifiers, common secret shapes, live-data markers, and non-synthetic source markers before fixtures are used by adapter conformance tests, replay tests, or audit examples.
