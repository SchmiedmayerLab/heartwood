# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Platform adapter implementations."""

from __future__ import annotations

from collections.abc import Mapping

from heartwood.adapters import PlatformAdapter
from heartwood.adapters.platform.carina import CarinaPlatformAdapter
from heartwood.adapters.platform.generic import GenericPlatformAdapter
from heartwood.detector import Platform, detect_platform

__all__ = ["CarinaPlatformAdapter", "GenericPlatformAdapter", "select_platform_adapter"]


def select_platform_adapter(env: Mapping[str, str]) -> PlatformAdapter:
    """Select the implemented adapter from deterministic environment evidence."""
    if detect_platform(env).platform is Platform.CARINA:
        return CarinaPlatformAdapter()
    return GenericPlatformAdapter()
