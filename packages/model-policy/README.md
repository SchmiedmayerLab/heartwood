<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# heartwood-model-policy

Deny-by-default model-call policy evaluation for Heartwood.

The package evaluates proposed model calls against a `PolicyProfile`, requires exact normalized endpoint matches, enforces allowed capability tiers, and produces both decision and attestation records.
