# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Heartwood visual identity: shared color tokens and the terminal form of the mark."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TextIO

NAME = "Heartwood"
TAGLINE = "Auditable agentic coding for biomedical research"


@dataclass(frozen=True, slots=True)
class Palette:
    """Identity colors for one appearance; the browser and documentation use the same values."""

    primary: str
    primary_foreground: str
    surface: str
    foreground: str
    muted_foreground: str
    warning: str


LIGHT = Palette(
    primary="#0b694d",
    primary_foreground="#ffffff",
    surface="#f7f7f8",
    foreground="#1d1d1f",
    muted_foreground="#66666b",
    warning="#9a4a00",
)

DARK = Palette(
    primary="#4cc792",
    primary_foreground="#07140e",
    surface="#0a0a0a",
    foreground="#f5f5f7",
    muted_foreground="#98989d",
    warning="#ff9f0a",
)

MARK_TILE = ("#0b694d", "#084d39")
MARK_RING = "#d8ebe0"
MARK_CORE = "#7ad8b0"

TERMINAL_MARK = "◉"
PROGRESS_FRAMES = ("◌", "○", "◎", "◉")
ASCII_PROGRESS_FRAMES = (".  ", ".. ", "...")


def supports_unicode(stream: TextIO) -> bool:
    """Return whether a stream can show the mark glyphs rather than their ASCII fallback."""
    encoding = (getattr(stream, "encoding", None) or "").lower().replace("-", "")
    return encoding == "utf8" and os.environ.get("TERM", "").lower() != "dumb"


def lockup(stream: TextIO) -> str:
    """Return the product name with the terminal mark when the stream can render it."""
    return f"{TERMINAL_MARK} {NAME}" if supports_unicode(stream) else NAME


def progress_frames(stream: TextIO) -> tuple[str, ...]:
    """Return growth-ring frames where the stream can render them, otherwise ASCII dots."""
    return PROGRESS_FRAMES if supports_unicode(stream) else ASCII_PROGRESS_FRAMES


def progress_line(label: str, frame: str) -> str:
    """Place a ring frame before the label and an ASCII frame after it."""
    return f"{frame} {label}..." if frame in PROGRESS_FRAMES else f"{label}{frame}"
