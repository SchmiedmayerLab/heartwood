# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import asyncio
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Literal, cast
from unittest.mock import patch

import pytest
from textual.containers import Vertical
from textual.pilot import Pilot
from textual.widgets import Input, OptionList, RichLog, Static, TabbedContent, Tree

from heartwood.cli._interactive import (
    InteractionResult,
    InteractiveSession,
    format_action_record_lines,
    format_projection_lines,
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
    ActionSettingsError,
    ProjectContext,
    ProjectionActionOutcome,
    ProjectionActionRecord,
    ProjectionAffectedPath,
    ProjectionApprovalGroup,
    ProjectionFileEditorActionDetails,
    ProjectionLifecycleState,
    ProjectionMessage,
    ProjectionOtherActionDetails,
    ProjectionResearcherNotice,
    ProjectionResearcherStatus,
    ProjectionSubagent,
    ProjectionSuggestion,
    ProjectionTask,
    ProjectionTaskActionDetails,
    ProjectionTerminalActionDetails,
    ProjectionUsage,
    RestGateway,
    RestRequest,
    SessionGateway,
    SessionLifecycle,
    SessionProjection,
    action_mode_label,
    action_risk_label,
    action_tool_label,
)
from heartwood.schemas import (
    ActionModeOptionResponse,
    ActionSettingsResponse,
    WorkspaceTreeResponse,
)
from heartwood.session import EventKind, JsonValue


