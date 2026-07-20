<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Heartwood Detector

Deterministic platform detection from content-safe environment markers.
The result selects the matching platform adapter but never grants data or model authorization.

The integration fixture supplies a synthetic OMOP fingerprint through a data-source adapter.
A normal runtime reports no dataset until a deployment adapter supplies explicit evidence.
See [System Architecture](../../documentation/architecture/system.md).
