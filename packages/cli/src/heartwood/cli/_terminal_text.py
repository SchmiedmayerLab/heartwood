# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Safe rendering of untrusted text in line and full-screen terminals."""

from __future__ import annotations

import unicodedata


def terminal_safe_text(value: object, *, preserve_newlines: bool = False) -> str:
    """Render control and formatting characters visibly instead of executing them."""
    rendered: list[str] = []
    for character in str(value):
        if character == "\n" and preserve_newlines:
            rendered.append(character)
            continue
        if unicodedata.category(character) not in {"Cc", "Cf", "Cs"}:
            rendered.append(character)
            continue
        codepoint = ord(character)
        if codepoint <= 0xFF:
            rendered.append(f"\\x{codepoint:02x}")
        elif codepoint <= 0xFFFF:
            rendered.append(f"\\u{codepoint:04x}")
        else:
            rendered.append(f"\\U{codepoint:08x}")
    return "".join(rendered)


__all__ = ["terminal_safe_text"]
