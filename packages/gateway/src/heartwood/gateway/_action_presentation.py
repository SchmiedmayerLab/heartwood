# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Shared researcher-facing action terminology."""

from __future__ import annotations

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


def action_presentation() -> dict[str, object]:
    """Return non-secret terminology consumed by interface adapters."""
    return {
        "risk_labels": dict(ACTION_RISK_LABELS),
        "tool_labels": dict(ACTION_TOOL_LABELS),
        "other_tool_label_template": OTHER_ACTION_TOOL_LABEL_TEMPLATE,
        "unknown_risk_label": UNKNOWN_ACTION_RISK_LABEL,
        "unknown_tool_label": UNKNOWN_ACTION_TOOL_LABEL,
    }


__all__ = [
    "ACTION_RISK_LABELS",
    "ACTION_TOOL_LABELS",
    "OTHER_ACTION_TOOL_LABEL_TEMPLATE",
    "UNKNOWN_ACTION_RISK_LABEL",
    "UNKNOWN_ACTION_TOOL_LABEL",
    "action_mode_label",
    "action_presentation",
    "action_risk_label",
    "action_tool_label",
]
