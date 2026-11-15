<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Heartwood Model Policy

Deny-by-default model-call policy evaluation for Heartwood.

The package evaluates proposed model profiles against a `PolicyProfile`, requires exact matches for the declared normalized policy endpoint, enforces allowed capability tiers and non-secret credential references, restricts project action-review modes to the deployment ceiling, and produces application-layer decision and attestation records without credential values. Platform network controls remain authoritative for actual traffic.
