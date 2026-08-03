<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Heartwood Persistence

Shared schema-migration, private-file, native-lock, atomic-write, and recoverable JSON Lines primitives for Heartwood state.

Domain packages retain ownership of their record validation and recovery policy.
This package supplies the filesystem and compatibility mechanisms used by those owners.
