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

from heartwood.cli._interactive import (
    InteractionResult,
    InteractiveSession,
    format_projection_lines,
    interaction_activity,
)
from heartwood.cli._tui import HeartwoodTerminalApp
from heartwood.gateway import (
    ProjectContext,
    ProjectionApprovalAction,
    ProjectionApprovalGroup,
    ProjectionMessage,
    ProjectionSubagent,
    ProjectionUsage,
    RestGateway,
    RestRequest,
    SessionGateway,
    SessionProjection,
)
from heartwood.session import EventKind


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
        allowed = session.submit("/allow")
        invalid = session.submit("/allow")
        replay = session.submit("/replay")

        assert not task.failed
        assert task.projection is not None
        assert any(
            message.role == "user" and "summarize" in message.content
            for message in task.projection.conversation
        )
        assert not allowed.failed
        assert any(
            str(event.kind) == EventKind.CONFIRMATION_RESOLVED.value
            and event.payload.get("decision") == "approved"
            for event in allowed.events
        )
        assert invalid.message == "No actions are awaiting review."
        assert invalid.error
        assert replay.events == ()
        assert replay.projection == session.replay()
        assert replay.replace_transcript
    finally:
        gateway.stop()


def test_terminal_and_browser_consume_the_same_gateway_projection(tmp_path: Path) -> None:
    gateway = SessionGateway(
        project=ProjectContext(tmp_path),
        env={},
        backend_id="deterministic",
    )
    session = InteractiveSession(gateway, session_id="shared-projection")
    try:
        session.submit("inspect the synthetic workspace")

        terminal_projection = session.replay()
        browser_response = RestGateway(gateway).handle(
            RestRequest(
                method="GET",
                path="/sessions/shared-projection/projection",
            )
        )

        assert browser_response.status_code == 200
        assert browser_response.body == terminal_projection.safe_dict()
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
        app = HeartwoodTerminalApp(session)
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
                message.role == "trace" and message.content == "Action set rejected (1 action)"
                for message in session.replay().conversation
            )
            conversation = app.query_one("#conversation", RichLog)
            line_count = len(conversation.lines)
            assert line_count > 0
            assert session.replay().event_count > 0

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


def test_line_formatter_renders_the_gateway_owned_atomic_action_set() -> None:
    projection = SessionProjection(
        session_id="terminal-batch",
        event_count=3,
        revision=2,
        conversation=(
            ProjectionMessage(
                id="proposal",
                sequence=1,
                role="trace",
                label="Trace",
                content="Proposed terminal command",
                detail="Run the synthetic cohort command",
            ),
        ),
        pending_approval=ProjectionApprovalGroup(
            group_id="action-set-synthetic",
            actions=(
                ProjectionApprovalAction(
                    target_id="tool-1",
                    tool_name="terminal",
                    risk="medium",
                    summary="Run the synthetic cohort command",
                    arguments={"command": "python run.py --output cohort-summary.json"},
                ),
                ProjectionApprovalAction(
                    target_id="tool-2",
                    tool_name="file_editor",
                    risk="unknown",
                    summary="Write the aggregate result",
                    arguments={
                        "command": "create",
                        "path": "cohort-summary.txt",
                        "file_text": "heartwood-corrected-review-ok\n",
                    },
                ),
            ),
        ),
        usage=ProjectionUsage(
            usage_id="total",
            model_name="synthetic-model",
            call_count=2,
            prompt_tokens=120,
            completion_tokens=30,
        ),
        usage_by_purpose=(
            ProjectionUsage(
                usage_id="agent",
                model_name="synthetic-model",
                call_count=2,
                prompt_tokens=120,
                completion_tokens=30,
            ),
        ),
        subagents=(
            ProjectionSubagent(
                invocation_id="task-call-1",
                task_id="task-1",
                agent_name="research-planner",
                status="completed",
                parent_session_id="session-test",
                parent_action_id="action-1",
            ),
        ),
    )

    lines = format_projection_lines(projection)
    rendered = "\n".join(lines)

    assert "Review 2 actions as one OpenHands action set:" in rendered
    assert "Run the synthetic cohort command" in rendered
    assert '"command": "python run.py --output cohort-summary.json"' in rendered
    assert "Write the aggregate result" in rendered
    assert '"file_text": "heartwood-corrected-review-ok\\n"' in rendered
    assert "Model activity: 2 calls · 150 tokens · synthetic-model" in rendered
    assert "agent: 2 calls · 150 tokens" in rendered
    assert "research-planner: completed · invocation task-call-1 · task task-1" in rendered
    assert lines[-2:] == ("Allow all once: /allow", "Reject all: /reject")
    assert "tool-1" not in rendered


def test_interaction_activity_matches_the_submitted_operation() -> None:
    assert interaction_activity("inspect the project").label == "Working on your task"
    assert "approved action set" in interaction_activity("/allow").label
    assert "model" not in interaction_activity("/reject").guidance
    assert interaction_activity("/unknown").label == "Running the command"


def test_textual_terminal_groups_multiple_actions_under_one_keyboard_decision() -> None:
    class BatchSession(InteractiveSession):
        def __init__(self) -> None:
            self.session_id = "batch"
            self.submitted: list[str] = []
            self.resolved = False
            self.projection = SessionProjection(
                session_id=self.session_id,
                event_count=2,
                revision=1,
                pending_approval=ProjectionApprovalGroup(
                    group_id="action-set-batch",
                    actions=(
                        ProjectionApprovalAction(
                            target_id="tool-1",
                            tool_name="terminal",
                            risk="medium",
                            summary="Run cohort",
                        ),
                        ProjectionApprovalAction(
                            target_id="tool-2",
                            tool_name="file_editor",
                            risk="unknown",
                            summary="Write result",
                        ),
                    ),
                ),
            )

        def replay(self) -> SessionProjection:
            return self.projection

        def submit(self, line: str) -> InteractionResult:
            self.submitted.append(line)
            self.resolved = True
            self.projection = self.projection.model_copy(
                update={"pending_approval": None, "revision": 2}
            )
            return InteractionResult(projection=self.projection)

    async def exercise() -> None:
        session = BatchSession()
        app = HeartwoodTerminalApp(session)
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


def test_textual_terminal_reports_delayed_activity_without_claiming_agent_progress() -> None:
    class IdleSession(InteractiveSession):
        def __init__(self) -> None:
            self.session_id = "activity"

        def replay(self) -> SessionProjection:
            return SessionProjection(
                session_id=self.session_id,
                event_count=0,
                revision=-1,
            )

    async def exercise() -> None:
        app = HeartwoodTerminalApp(IdleSession())
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
