<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Heartwood Detector

Deterministic, propose-not-commit platform detection. The probes inspect environment markers only — no model call, participant-level data, or side effects — and return a platform proposal with confidence and evidence.

The integration fixture supplies a synthetic OMOP fingerprint through a data-source adapter. A normal runtime must report no dataset until a real adapter supplies evidence. The durable detection contract is defined in [04 — Skills And Auto-Detection](../../design/04-skills.md), and replacement of the implicit fixture is tracked in [Issue #37](https://github.com/SchmiedmayerLab/heartwood/issues/37).
