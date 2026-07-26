# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Shared projections derived from the canonical session event stream."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from pydantic import JsonValue

from heartwood.session._contracts import EventKind, SessionEvent

__all__ = ["PendingToolAction", "pending_tool_actions"]


@dataclass(frozen=True, slots=True)
class PendingToolAction:
    """One unresolved member of an agent confirmation batch."""

    request_id: str
    tool_call_id: str
    tool_name: str
    risk: str
    summary: str
    arguments: dict[str, JsonValue] = field(default_factory=dict)


def pending_tool_actions(events: Iterable[SessionEvent]) -> tuple[PendingToolAction, ...]:
    """Return unresolved tool actions in their original proposal order."""
    pending: dict[str, PendingToolAction] = {}
    for event in events:
        kind = str(event.kind)
        if kind == EventKind.CONFIRMATION_REQUESTED.value:
            request = event.payload.get("request")
            if not isinstance(request, dict):
                continue
            tool_call_id = request.get("tool_call_id")
            if not isinstance(tool_call_id, str) or not tool_call_id:
                continue
            raw_arguments = request.get("arguments")
            arguments = dict(raw_arguments) if isinstance(raw_arguments, dict) else {}
            pending[tool_call_id] = PendingToolAction(
                request_id=str(request.get("request_id", "")),
                tool_call_id=tool_call_id,
                tool_name=str(request.get("tool_name", "unknown-tool")),
                risk=str(request.get("risk", "unknown")),
                summary=str(request.get("summary", request.get("tool_name", "action"))),
                arguments=arguments,
            )
        elif kind == EventKind.CONFIRMATION_RESOLVED.value:
            tool_call_id = event.payload.get("tool_call_id")
            if isinstance(tool_call_id, str):
                pending.pop(tool_call_id, None)
    return tuple(pending.values())
