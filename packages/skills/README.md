<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Heartwood Skills

Local `SKILL.md` verification and deterministic skill test helpers for Heartwood.

The package validates checked-in skill directories before they can be loaded by the session harness. The Phase 0 implementation is intentionally offline: verified skills must declare no network requirement, carry `heartwood.*` metadata, expose an approval summary, and point to a root-confined script entry point.
