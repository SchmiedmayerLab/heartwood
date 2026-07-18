# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Deterministic environment and dataset detection for Heartwood.

Detection proposes; a human confirms. Nothing here loads a skill or runs code.
See ``design/04-skills.md``.
"""

from __future__ import annotations

from heartwood.detector._platforms import (
    Platform,
    PlatformDetection,
    detect_platform,
    platform_detection_evidence,
)

__all__ = [
    "Platform",
    "PlatformDetection",
    "__version__",
    "detect_platform",
    "platform_detection_evidence",
]

__version__ = "0.2.0-beta.3"