async def _wait_for_tui(
    pilot: Pilot[None],
    condition: Callable[[], bool],
    *,
    description: str,
    timeout: float = 10.0,
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() >= deadline:
            raise AssertionError(f"Timed out waiting for {description}.")
        await pilot.pause(0.05)


def _test_action_settings() -> ActionSettingsResponse:
    return {
        "schema_version": "heartwood.action-settings.v1",
        "confirmation_mode": "always-confirm",
        "scope_description": "Applies to this test project.",
        "presentation": {
            "risk_labels": {
                "low": "Low Risk",
                "medium": "Medium Risk",
                "high": "High Risk",
            },
            "state_labels": {
                "approved": "Approved",
                "awaiting-review": "Awaiting Review",
                "failed": "Failed",
                "outcome-unknown": "Outcome Unknown",
                "proposed": "Proposed",
                "rejected": "Rejected",
                "running": "Running",
                "succeeded": "Succeeded",
            },
            "tool_labels": {
                "file_editor": "File Change",
                "terminal": "Terminal Command",
            },
            "other_tool_label_template": "{tool_name} Action",
            "unknown_risk_label": "Not Classified",
            "unknown_tool_label": "Tool Action",
        },
        "change_allowed": True,
        "change_blocked_reason": None,
        "modes": [
            {
                "allowed": True,
                "automatic_risks": [],
                "command_value": "ask-every-time",
                "description": "Review every proposed action set.",
                "label": "Review Every Action",
                "mode": "always-confirm",
                "recommended": True,
                "reviewed_risks": ["low", "medium", "high", "unknown"],
                "unavailable_reason": None,
            }
        ],
    }


def _approval_action(
    tool_call_id: str,
    *,
    tool_name: str,
    risk: Literal["high", "low", "medium", "unknown"] = "unknown",
    summary: str,
    arguments: dict[str, JsonValue] | None = None,
    group_id: str = "action-set-synthetic",
    sequence: int = 0,
) -> ProjectionActionRecord:
    action_arguments: dict[str, JsonValue] = {} if arguments is None else arguments
    details: (
        ProjectionTerminalActionDetails
        | ProjectionFileEditorActionDetails
        | ProjectionOtherActionDetails
    )
    if tool_name == "terminal":
        details = ProjectionTerminalActionDetails(
            command=str(action_arguments.get("command", "")),
        )
    elif tool_name == "file_editor":
        operation_value = str(action_arguments.get("command", "unknown"))
        operation = (
            cast(
                Literal["view", "create", "str_replace", "insert", "undo_edit"],
                operation_value,
            )
            if operation_value
            in {
                "view",
                "create",
                "str_replace",
                "insert",
                "undo_edit",
            }
            else "unknown"
        )
        details = ProjectionFileEditorActionDetails(
            operation=operation,
            path=(str(action_arguments["path"]) if "path" in action_arguments else None),
        )
    else:
        details = ProjectionOtherActionDetails()
    return ProjectionActionRecord(
        tool_call_id=tool_call_id,
        group_id=group_id,
        tool_name=tool_name,
        risk=risk,
        summary=summary,
        arguments=action_arguments,
        details=details,
        state="awaiting-review",
        proposed_sequence=sequence,
        updated_sequence=sequence,
    )


def test_plain_terminal_marks_bounded_action_output_as_truncated() -> None:
    action = _approval_action(
        "tool-1",
        tool_name="terminal",
        summary="Run focused tests",
        arguments={"command": "pytest tests/test_analysis.py"},
    ).model_copy(
        update={
            "affected_paths": (
                ProjectionAffectedPath(
                    path="results/report.txt",
                    effect="modified",
                ),
            ),
            "decision": "approved",
            "state": "failed",
            "outcome": ProjectionActionOutcome(
                exit_code=1,
                summary="terminal failed",
                result="synthetic failure\n",
                result_truncated=True,
            ),
        }
    )

    rendered = "\n".join(format_action_record_lines(action))

    assert "$ pytest tests/test_analysis.py" in rendered
    assert "decision approved (complete action set)" in rendered
    assert '"command":"pytest tests/test_analysis.py"' in rendered
    assert "modified results/report.txt (file-editor-action)" in rendered
    assert "synthetic failure" in rendered
    assert "[output truncated]" in rendered


def test_plain_terminal_renders_every_typed_action_and_automatic_decision() -> None:
    file_action = _approval_action(
        "file-1",
        tool_name="file_editor",
        summary="Edit a file",
    ).model_copy(
        update={
            "group_id": None,
            "decision": "approved",
            "state": "succeeded",
            "affected_paths": (
                ProjectionAffectedPath(
                    path="results/summary.txt",
                    effect="created",
                ),
            ),
        }
    )
    specialist_action = _approval_action(
        "task-1",
        tool_name="task",
        summary="Delegate the analysis",
    ).model_copy(
        update={
            "details": ProjectionTaskActionDetails(subagent_type="research-planner"),
        }
    )
    other_action = _approval_action(
        "other-1",
        tool_name="synthetic_tool",
        summary="Run a custom action",
    )

    rendered = "\n".join(
        line
        for action in (file_action, specialist_action, other_action)
        for line in format_action_record_lines(action)
    )

    assert "[Succeeded] unknown path unavailable" in rendered
    assert "decision approved (automatic policy)" in rendered
    assert "created results/summary.txt (file-editor-action)" in rendered
    assert "specialist research-planner" in rendered
    assert "Run a custom action" in rendered


def test_terminal_projection_renders_control_sequences_visibly() -> None:
    action = _approval_action(
        "tool-control",
        tool_name="terminal",
        summary="Run\x1b]0;spoofed\x07 command",
        arguments={"command": "printf '\\033]0;spoofed\\007'"},
    ).model_copy(
        update={
            "state": "failed",
            "decision": "approved",
            "outcome": ProjectionActionOutcome(
                exit_code=1,
                summary="failed\x9b31m",
                result="before\x1b]0;spoofed\x07after\u202e",
            ),
        }
    )
    projection = SessionProjection(
        session_id="terminal-controls",
        event_count=1,
        revision=0,
        conversation=(
            ProjectionMessage(
                id="message-control",
                sequence=0,
                role="agent",
                label="Agent\x1b[31m",
                content="message\x1b]0;spoofed\x07\u202e",
            ),
        ),
        actions=(action,),
        streaming_text="stream\x1b[2J",
    )

    rendered = "\n".join(format_projection_lines(projection))

    assert "\x1b" not in rendered
    assert "\x07" not in rendered
    assert "\x9b" not in rendered
    assert "\u202e" not in rendered
    assert "\\x1b" in rendered
    assert "\\x07" in rendered
    assert "\\x9b" in rendered
    assert "\\u202e" in rendered


def test_invalid_workspace_command_does_not_end_the_interactive_session(
    tmp_path: Path,
) -> None:
    gateway = SessionGateway(
        project=ProjectContext(tmp_path),
        env={},
        backend_id="deterministic",
    )
    session_id = gateway.default_session()["session_id"]
    session = InteractiveSession(gateway, session_id=session_id)

    rejected = session.submit("/show ../outside.txt")
    continued = session.submit("/help")

    assert rejected.error is True
    assert rejected.message is not None
    assert rejected.message.startswith("HW-WORKSPACE-001:")
    assert continued.error is False
    assert continued.message is not None
    assert "/files" in continued.message


def test_interactive_workspace_commands_share_the_gateway_inspector(
    tmp_path: Path,
) -> None:
    (tmp_path / "analysis.py").write_text("answer = 42\n", encoding="utf-8")
    gateway = SessionGateway(
        project=ProjectContext(tmp_path),
        env={},
        backend_id="deterministic",
    )
    session = InteractiveSession(
        gateway,
        session_id=gateway.default_session()["session_id"],
    )

    files = session.submit("/files")
    nested_files = session.submit("/files .")
    shown = session.submit("/show analysis.py")
    changes = session.submit("/changes")
    diff = session.submit("/changes analysis.py")
    unknown = session.submit("/future-command")

    assert files.message is not None
    assert "analysis.py" in files.message
    assert nested_files.message is not None
    assert "analysis.py" in nested_files.message
    assert shown.message is not None
    assert "answer = 42" in shown.message
    assert changes.message is not None
    assert "Project changes" in changes.message
    assert diff.message is not None
    assert "analysis.py" in diff.message
    assert unknown.message == "Unknown command: /future-command"


def test_interactive_session_stable_wait_has_a_deterministic_deadline() -> None:
    class RunningSession(InteractiveSession):
        def __init__(self) -> None:
            self.session_id = "running"
            self.replay_count = 0

        def replay(self) -> SessionProjection:
            self.replay_count += 1
            return SessionProjection(
                session_id=self.session_id,
                event_count=1,
                revision=0,
                lifecycle=ProjectionLifecycleState(status=SessionLifecycle.RUNNING),
            )

    session = RunningSession()
    now = 0.0
    sleeps: list[float] = []

    def monotonic() -> float:
        return now

    def sleep(duration: float) -> None:
        nonlocal now
        sleeps.append(duration)
        now += duration

    with (
        patch("heartwood.cli._interactive.time.monotonic", side_effect=monotonic),
        patch("heartwood.cli._interactive.time.sleep", side_effect=sleep),
        pytest.raises(
            TimeoutError,
            match=r"session running remained active for 0\.5 seconds",
        ),
    ):
        session.wait_until_stable(poll_interval=0.2, timeout=0.5)

    assert sleeps == pytest.approx([0.2, 0.2, 0.1])
    assert session.replay_count == 4


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
        session.wait_until_stable()
        invalid = session.submit("/allow")
        replay = session.submit("/replay")

        assert not task.failed
        assert task.projection is not None
        assert any(
            message.role == "user" and "summarize" in message.content
            for message in task.projection.conversation
        )
        assert locked.error
        assert "resolve the pending action set" in (locked.message or "")
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

        action_settings = session.submit("/permissions")
        specialists = session.submit("/specialists")
        selected = session.submit("/permissions auto-approve-low-risk")
        assert "Review Every Action" in (action_settings.message or "")
        assert "Low-Risk Automation" in (action_settings.message or "")
        assert "Research Planner" in (specialists.message or "")
        assert "Analysis Implementer" in (specialists.message or "")
        assert "Not available in this release" in (specialists.message or "")
        assert not selected.failed
        assert gateway.action_settings()["confirmation_mode"] == "confirm-risky"
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
            await _wait_for_tui(
                pilot,
                lambda: composer.has_focus,
                description="the composer to receive initial focus",
            )
            composer.value = "inspect the synthetic workspace"
            await pilot.press("enter")
            approval = app.query_one("#approval", Vertical)
            options = app.query_one("#approval-options", OptionList)
            await _wait_for_tui(
                pilot,
                lambda: approval.display and composer.disabled and options.has_focus,
                description="the action review to receive focus",
            )

            assert composer.disabled
            assert approval.display
            assert options.has_focus
            conversation = app.query_one("#conversation", RichLog)
            assert str(conversation.lines).count("You: inspect the synthetic workspace") == 1

            await pilot.press("enter")
            await _wait_for_tui(
                pilot,
                lambda: (
                    not app.query_one("#approval", Vertical).display
                    and not app.query_one("#composer", Input).disabled
                ),
                description="the rejected action set to return control to the composer",
            )

            assert app.query_one("#composer", Input).disabled is False
            assert any(
                message.role == "trace" and message.content == "Action set rejected (1 action)"
                for message in session.replay().conversation
            )
            assert len(conversation.lines) > 0
            assert str(conversation.lines).count("You: inspect the synthetic workspace") == 1
            assert session.replay().event_count > 0

            composer.value = "/replay"
            await pilot.press("enter")
            await asyncio.wait_for(app.workers.wait_for_complete(), timeout=10.0)
            await pilot.pause()
            assert app.query_one("#composer", Input).disabled is False
            assert str(conversation.lines).count("You: inspect the synthetic workspace") == 1

    try:
        asyncio.run(exercise())
    finally:
        gateway.stop()


def test_textual_terminal_inspects_files_and_changes_without_a_local_reducer(
    tmp_path: Path,
) -> None:
    analysis = tmp_path / "analysis.py"
    analysis.write_text("answer = 41\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "analysis.py"], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Heartwood Test",
            "-c",
            "user.email=heartwood@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            "Add synthetic analysis",
        ],
        cwd=tmp_path,
        check=True,
    )
    analysis.write_text("answer = 42\n", encoding="utf-8")
    markup_name = "[not-a-style].txt"
    (tmp_path / markup_name).write_text("literal filename\n", encoding="utf-8")
    gateway = SessionGateway(
        project=ProjectContext(tmp_path),
        env={},
        backend_id="deterministic",
    )
    gateway.start()

    async def exercise() -> None:
        app = HeartwoodTerminalApp(InteractiveSession(gateway, session_id="workspace-tui"))
        async with app.run_test(size=(80, 24)) as pilot:
            tree = app.query_one("#file-tree", Tree)
            await _wait_for_tui(
                pilot,
                lambda: (
                    {node.data for node in tree.root.children if node.data is not None}
                    >= {"analysis.py", markup_name}
                ),
                description="the bounded project tree",
            )
            markup_node = next(node for node in tree.root.children if node.data == markup_name)
            assert str(markup_node.label) == markup_name

            app.action_show_files()
            assert app.query_one("#workspace-tabs", TabbedContent).active == "files-pane"
            file_node = next(node for node in tree.root.children if node.data == "analysis.py")
            tree.select_node(file_node)
            tree.action_select_cursor()
            await _wait_for_tui(
                pilot,
                lambda: (
                    "answer = 42"
                    in "".join(line.text for line in app.query_one("#file-preview", RichLog).lines)
                ),
                description="the selected file preview",
            )

            app.action_show_changes()
            assert app.query_one("#workspace-tabs", TabbedContent).active == "changes-pane"
            changes = app.query_one("#change-list", OptionList)
            await _wait_for_tui(
                pilot,
                lambda: any(path == "analysis.py" for path in app._change_paths.values()),
                description="the changed-file list",
            )
            changes.highlighted = next(
                index
                for index, path in enumerate(app._change_paths.values())
                if path == "analysis.py"
            )
            await _wait_for_tui(
                pilot,
                lambda: (
                    "answer = 42"
                    in "".join(
                        line.text for line in app.query_one("#change-preview", RichLog).lines
                    )
                ),
                description="the selected change preview",
            )

            analysis.write_text("answer = 43\n", encoding="utf-8")
            app._request_workspace_overview()
            await _wait_for_tui(
                pilot,
                lambda: (
                    "answer = 43"
                    in "".join(line.text for line in app.query_one("#file-preview", RichLog).lines)
                    and "answer = 43"
                    in "".join(
                        line.text for line in app.query_one("#change-preview", RichLog).lines
                    )
                ),
                description="the preserved file and change selections after refresh",
            )

    try:
        asyncio.run(exercise())
    finally:
        gateway.stop()


