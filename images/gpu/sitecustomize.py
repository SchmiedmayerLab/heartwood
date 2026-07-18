# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Apply Heartwood's reviewed vLLM boundary in spawned Python interpreters."""

from __future__ import annotations

from heartwood_vllm import activate_runtime_boundary

activate_runtime_boundary()
