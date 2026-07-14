# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import asyncio
from pathlib import Path

from textual.containers import Vertical
from textual.widgets import Input, OptionList, RichLog, Static

from heartwood.cli import _format_event, _format_event_lines, _format_tui_event_lines
from heartwood.cli._interactive import (
    InteractionResult,
    InteractiveSession,
    PendingAction,
)
from heartwood.cli._tui import HeartwoodTerminalApp
from heartwood.gateway import ProjectContext, SessionGateway
from heartwood.session import EventKind, JsonValue, SessionEvent


def test_interactive_session_uses_gateway_commands_and_persisted_replay(
    tmp_path: Path,
) -> None:
    gateway = SessionGateway(
        project=ProjectContext(tmp_path),
        backend_id="deterministic",
    )
    gateway.start()
    try:
        session = InteractiveSession(gateway, session_id="terminal")

        task = session.submit("summarize the synthetic cohort")
        allowed = session.submit("/allow")
        invalid = session.submit("/allow")
        replay = session.submit("/replay")

        assert not task.failed
        assert any("You: summarize" in _format_event(event) for event in task.events)
        assert not allowed.failed
        assert any("Action approved" in _format_event(event) for event in allowed.events)
        assert invalid.message == "No actions are awaiting review."
        assert invalid.error
        assert replay.events == session.replay()
        assert replay.replace_transcript
    finally:
        gateway.stop()


def test_textual_terminal_submits_without_blocking_and_replays_session(
    tmp_path: Path,
) -> None:
    gateway = SessionGateway(
        project=ProjectContext(tmp_path),
        backend_id="deterministic",
    )
    gateway.start()

    async def exercise() -> None:
        session = InteractiveSession(gateway, session_id="tui")
        app = HeartwoodTerminalApp(session, format_events=_format_tui_event_lines)
        async with app.run_test(size=(64, 22)) as pilot:
            composer = app.query_one("#composer", Input)
            composer.value = "inspect the synthetic workspace"
            await pilot.press("enter")
            for _ in range(50):
                await pilot.pause(0.02)
                if "waiting" in str(app.query_one("#status", Static).render()):
                    break

            assert composer.disabled
            assert app.query_one("#approval", Vertical).display
            assert app.query_one("#approval-options", OptionList).has_focus
            await pilot.press("down", "enter")
            for _ in range(50):
                await pilot.pause(0.02)
                if "ready" in str(app.query_one("#status", Static).render()):
                    break

            assert app.query_one("#composer", Input).disabled is False
            assert any(
                str(event.kind) == EventKind.CONFIRMATION_RESOLVED.value
                and event.payload.get("decision") == "denied"
                for event in session.replay()
            )
            conversation = app.query_one("#conversation", RichLog)
            line_count = len(conversation.lines)
            assert line_count > 0
            assert session.replay()

            composer.value = "/replay"
            await pilot.press("enter")
            for _ in range(50):
                await pilot.pause(0.02)
                if "ready" in str(app.query_one("#status", Static).render()):
                    break
            assert len(conversation.lines) == line_count

    try:
        asyncio.run(exercise())
    finally:
        gateway.stop()


def test_line_formatter_groups_multi_action_review_and_resolution() -> None:
    pending = (
        _event(
            1,
            EventKind.CONFIRMATION_REQUESTED,
            {
                "request": {
                    "request_id": "internal-request-1",
                    "tool_call_id": "tool-1",
                    "tool_name": "terminal",
                    "risk": "medium",
                    "summary": "Run the synthetic cohort command",
                }
            },
        ),
        _event(
            2,
            EventKind.CONFIRMATION_REQUESTED,
            {
                "request": {
                    "request_id": "internal-request-2",
                    "tool_call_id": "tool-2",
                    "tool_name": "file_editor",
                    "risk": "unknown",
                    "summary": "Write the aggregate result",
                }
            },
        ),
    )

    pending_lines = _format_event_lines(pending)

    assert pending_lines[0] == "Review 2 actions as one OpenHands action set:"
    assert "Run the synthetic cohort command" in pending_lines[1]
    assert "Write the aggregate result" in pending_lines[2]
    assert pending_lines[-2:] == ("Allow all once: /allow", "Reject all: /reject")
    assert "internal-request" not in "\n".join(pending_lines)

    resolved_lines = _format_event_lines(
        (
            _event(
                3,
                EventKind.CONFIRMATION_RESOLVED,
                {"tool_call_id": "tool-1", "decision": "approved"},
            ),
            _event(
                4,
                EventKind.CONFIRMATION_RESOLVED,
                {"tool_call_id": "tool-2", "decision": "approved"},
            ),
        )
    )

    assert resolved_lines == ("[003-004] Action set approved (2 actions)",)


def test_textual_terminal_groups_multiple_actions_under_one_keyboard_decision() -> None:
    class BatchSession(InteractiveSession):
        def __init__(self) -> None:
            self.session_id = "batch"
            self.submitted: list[str] = []
            self.resolved = False

        def replay(self) -> tuple[SessionEvent, ...]:
            return ()

        def pending_actions(self) -> tuple[PendingAction, ...]:
            if self.resolved:
                return ()
            return (
                PendingAction("request-1", "tool-1", "terminal", "medium", "Run cohort"),
                PendingAction("request-2", "tool-2", "file_editor", "unknown", "Write result"),
            )

        def submit(self, line: str) -> InteractionResult:
            self.submitted.append(line)
            self.resolved = True
            return InteractionResult()

    async def exercise() -> None:
        session = BatchSession()
        app = HeartwoodTerminalApp(session, format_events=_format_tui_event_lines)
        async with app.run_test(size=(64, 22)) as pilot:
            title = str(app.query_one("#approval-title", Static).render())
            assert "One decision applies to all 2 actions" in title
            assert app.query_one("#approval-options", OptionList).has_focus
            assert app.query_one("#composer", Input).disabled

            await pilot.press("down", "enter")
            for _ in range(50):
                await pilot.pause(0.02)
                if session.submitted:
                    break

            assert session.submitted == ["/reject"]
            assert not app.query_one("#approval", Vertical).display
            assert not app.query_one("#composer", Input).disabled

    asyncio.run(exercise())


def _event(
    sequence: int,
    kind: EventKind,
    payload: dict[str, JsonValue],
) -> SessionEvent:
    return SessionEvent(
        event_id=f"event-{sequence}",
        session_id="session",
        sequence=sequence,
        kind=kind,
        occurred_at="2026-07-13T00:00:00Z",
        payload=payload,
    )