def test_textual_terminal_discards_a_stale_workspace_overview(
    tmp_path: Path,
) -> None:
    (tmp_path / "current.txt").write_text("current\n", encoding="utf-8")
    gateway = SessionGateway(
        project=ProjectContext(tmp_path),
        env={},
        backend_id="deterministic",
    )
    gateway.start()

    async def exercise() -> None:
        session = InteractiveSession(gateway, session_id="workspace-generation")
        app = HeartwoodTerminalApp(session)
        async with app.run_test(size=(80, 24)) as pilot:
            await asyncio.wait_for(app.workers.wait_for_complete(), timeout=10.0)
            current = session.workspace_tree()
            changes = session.workspace_changes()
            stale = cast(
                WorkspaceTreeResponse,
                {
                    **current,
                    "entries": [
                        {
                            "path": "stale.txt",
                            "name": "stale.txt",
                            "kind": "file",
                            "depth": 1,
                            "size_bytes": 6,
                        }
                    ],
                },
            )
            app._workspace_overview_generation = 100

            app._finish_workspace_overview(stale, changes, None, 99)
            tree = app.query_one("#file-tree", Tree)
            assert all(node.data != "stale.txt" for node in tree.root.children)

            app._finish_workspace_overview(current, changes, None, 100)
            await pilot.pause()
            assert any(node.data == "current.txt" for node in tree.root.children)

    try:
        asyncio.run(exercise())
    finally:
        gateway.stop()


