<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# heartwood-detector

Deterministic, propose-not-commit environment and dataset detection. The probes inspect environment markers and data fingerprints only — no model call, no participant-level data, no side effects — and return a *proposal* (platform, confidence, evidence) for a human to confirm.

This package currently implements platform detection. Dataset fingerprinting and skill selection follow (see [`design/04-skills.md`](../../design/04-skills.md)).
