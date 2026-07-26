# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Tests for interface-neutral session projections."""

from __future__ import annotations

from heartwood.session import EventKind, JsonValue, SessionEvent, pending_tool_actions


def test_pending_tool_actions_preserve_unresolved_batch_members() -> None:
    events = (
        _event(
            sequence=1,
            kind=EventKind.CONFIRMATION_REQUESTED,
            payload={
                "request": {
                    "request_id": "request-1",
                    "tool_call_id": "tool-1",
                    "tool_name": "terminal",
                    "risk": "low",
                    "summary": "Create the result",
                    "arguments": {"command": "touch result.txt"},
                }
            },
        ),
        _event(
            sequence=2,
            kind=EventKind.CONFIRMATION_REQUESTED,
            payload={
                "request": {
                    "request_id": "request-2",
                    "tool_call_id": "tool-2",
                    "tool_name": "file_editor",
                }
            },
        ),
        _event(
            sequence=3,
            kind=EventKind.CONFIRMATION_RESOLVED,
            payload={"tool_call_id": "tool-1", "decision": "approved"},
        ),
    )

    actions = pending_tool_actions(events)

    assert len(actions) == 1
    assert actions[0].tool_call_id == "tool-2"
    assert actions[0].summary == "file_editor"
    assert actions[0].arguments == {}


def test_pending_tool_actions_ignore_malformed_confirmation_payloads() -> None:
    actions = pending_tool_actions(
        (
            _event(
                sequence=1,
                kind=EventKind.CONFIRMATION_REQUESTED,
                payload={"request": "not-an-object"},
            ),
        )
    )

    assert actions == ()


def _event(
    *,
    sequence: int,
    kind: EventKind,
    payload: dict[str, JsonValue],
) -> SessionEvent:
    return SessionEvent(
        event_id=f"event-{sequence}",
        session_id="session-1",
        sequence=sequence,
        kind=kind,
        occurred_at="2026-07-25T12:00:00Z",
        payload=payload,
        previous_event_hash=None,
    )
