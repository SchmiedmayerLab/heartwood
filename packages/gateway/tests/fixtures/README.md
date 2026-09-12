<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# SDK Compatibility Fixtures

`openhands-1.41.0-pending.json` contains a synthetic `TestLLM` conversation captured with OpenHands SDK and tools 1.41.0.
The conversation stopped at `AlwaysConfirm` with one pending terminal action; no tool action was approved or executed.
Temporary paths are normalized to `/synthetic/project`, and the system-prompt text is replaced with a short synthetic instruction.
The base-state schema, event schemas, pending action, and Heartwood persistence marker retain their original structure.

The fixture verifies rejection before model or tool work and byte-preservation of the original records when no SDK migration is supported.
It is not evidence that the current runtime can resume a 1.41.0 conversation.
