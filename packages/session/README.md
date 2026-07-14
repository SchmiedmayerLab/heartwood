<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Heartwood Session

The shared session command/event contract for Heartwood interfaces. The CLI, notebook API, web UI, scripts, and tests drive the same session through this contract, so no interface owns separate execution semantics.

See [`design/03-architecture.md`](../../design/03-architecture.md) and the [Heartwood issue tracker](https://github.com/SchmiedmayerLab/heartwood/issues).
