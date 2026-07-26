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
    interaction_activity,
)
from heartwood.cli._tui import (
    ActionModeScreen,
    HeartwoodTerminalApp,
    _line_style,
    _mode_option_prompt,
    _risk_presentation,
    _unavailable_mode_summary,
)
from heartwood.gateway import (
    ProjectContext,
    SessionGateway,
    action_mode_label,
    action_risk_label,
    action_tool_label,
)
from heartwood.session import EventKind, JsonValue, SessionEvent


def test_interactive_session_uses_gateway_commands_and_persisted_replay(
    tmp_path: Path,
) -> None:
    gateway = SessionGateway(
        project=ProjectContext(tmp_path),
        env={},
        backend_id="deterministic",
    )
    gateway.start()
    try:
        session = InteractiveSession(gateway, session_id="terminal")

        task = session.submit("summarize the synthetic cohort")
        locked = session.submit("/permissions auto-approve-low-risk")
        allowed = session.submit("/allow")
        invalid = session.submit("/allow")
        replay = session.submit("/replay")
        action_settings = session.submit("/permissions")
        selected = session.submit("/permissions auto-approve-low-risk")

        assert not task.failed
        assert any("You: summarize" in _format_event(event) for event in task.events)
        assert locked.error
        assert "resolve the pending action set" in (locked.message or "")
        assert not allowed.failed
        assert any("Action approved" in _format_event(event) for event in allowed.events)
        assert invalid.message == "No actions are awaiting review."
        assert invalid.error
        assert replay.events == session.replay()
        assert replay.replace_transcript
        assert "Review Every Action" in (action_settings.message or "")
        assert "Low-Risk Automation" in (action_settings.message or "")
        assert not selected.failed
        assert gateway.action_settings()["confirmation_mode"] == "confirm-risky"
    finally:
        gateway.stop()


