# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Shared researcher-facing action terminology."""

from __future__ import annotations

import unicodedata

from heartwood.gateway._action_settings import ACTION_MODE_OPTIONS

ACTION_TOOL_LABELS = {
    "file_editor": "File Change",
    "terminal": "Terminal Command",
}
ACTION_RISK_LABELS = {
    "high": "High Risk",
    "low": "Low Risk",
    "medium": "Medium Risk",
    "unknown": "Not Classified",
}
UNKNOWN_ACTION_TOOL_LABEL = "Tool Action"
UNKNOWN_ACTION_RISK_LABEL = "Not Classified"
OTHER_ACTION_TOOL_LABEL_TEMPLATE = "{tool_name} Action"
ACTION_STATE_LABELS = {
    "approved": "Approved",
    "awaiting-review": "Awaiting Review",
    "failed": "Failed",
    "outcome-unknown": "Outcome Unknown",
    "proposed": "Proposed",
    "rejected": "Rejected",
    "running": "Running",
    "succeeded": "Succeeded",
}


def action_mode_label(value: object) -> str:
    """Return the researcher-facing label for an action-review mode."""
    return next(
        (option.label for option in ACTION_MODE_OPTIONS if option.mode == value),
        str(value),
    )


def action_tool_label(tool_name: str) -> str:
    """Return the researcher-facing label for an OpenHands tool."""
    return ACTION_TOOL_LABELS.get(
        tool_name,
        (
            OTHER_ACTION_TOOL_LABEL_TEMPLATE.format(tool_name=tool_name)
            if tool_name
            else UNKNOWN_ACTION_TOOL_LABEL
        ),
    )


def action_risk_label(risk: str) -> str:
    """Return the researcher-facing label for an action risk."""
    return ACTION_RISK_LABELS.get(risk.lower(), UNKNOWN_ACTION_RISK_LABEL)


def action_state_label(state: str) -> str:
    """Return the researcher-facing label for a projected action state."""
    return ACTION_STATE_LABELS.get(state, state.replace("-", " ").title())


def display_safe_text(value: object, *, preserve_newlines: bool = False) -> str:
    """Render control and formatting characters visibly in presentation adapters."""
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


def action_presentation() -> dict[str, object]:
    """Return non-secret terminology consumed by interface adapters."""
    return {
        "risk_labels": dict(ACTION_RISK_LABELS),
        "state_labels": dict(ACTION_STATE_LABELS),
        "tool_labels": dict(ACTION_TOOL_LABELS),
        "other_tool_label_template": OTHER_ACTION_TOOL_LABEL_TEMPLATE,
        "unknown_risk_label": UNKNOWN_ACTION_RISK_LABEL,
        "unknown_tool_label": UNKNOWN_ACTION_TOOL_LABEL,
    }


__all__ = [
    "ACTION_RISK_LABELS",
    "ACTION_STATE_LABELS",
    "ACTION_TOOL_LABELS",
    "OTHER_ACTION_TOOL_LABEL_TEMPLATE",
    "UNKNOWN_ACTION_RISK_LABEL",
    "UNKNOWN_ACTION_TOOL_LABEL",
    "action_mode_label",
    "action_presentation",
    "action_risk_label",
    "action_state_label",
    "action_tool_label",
    "display_safe_text",
]
