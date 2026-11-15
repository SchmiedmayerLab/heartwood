# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Shared researcher-facing action terminology."""

from __future__ import annotations

import json
import unicodedata
from typing import TYPE_CHECKING

from heartwood.gateway._action_settings import ACTION_MODE_OPTIONS

if TYPE_CHECKING:
    from heartwood.gateway._session_projection import (
        ProjectionActionRecord,
        ProjectionOutsideContent,
    )

ACTION_TOOL_LABELS = {
    "file_editor": "File Change",
    "task": "Specialist Review",
    "terminal": "Terminal Command",
}
ACTION_RISK_LABELS = {
    "high": "Advisory High Risk",
    "low": "Advisory Low Risk",
    "medium": "Advisory Medium Risk",
    "unknown": "Not Classified",
}
OUTSIDE_REVIEW_TITLES = {
    "outside-operation": "Outside Operation",
    "outside-result": "Outside Result Release",
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
    fallback = display_safe_text(state.replace("-", " ").title())
    return ACTION_STATE_LABELS.get(state, fallback)


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


def action_summary_label(action: ProjectionActionRecord) -> str:
    """Name the author of an action summary so trusted text is not read as model text."""
    return (
        "Gateway-provided summary"
        if action.summary_source == "gateway"
        else "Model-provided summary"
    )


def outside_review_title(action: ProjectionActionRecord) -> str | None:
    """Return the title of a gateway-issued outside review, or None for a sandbox action."""
    return OUTSIDE_REVIEW_TITLES.get(action.details.kind)


def action_heading(action: ProjectionActionRecord) -> str:
    """Head an action with its command, path or trusted name, labelling any model-written text."""
    details = action.details
    if details.kind == "terminal":
        heading = f"$ {details.command}"
    elif details.kind == "file-editor":
        heading = f"{details.operation} {details.path or 'path unavailable'}"
    elif details.kind == "task" and details.role_label:
        heading = details.role_label
    elif details.kind == "task" and details.description:
        heading = f"Model-provided task description: {details.description}"
    elif details.kind == "task" and details.subagent_type:
        heading = f"Model-provided specialist name: {details.subagent_type}"
    elif title := outside_review_title(action):
        heading = f"{title}: {action.summary}"
    elif action.summary:
        heading = f"{action_summary_label(action)}: {action.summary}"
    else:
        heading = action_tool_label(action.tool_name)
    return display_safe_text(heading)


def specialist_review_lines(action: ProjectionActionRecord) -> tuple[str, ...]:
    """Name a specialist task's catalog role and label each field the model wrote."""
    details = action.details
    if details.kind != "task":
        return ()
    lines: list[str] = []
    if details.role_label:
        lines.append(f"Specialist: {details.role_label}")
    elif details.subagent_type:
        lines.append(f"Model-provided specialist name: {details.subagent_type}")
    if details.capability:
        lines.append("Capability: " + details.capability.replace("-", " ").title())
    if details.description:
        lines.append(f"Model-provided task description: {details.description}")
    if details.prompt:
        lines.append(f"Model-provided task prompt: {details.prompt}")
    return tuple(display_safe_text(line, preserve_newlines=True) for line in lines)


def _outside_content_identity(content: ProjectionOutsideContent) -> str:
    return f"{content.size_bytes:,} bytes of canonical JSON · SHA-256 {content.sha256}"


def outside_review_lines(action: ProjectionActionRecord) -> tuple[str, ...]:
    """State what an outside review sends and where; empty for a sandbox action."""
    details = action.details
    if details.kind == "outside-operation":
        lines = [
            "Leaves the sandbox",
            f"Service: {details.service_id}",
            f"Destination: {details.destination}",
            f"Operation: {action.tool_name}",
            *(
                f"Input: {item.reference} · {item.size_bytes:,} bytes · SHA-256 {item.sha256}"
                for item in details.inputs
            ),
            f"Request sent to the service: {_outside_content_identity(details.request)}",
        ]
    elif details.kind == "outside-result":
        lines = [
            "Leaves Heartwood for the model route",
            "Model route: " + json.dumps(details.model_route, sort_keys=True),
            "Content sent to the worker and its model: "
            + _outside_content_identity(details.content),
        ]
    else:
        return ()
    lines.append(f"Review valid until: {details.expires_at}")
    return tuple(display_safe_text(line) for line in lines)


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
    "action_heading",
    "action_mode_label",
    "action_presentation",
    "action_risk_label",
    "action_state_label",
    "action_summary_label",
    "action_tool_label",
    "display_safe_text",
    "outside_review_lines",
    "outside_review_title",
    "specialist_review_lines",
]