def test_textual_terminal_submits_without_blocking_and_replays_session(
    tmp_path: Path,
) -> None:
    gateway = SessionGateway(
        project=ProjectContext(tmp_path),
        env={},
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
            conversation = app.query_one("#conversation", RichLog)
            assert str(conversation.lines).count("You: inspect the synthetic workspace") == 1

            await pilot.press("enter")
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
            assert len(conversation.lines) > 0
            assert str(conversation.lines).count("You: inspect the synthetic workspace") == 1
            assert session.replay()

            composer.value = "/replay"
            await pilot.press("enter")
            for _ in range(50):
                await pilot.pause(0.02)
                if "ready" in str(app.query_one("#status", Static).render()):
                    break
            assert str(conversation.lines).count("You: inspect the synthetic workspace") == 1

    try:
        asyncio.run(exercise())
    finally:
        gateway.stop()


def test_line_formatter_groups_multi_action_review_and_resolution() -> None:
    proposal_without_arguments = _event(
        0,
        EventKind.TOOL_CALL_PROPOSED,
        {"tool_name": "terminal", "risk": "low", "summary": "Inspect status"},
    )
    proposal_with_empty_arguments = _event(
        0,
        EventKind.TOOL_CALL_PROPOSED,
        {
            "tool_name": "terminal",
            "risk": "low",
            "summary": "Inspect status",
            "arguments": {},
        },
    )
    assert _format_event(proposal_without_arguments) == ("[000] Action: Inspect status (risk=low)")
    assert _format_event(proposal_with_empty_arguments) == (
        "[000] Action: Inspect status (risk=low)"
    )

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
                    "arguments": {"command": "python run.py --output /project/cohort-summary.json"},
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
    assert "Terminal Command · Medium Risk" in pending_lines[2]
    assert "Arguments:" in pending_lines[3]
    assert "python run.py --output /project/cohort-summary.json" in "\n".join(pending_lines)
    assert any("Write the aggregate result" in line for line in pending_lines)
    assert pending_lines[-2:] == (
        "Allow the complete set once: /allow",
        "Reject the complete set: /reject",
    )
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

    replay_lines = _format_event_lines(
        (
            _event(
                5,
                EventKind.TOOL_CALL_PROPOSED,
                {
                    "tool_call_id": "tool-3",
                    "tool_name": "file_editor",
                    "risk": "medium",
                    "summary": "Write the reviewed aggregate",
                    "arguments": {
                        "command": "create",
                        "path": "/project/cohort-summary.txt",
                        "file_text": "heartwood-corrected-review-ok\n",
                    },
                },
            ),
            _event(
                6,
                EventKind.CONFIRMATION_RESOLVED,
                {"tool_call_id": "tool-3", "decision": "denied"},
            ),
        )
    )
    replay_text = "\n".join(replay_lines)
    assert "Arguments:" in replay_text
    assert '"command": "create"' in replay_text
    assert '"path": "/project/cohort-summary.txt"' in replay_text
    assert '"file_text": "heartwood-corrected-review-ok\\n"' in replay_text
    assert replay_lines[-1] == "[006] Action set denied (1 action)"


def test_interaction_activity_matches_the_submitted_operation() -> None:
    assert interaction_activity("inspect the project").label == "Working on your task"
    assert "approved action set" in interaction_activity("/allow").label
    assert "model" not in interaction_activity("/reject").guidance
    assert interaction_activity("/unknown").label == "Running the command"
    assert interaction_activity("/permissions").label == "Updating action review"


def test_terminal_presentation_uses_researcher_facing_labels() -> None:
    assert action_mode_label("always-confirm") == "Review Every Action"
    assert action_mode_label("future-mode") == "future-mode"
    assert action_tool_label("file_editor") == "File Change"
    assert action_tool_label("terminal") == "Terminal Command"
    assert action_tool_label("custom") == "custom Action"
    assert action_tool_label("") == "Tool Action"
    assert action_risk_label("high") == "High Risk"
    assert action_risk_label("low") == "Low Risk"
    assert action_risk_label("medium") == "Medium Risk"
    assert action_risk_label("unknown") == "Not Classified"
    assert action_risk_label("unexpected") == "Not Classified"

    assert _line_style("[001] Error: failed") == "bold red"
    assert _line_style("[001] Agent: complete") == "green"
    assert _line_style("[001] You: inspect") == "cyan"
    assert _line_style("[001] Action set approved") == "yellow"
    assert _line_style("[001] Tool terminal exit=0") == "blue"
    assert _line_style("[001] Model route allow") is None
    assert _risk_presentation("low") == ("Low Risk", "green")
    assert _risk_presentation("other") == ("Not Classified", "bold yellow")

    selected = _mode_option_prompt(
        {
            "allowed": True,
            "label": "Review Every Action",
            "mode": "always-confirm",
            "recommended": True,
        },
        selected="always-confirm",
    )
    unavailable = _mode_option_prompt(
        {
            "allowed": False,
            "label": "Low-Risk Automation",
            "mode": "confirm-risky",
        },
        selected="always-confirm",
    )
    assert selected.plain == "● Review Every Action · Recommended"
    assert unavailable.plain == "  Low-Risk Automation · Unavailable"
    assert _unavailable_mode_summary(
        (
            {
                "allowed": False,
                "label": "Low-Risk Automation",
                "unavailable_reason": "Unavailable under the active platform policy.",
            },
        )
    ) == ("Low-Risk Automation unavailable: Unavailable under the active platform policy.")


def test_textual_terminal_selects_action_review_mode_with_arrow_keys(
    tmp_path: Path,
) -> None:
    gateway = SessionGateway(
        project=ProjectContext(tmp_path),
        env={},
        backend_id="deterministic",
    )
    gateway.start()

    async def exercise() -> None:
        session = InteractiveSession(gateway, session_id="permissions")
        app = HeartwoodTerminalApp(session, format_events=_format_tui_event_lines)
        async with app.run_test(size=(72, 24)) as pilot:
            composer = app.query_one("#composer", Input)
            composer.value = "/permissions"
            await pilot.press("enter")
            await pilot.pause()

            assert isinstance(app.screen, ActionModeScreen)
            options = app.screen.query_one("#action-mode-options", OptionList)
            assert options.has_focus
            assert options.highlighted == 0

            await pilot.press("down", "enter")
            for _ in range(50):
                await pilot.pause(0.02)
                if gateway.action_settings()["confirmation_mode"] == "confirm-risky":
                    break

            assert gateway.action_settings()["confirmation_mode"] == "confirm-risky"
            assert "Low-Risk Automation" in str(app.query_one("#status", Static).render())
            assert app.query_one("#composer", Input).has_focus

    try:
        asyncio.run(exercise())
    finally:
        gateway.stop()


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
            assert "Review Agent Actions · 2 Actions" in title
            assert app.query_one("#approval-options", OptionList).has_focus
            assert app.query_one("#composer", Input).disabled

            await pilot.press("enter")
            for _ in range(50):
                await pilot.pause(0.02)
                if session.submitted:
                    break

            assert session.submitted == ["/reject"]
            assert not app.query_one("#approval", Vertical).display
            assert not app.query_one("#composer", Input).disabled

    asyncio.run(exercise())


def test_textual_terminal_keeps_the_first_long_action_visible() -> None:
    class LongBatchSession(InteractiveSession):
        def __init__(self) -> None:
            self.session_id = "long-batch"

        def replay(self) -> tuple[SessionEvent, ...]:
            return ()

        def pending_actions(self) -> tuple[PendingAction, ...]:
            return tuple(
                PendingAction(
                    f"request-{index}",
                    f"tool-{index}",
                    "terminal",
                    "medium",
                    f"Review step {index}",
                    {"command": f"python analyze.py --step {index} --output result-{index}.json"},
                )
                for index in range(1, 9)
            )

    async def exercise() -> None:
        app = HeartwoodTerminalApp(LongBatchSession(), format_events=_format_tui_event_lines)
        async with app.run_test(size=(64, 18)):
            action_log = app.query_one("#approval-actions", RichLog)

            assert action_log.auto_scroll is False
            assert action_log.scroll_y == 0
            assert "1. Review step 1" in str(action_log.lines)
            assert app.query_one("#approval-options", OptionList).highlighted == 0

    asyncio.run(exercise())


def test_textual_terminal_reports_delayed_activity_without_claiming_agent_progress() -> None:
    class IdleSession(InteractiveSession):
        def __init__(self) -> None:
            self.session_id = "activity"

        def replay(self) -> tuple[SessionEvent, ...]:
            return ()

        def pending_actions(self) -> tuple[PendingAction, ...]:
            return ()

    async def exercise() -> None:
        app = HeartwoodTerminalApp(IdleSession(), format_events=_format_tui_event_lines)
        async with app.run_test(size=(72, 20)):
            app._set_busy(
                True,
                activity=interaction_activity("inspect the project"),
            )
            app._busy_started -= 11
            app._refresh_working_status()

            status = str(app.query_one("#status", Static).render())
            assert "Still working on your task" in status
            assert "elapsed" in status
            assert "managed models can take several minutes" not in status

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
