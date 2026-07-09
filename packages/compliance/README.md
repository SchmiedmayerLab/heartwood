<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# heartwood-compliance

Synthetic-only reviewer packet and audit bundle generation for Heartwood.

The package reads checked-in synthetic fixtures and scrubbed session audit logs, validates the policy and attestation records through the shared schemas, and writes a deterministic reviewer packet for Phase 0 review.
