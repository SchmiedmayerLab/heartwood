# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Apply the reviewed Transformers compatibility hook in every vLLM process."""

from __future__ import annotations

from heartwood_vllm import apply_transformers_compatibility

apply_transformers_compatibility()