def test_textual_terminal_appends_persisted_messages_and_replaces_transient_state() -> None:
    class ProjectionSession(InteractiveSession):
        def __init__(self) -> None:
            self.session_id = "incremental"
            self.projection = SessionProjection(
                session_id=self.session_id,
                event_count=1,
                revision=0,
                stream_epoch="process-a",
                stream_revision=1,
                lifecycle=ProjectionLifecycleState(status=SessionLifecycle.RUNNING),
                conversation=(
                    ProjectionMessage(
                        id="user-1",
                        sequence=0,
                        role="user",
                        label="You",
                        content="Inspect the synthetic project",
                    ),
                ),
                streaming_text="Inspecting",
                usage=ProjectionUsage(
                    usage_id="total",
                    purpose_label="Total Model Activity",
                    model_name="synthetic-model",
                    call_count=1,
                    prompt_tokens=10,
                    completion_tokens=2,
                ),
            )

        def replay(self) -> SessionProjection:
            return self.projection

        def action_settings(self) -> ActionSettingsResponse:
            return _test_action_settings()

    async def exercise() -> None:
        session = ProjectionSession()
        app = HeartwoodTerminalApp(session)
        async with app.run_test(size=(72, 24)) as pilot:
            conversation = app.query_one("#conversation", RichLog)
            streaming = app.query_one("#streaming", Static)
            details = app.query_one("#projection-details", RichLog)
            await _wait_for_tui(
                pilot,
                lambda: (
                    "You: Inspect the synthetic project" in str(conversation.lines)
                    and "Agent: Inspecting" in str(streaming.render())
                ),
                description="the initial projection",
            )
            first_rendered_line = conversation.lines[0]

            session.projection = session.projection.model_copy(
                update={
                    "event_count": 2,
                    "revision": 1,
                    "stream_revision": 2,
                    "lifecycle": ProjectionLifecycleState(status=SessionLifecycle.FINISHED),
                    "conversation": (
                        *session.projection.conversation,
                        ProjectionMessage(
                            id="agent-1",
                            sequence=1,
                            role="agent",
                            label="Agent",
                            content="Inspection complete",
                        ),
                    ),
                    "streaming_text": "",
                    "usage": ProjectionUsage(
                        usage_id="total",
                        purpose_label="Total Model Activity",
                        model_name="synthetic-model",
                        call_count=2,
                        prompt_tokens=20,
                        completion_tokens=5,
                    ),
                }
            )
            app._apply_projection(session.projection)
            await _wait_for_tui(
                pilot,
                lambda: "2 calls · 25 tokens" in str(details.lines),
                description="the updated projection details",
            )

            assert conversation.lines[0] is first_rendered_line
            assert str(conversation.lines).count("You: Inspect the synthetic project") == 1
            assert str(conversation.lines).count("Agent: Inspection complete") == 1
            assert not streaming.display
            assert "2 calls · 25 tokens" in str(details.lines)

    asyncio.run(exercise())


