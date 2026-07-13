# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Compatibility import for model-snapshot verification owned by the gateway."""

from __future__ import annotations

from heartwood.gateway import verify_model_snapshot as verify_snapshot

__all__ = ["verify_snapshot"]
