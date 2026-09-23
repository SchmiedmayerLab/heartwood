# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Tests for the terminal form of the Heartwood identity."""

from __future__ import annotations

import io

import pytest

from heartwood.cli._brand import (
    ASCII_PROGRESS_FRAMES,
    PROGRESS_FRAMES,
    lockup,
    progress_frames,
    progress_line,
)


class _Stream(io.StringIO):
    def __init__(self, encoding: str | None) -> None:
        super().__init__()
        self._stream_encoding = encoding

    @property
    def encoding(self) -> str | None:  # type: ignore[override]
        return self._stream_encoding


def test_terminal_mark_falls_back_to_plain_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")
    assert lockup(_Stream("utf-8")) == "◉ Heartwood"
    assert lockup(_Stream("ascii")) == "Heartwood"
    assert lockup(_Stream(None)) == "Heartwood"
    assert progress_frames(_Stream("UTF-8")) == PROGRESS_FRAMES

    monkeypatch.setenv("TERM", "dumb")
    assert lockup(_Stream("utf-8")) == "Heartwood"
    assert progress_frames(_Stream("utf-8")) == ASCII_PROGRESS_FRAMES


def test_progress_line_places_ring_frames_before_the_label() -> None:
    assert progress_line("Checking the project", PROGRESS_FRAMES[2]) == "◎ Checking the project..."
    assert progress_line("Checking the project", ".. ") == "Checking the project.. "
    assert progress_line("Checking the project", "...") == "Checking the project..."