def test_textual_terminal_resets_on_stream_handoff_and_rejects_retired_epoch() -> None:
    class HandoffSession(InteractiveSession):
        def __init__(self) -> None:
            self.session_id = "handoff"
            self.projection = SessionProjection(
                session_id=self.session_id,
                event_count=1,
                revision=0,
                stream_epoch="first-process",
                stream_revision=4,
                conversation=(
                    ProjectionMessage(
                        id="old-message",
                        sequence=0,
                        role="agent",
                        label="Agent",
                        content="Old process response",
                    ),
                ),
                streaming_text="Stale partial response",
            )

        def replay(self) -> SessionProjection:
            return self.projection

        def action_settings(self) -> ActionSettingsResponse:
            return _test_action_settings()

    async def exercise() -> None:
        session = HandoffSession()
        app = HeartwoodTerminalApp(session)
        async with app.run_test(size=(72, 22)) as pilot:
            conversation = app.query_one("#conversation", RichLog)
            streaming = app.query_one("#streaming", Static)
            await _wait_for_tui(
                pilot,
                lambda: "Old process response" in str(conversation.lines),
                description="the first stream epoch",
            )
            first_epoch = session.projection
            restarted = first_epoch.model_copy(
                update={
                    "stream_epoch": "restarted-process",
                    "stream_revision": 0,
                    "conversation": (
                        ProjectionMessage(
                            id="fresh-message",
                            sequence=0,
                            role="agent",
                            label="Agent",
                            content="Fresh process response",
                        ),
                    ),
                    "streaming_text": "Fresh partial response",
                }
            )
            session.projection = restarted
            app._apply_projection(restarted)

            assert "Old process response" not in str(conversation.lines)
            assert "Fresh process response" in str(conversation.lines)
            assert "Fresh partial response" in str(streaming.render())

            delayed = first_epoch.model_copy(
                update={
                    "stream_revision": 5,
                    "streaming_text": "Delayed stale response",
                }
            )
            app._apply_projection(delayed)

            assert app._projection == restarted
            assert "Fresh process response" in str(conversation.lines)
            assert "Delayed stale response" not in str(streaming.render())

    asyncio.run(exercise())


