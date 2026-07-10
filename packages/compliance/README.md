<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Heartwood Compliance

Synthetic-only deployment evidence and audit bundle generation for Heartwood. Run `heartwood reviewer packet` to create a deterministic set containing the active synthetic policy profile, sample egress attestation, scrubbed audit export, dependency summary, and current limitations.

The package reads checked-in synthetic fixtures and scrubbed session audit logs, validates policy and attestation records through the shared schemas, and writes a deterministic evidence aid. The packet does not approve a deployment or replace platform, institutional, security, privacy, clinical, or statistical review.
