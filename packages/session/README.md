<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# heartwood-session

The shared session command/event contract for Heartwood interfaces. The CLI, the notebook API, and future UI surfaces all drive the same session through this contract, so no interface owns separate execution semantics.

See [`design/03-architecture.md`](../../design/03-architecture.md) and [`design/09-implementation-plan.md`](../../design/09-implementation-plan.md).