def test_textual_gateway_reads_run_outside_the_event_loop(tmp_path: Path) -> None:
    gateway = SessionGateway(
        project=ProjectContext(tmp_path),
        env={},
        backend_id="deterministic",
    )
    gateway.start()

    class RecordingSession(InteractiveSession):
        def __init__(self) -> None:
            super().__init__(gateway, session_id="threaded-reads")
            self.replay_threads: list[int] = []
            self.action_settings_threads: list[int] = []

        def replay(self) -> SessionProjection:
            self.replay_threads.append(threading.get_ident())
            return super().replay()

        def pending_approval(self) -> ProjectionApprovalGroup | None:
            raise AssertionError("key handlers must use the cached session projection")

        def action_settings(self) -> ActionSettingsResponse:
            self.action_settings_threads.append(threading.get_ident())
            return super().action_settings()

    async def exercise() -> None:
        event_loop_thread = threading.get_ident()
        session = RecordingSession()
        app = HeartwoodTerminalApp(session)
        async with app.run_test(size=(72, 22)) as pilot:
            await _wait_for_tui(
                pilot,
                lambda: bool(
                    session.replay_threads
                    and session.action_settings_threads
                    and app._projection is not None
                ),
                description="the initial gateway reads",
            )

            app.action_focus_composer()
            app.action_pause()
            await _wait_for_tui(
                pilot,
                lambda: not app._busy,
                description="the pause command",
            )

            app.action_show_permissions()
            await _wait_for_tui(
                pilot,
                lambda: isinstance(app.screen, ActionModeScreen),
                description="the action review mode screen",
            )

            assert event_loop_thread not in session.replay_threads
            assert event_loop_thread not in session.action_settings_threads

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
        task_plan=(
            ProjectionTask(
                title="Inspect the synthetic cohort",
                status="done",
                status_label="Complete",
            ),
            ProjectionTask(
                title="Write the aggregate result",
                status="in-progress",
                status_label="In Progress",
            ),
        ),
        pending_approval=ProjectionApprovalGroup(
            group_id="action-set-synthetic",
            actions=(
                _approval_action(
                    "tool-1",
                    tool_name="terminal",
                    risk="medium",
                    summary="Run the synthetic cohort command",
                    arguments={"command": "python run.py --output cohort-summary.json"},
                ),
                _approval_action(
                    "tool-2",
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
            purpose_label="Total Model Activity",
            model_name="synthetic-model",
            call_count=2,
            prompt_tokens=120,
            completion_tokens=30,
        ),
        usage_by_purpose=(
            ProjectionUsage(
                usage_id="agent",
                purpose_label="Primary Agent",
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
                role_label="Research Planner",
                status="completed",
                status_label="Complete",
                parent_session_id="session-test",
                parent_action_id="action-1",
                task_summary="Review the synthetic analysis plan",
                result_summary="Plan review completed",
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
    assert "Primary Agent: 2 calls · 150 tokens" in rendered
    assert "Task plan:" in rendered
    assert "[x] Inspect the synthetic cohort (Complete)" in rendered
    assert "[ ] Write the aggregate result (In Progress)" in rendered
    assert "Research Planner: Complete" in rendered
    assert "Task: Review the synthetic analysis plan" in rendered
    assert "Result: Plan review completed" in rendered
    assert "task-call-1" not in rendered
    assert lines[-2:] == (
        "Allow the complete set once: /allow",
        "Reject the complete set: /reject",
    )
    assert "tool-1" not in rendered

    filtered = "\n".join(format_projection_lines(projection, after_sequence=1))
    assert "Proposed terminal command" not in filtered
    assert "Model activity: 2 calls · 150 tokens · synthetic-model" in filtered
    assert "Review 2 actions as one OpenHands action set:" in filtered


def test_line_formatter_renders_gateway_owned_suggestions_without_internal_ids() -> None:
    projection = SessionProjection(
        session_id="terminal-suggestions",
        event_count=0,
        revision=-1,
        suggestions=(
            ProjectionSuggestion(
                suggestion_id="inspect-project",
                label="Inspect the Project",
                prompt="Inspect this project without changing files.",
                kind="task",
            ),
        ),
    )

    rendered = "\n".join(format_projection_lines(projection))

    assert "Suggested next steps:" in rendered
    assert "Inspect the Project: Inspect this project without changing files." in rendered
    assert "inspect-project" not in rendered


def test_line_formatter_renders_gateway_owned_command_notice() -> None:
    projection = SessionProjection(
        session_id="terminal-command-notice",
        event_count=0,
        revision=-1,
        lifecycle=ProjectionLifecycleState(
            status=SessionLifecycle.RUNNING,
            can_pause=True,
            can_steer=True,
        ),
        researcher_status=ProjectionResearcherStatus(
            code="working",
            label="Heartwood Is Working",
            detail="You can send guidance or pause while the task is active.",
            tone="progress",
        ),
        researcher_notice=ProjectionResearcherNotice(
            notice_id="command:pause-race:rejected",
            code="request-not-applied",
            label="Request Not Applied",
            detail="The task reached a stable boundary before pause was applied.",
            tone="attention",
        ),
    )

    rendered = "\n".join(format_projection_lines(projection))

    assert "Status: Heartwood Is Working" in rendered
    assert "Notice: Request Not Applied" in rendered
    assert "stable boundary" in rendered


def test_interaction_activity_matches_the_submitted_operation() -> None:
    assert interaction_activity("inspect the project").label == "Working on your task"
    assert "approved action set" in interaction_activity("/allow").label
    assert "model" not in interaction_activity("/reject").guidance
    assert interaction_activity("/unknown").label == "Running the command"
    assert interaction_activity("/permissions").label == "Updating action review"
    assert interaction_activity("/specialists").label == "Loading research specialists"


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
    assert _line_style("[001] Tool: Ran Terminal Command") == "blue"
    assert _line_style("[002] Tool: Ran File Change") == "blue"
    assert _line_style("[003] Tool terminal exit=0") is None
    assert _line_style("[001] Model route allow") is None
    assert _risk_presentation("low") == ("Low Risk", "green")
    assert _risk_presentation("other") == ("Not Classified", "bold yellow")

    selected_mode: ActionModeOptionResponse = {
        "allowed": True,
        "automatic_risks": [],
        "command_value": "ask-every-time",
        "description": "Review every proposed action set.",
        "label": "Review Every Action",
        "mode": "always-confirm",
        "recommended": True,
        "reviewed_risks": ["low", "medium", "high", "unknown"],
        "unavailable_reason": None,
    }
    unavailable_mode: ActionModeOptionResponse = {
        "allowed": False,
        "automatic_risks": ["low"],
        "command_value": "auto-approve-low-risk",
        "description": "Continue automatically only for low-risk action sets.",
        "label": "Low-Risk Automation",
        "mode": "confirm-risky",
        "recommended": False,
        "reviewed_risks": ["medium", "high", "unknown"],
        "unavailable_reason": "Unavailable under the active platform policy.",
    }
    selected = _mode_option_prompt(selected_mode, selected="always-confirm")
    unavailable = _mode_option_prompt(unavailable_mode, selected="always-confirm")
    assert selected.plain == "● Review Every Action · Recommended"
    assert unavailable.plain == "  Low-Risk Automation · Unavailable"
    assert _unavailable_mode_summary((unavailable_mode,)) == (
        "Low-Risk Automation unavailable: Unavailable under the active platform policy."
    )


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
        app = HeartwoodTerminalApp(session)
        async with app.run_test(size=(72, 24)) as pilot:
            composer = app.query_one("#composer", Input)
            await _wait_for_tui(
                pilot,
                lambda: composer.has_focus,
                description="the composer to receive initial focus",
            )
            composer.value = "/permissions"
            await pilot.press("enter")
            await _wait_for_tui(
                pilot,
                lambda: isinstance(app.screen, ActionModeScreen),
                description="the action review mode screen",
            )

            assert isinstance(app.screen, ActionModeScreen)
            options = app.screen.query_one("#action-mode-options", OptionList)
            assert options.has_focus
            assert options.highlighted == 0

            await pilot.press("down", "enter")
            await _wait_for_tui(
                pilot,
                lambda: gateway.action_settings()["confirmation_mode"] == "confirm-risky",
                description="the selected action review mode to persist",
            )

            assert gateway.action_settings()["confirmation_mode"] == "confirm-risky"
            assert "Low-Risk Automation" in str(app.query_one("#status", Static).render())
            assert app.query_one("#composer", Input).has_focus

    try:
        asyncio.run(exercise())
    finally:
        gateway.stop()


def test_textual_terminal_reports_action_settings_load_failure() -> None:
    class InvalidSettingsSession(InteractiveSession):
        def __init__(self) -> None:
            self.session_id = "invalid-settings"

        def replay(self) -> SessionProjection:
            return SessionProjection(
                session_id=self.session_id,
                event_count=0,
                revision=-1,
            )

        def action_settings(self) -> ActionSettingsResponse:
            raise ActionSettingsError("project action settings are invalid")

    async def exercise() -> None:
        app = HeartwoodTerminalApp(InvalidSettingsSession())
        async with app.run_test(size=(72, 20)) as pilot:
            with patch.object(app, "notify") as notify:
                app.action_show_permissions()
                await _wait_for_tui(
                    pilot,
                    lambda: notify.called,
                    description="the action settings error",
                )

            notify.assert_called_once_with(
                "project action settings are invalid",
                title="Action Review",
                severity="error",
            )

    asyncio.run(exercise())


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
                        _approval_action(
                            "tool-1",
                            tool_name="terminal",
                            risk="medium",
                            summary="Run cohort",
                            group_id="action-set-batch",
                        ),
                        _approval_action(
                            "tool-2",
                            tool_name="file_editor",
                            risk="unknown",
                            summary="Write result",
                            group_id="action-set-batch",
                        ),
                    ),
                ),
            )

        def replay(self) -> SessionProjection:
            return self.projection

        def action_settings(self) -> ActionSettingsResponse:
            return _test_action_settings()

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
            await _wait_for_tui(
                pilot,
                lambda: (
                    app.query_one("#approval", Vertical).display
                    and app.query_one("#approval-options", OptionList).has_focus
                ),
                description="the grouped action review",
            )
            title = str(app.query_one("#approval-title", Static).render())
            assert "Review Agent Actions · 2 Actions" in title
            assert app.query_one("#approval-options", OptionList).has_focus
            assert app.query_one("#composer", Input).disabled

            await pilot.press("enter")
            await _wait_for_tui(
                pilot,
                lambda: bool(session.submitted),
                description="the grouped action decision to be submitted",
            )

            assert session.submitted == ["/reject"]
            assert not app.query_one("#approval", Vertical).display
            assert not app.query_one("#composer", Input).disabled

    asyncio.run(exercise())


def test_textual_terminal_restores_action_review_after_busy_projection_race() -> None:
    class PendingSession(InteractiveSession):
        def __init__(self) -> None:
            self.session_id = "pending-race"
            self.projection = SessionProjection(
                session_id=self.session_id,
                event_count=0,
                revision=-1,
            )

        def replay(self) -> SessionProjection:
            return self.projection

        def action_settings(self) -> ActionSettingsResponse:
            return _test_action_settings()

    async def exercise() -> None:
        session = PendingSession()
        app = HeartwoodTerminalApp(session)
        async with app.run_test(size=(64, 22)) as pilot:
            await _wait_for_tui(
                pilot,
                lambda: app.query_one("#composer", Input).has_focus,
                description="the initial composer focus",
            )
            app._set_busy(True, activity=interaction_activity("inspect the project"))
            session.projection = SessionProjection(
                session_id=session.session_id,
                event_count=1,
                revision=0,
                pending_approval=ProjectionApprovalGroup(
                    group_id="action-set-race",
                    actions=(
                        _approval_action(
                            "tool-race",
                            tool_name="file_editor",
                            summary="Write the synthetic result",
                            group_id="action-set-race",
                        ),
                    ),
                ),
            )
            app._apply_projection(session.projection)
            app._set_busy(False)
            await pilot.pause()

            assert app.query_one("#approval", Vertical).display
            assert app.query_one("#composer", Input).disabled
            assert app.query_one("#approval-options", OptionList).has_focus

    asyncio.run(exercise())


def test_textual_terminal_keeps_the_first_long_action_visible() -> None:
    class LongBatchSession(InteractiveSession):
        def __init__(self) -> None:
            self.session_id = "long-batch"
            self.projection = SessionProjection(
                session_id=self.session_id,
                event_count=8,
                revision=7,
                pending_approval=ProjectionApprovalGroup(
                    group_id="action-set-long",
                    actions=tuple(
                        _approval_action(
                            f"tool-{index}",
                            tool_name="terminal",
                            risk="medium",
                            summary=f"Review step {index}",
                            arguments={
                                "command": (
                                    f"python analyze.py --step {index} --output result-{index}.json"
                                )
                            },
                            group_id="action-set-long",
                            sequence=index,
                        )
                        for index in range(1, 9)
                    ),
                ),
            )

        def replay(self) -> SessionProjection:
            return self.projection

        def action_settings(self) -> ActionSettingsResponse:
            return _test_action_settings()

    async def exercise() -> None:
        app = HeartwoodTerminalApp(LongBatchSession())
        async with app.run_test(size=(64, 18)) as pilot:
            await _wait_for_tui(
                pilot,
                lambda: (
                    app.query_one("#approval", Vertical).display
                    and "1. Review step 1" in str(app.query_one("#approval-actions", RichLog).lines)
                ),
                description="the long action review",
            )
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

        def replay(self) -> SessionProjection:
            return SessionProjection(
                session_id=self.session_id,
                event_count=0,
                revision=-1,
            )

        def action_settings(self) -> ActionSettingsResponse:
            return _test_action_settings()

    async def exercise() -> None:
        app = HeartwoodTerminalApp(IdleSession())
        assert app._idle_status("ready") == "Ready · Action Review"
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
