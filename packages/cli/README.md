<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# heartwood-cli

The `heartwood` command-line interface — the primary interaction surface and the stable target for CI. Interfaces are thin presentations over shared core logic, so the CLI and the notebook adapter drive the same session without a second execution path.

Today it exposes environment detection (`heartwood detect`, propose-not-commit). The agentic session commands (`chat`, `run`, `replay`, `audit export`) arrive with the session contract in later phases — see [`design/09-implementation-plan.md`](../../design/09-implementation-plan.md).
