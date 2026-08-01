# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Conformance tests for the gateway-owned interface projection."""

from __future__ import annotations

import pytest

from heartwood.gateway import SessionLifecycle, project_session
from heartwood.session import EventKind, JsonValue, SessionEvent


def test_projection_replays_lifecycle_tasks_usage_and_subagent_lineage() -> None:
    events = (
        _event(
            0,
            EventKind.USER_MESSAGE_RECORDED,
            {"command_id": "command-1", "content": "Analyze the synthetic cohort"},
        ),
        _event(1, EventKind.AGENT_LIFECYCLE_UPDATED, {"status": "running"}),
        _event(
            2,
            EventKind.TASK_PLAN_UPDATED,
            {
                "tasks": [
                    {
                        "title": "Inspect the cohort",
                        "status": "in-progress",
                        "notes": "This private SDK note must not enter the projection.",
                    }
                ]
            },
        ),
        _usage_event(3, "total", calls=3, prompt_tokens=150, completion_tokens=30),
        _usage_event(4, "agent", calls=2, prompt_tokens=120, completion_tokens=25),
        _usage_event(5, "condenser", calls=1, prompt_tokens=30, completion_tokens=5),
        _event(
            6,
            EventKind.SUBAGENT_UPDATED,
            {
                "subagent": {
                    "invocation_id": "task-plan-call",
                    "task_id": "task-plan",
                    "agent_name": "research-planner",
                    "status": "proposed",
                    "parent_session_id": "session-1",
                    "parent_action_id": "action-1",
                }
            },
        ),
        _event(
            7,
            EventKind.SUBAGENT_UPDATED,
            {
                "subagent": {
                    "invocation_id": "task-plan-call",
                    "task_id": "task-plan",
                    "agent_name": "research-planner",
                    "status": "completed",
                    "parent_session_id": "session-1",
                    "parent_action_id": "action-1",
                }
            },
        ),
        _event(8, EventKind.AGENT_MESSAGE_EMITTED, {"content": "Analysis complete."}),
        _event(9, EventKind.AGENT_LIFECYCLE_UPDATED, {"status": "finished"}),
    )

    projection = project_session(
        events,
        session_id="session-1",
        streaming_text="transient token",
    )

    assert projection == project_session(
        events,
        session_id="session-1",
        streaming_text="transient token",
    )
    assert projection.lifecycle.status == SessionLifecycle.FINISHED
    assert projection.event_count == len(events)
    assert projection.revision == 9
    assert projection.streaming_text == ""
    assert projection.task_plan[0].model_dump() == {
        "title": "Inspect the cohort",
        "status": "in-progress",
    }
    assert projection.usage is not None
    assert projection.usage.call_count == 3
    assert [usage.usage_id for usage in projection.usage_by_purpose] == [
        "agent",
        "condenser",
    ]
    assert projection.subagents[0].status == "completed"
    assert projection.subagents[0].parent_session_id == "session-1"
    assert "private SDK note" not in str(projection.safe_dict())


def test_projection_represents_one_atomic_decision_for_a_grouped_action_set() -> None:
    events = (
        _confirmation_event(0, "group-1", "call-1", "terminal"),
        _confirmation_event(1, "group-1", "call-2", "file_editor"),
        _event(
            2,
            EventKind.CONFIRMATION_RESOLVED,
            {
                "group_id": "group-1",
                "tool_call_id": "call-1",
                "decision": "approved",
            },
        ),
        _event(
            3,
            EventKind.CONFIRMATION_RESOLVED,
            {
                "group_id": "group-1",
                "tool_call_id": "call-2",
                "decision": "approved",
            },
        ),
    )

    projection = project_session(events, session_id="session-1")

    assert projection.pending_approval is None
    assert projection.lifecycle.status == SessionLifecycle.IDLE
    approval_messages = [item for item in projection.conversation if item.label == "Approval"]
    assert len(approval_messages) == 1
    assert approval_messages[0].content == "Action set approved (2 actions)"


def test_atomic_approval_record_survives_every_group_resolution_boundary() -> None:
    requested = (
        _confirmation_event(0, "group-1", "call-1", "terminal"),
        _confirmation_event(1, "group-1", "call-2", "file_editor"),
    )
    approval = _event(
        2,
        EventKind.APPROVAL_RECORDED,
        {
            "group_id": "group-1",
            "decision": "approved",
            "tool_call_ids": ["call-1", "call-2"],
        },
    )
    resolutions = (
        _event(
            3,
            EventKind.CONFIRMATION_RESOLVED,
            {
                "group_id": "group-1",
                "tool_call_id": "call-1",
                "decision": "approved",
            },
        ),
        _event(
            4,
            EventKind.CONFIRMATION_RESOLVED,
            {
                "group_id": "group-1",
                "tool_call_id": "call-2",
                "decision": "approved",
            },
        ),
    )

    before_decision = project_session(requested, session_id="session-1")
    assert before_decision.pending_approval is not None
    assert {action.state for action in before_decision.actions} == {"awaiting-review"}

    for boundary in range(len(resolutions) + 1):
        projection = project_session(
            (*requested, approval, *resolutions[:boundary]),
            session_id="session-1",
        )
        assert projection.pending_approval is None
        assert projection.lifecycle.status == SessionLifecycle.IDLE
        assert {action.state for action in projection.actions} == {"approved"}
        assert {action.decision for action in projection.actions} == {"approved"}
        assert len([item for item in projection.conversation if item.label == "Approval"]) == 1


@pytest.mark.parametrize("decision", ["unexpected", "", None])
def test_invalid_confirmation_decisions_fail_closed(
    decision: str | None,
) -> None:
    projection = project_session(
        (
            _confirmation_event(0, "group-1", "call-1", "terminal"),
            _event(
                1,
                EventKind.CONFIRMATION_RESOLVED,
                {
                    "group_id": "group-1",
                    "tool_call_id": "call-1",
                    "decision": decision,
                },
            ),
        ),
        session_id="session-1",
    )

    assert projection.pending_approval is not None
    assert projection.lifecycle.status == SessionLifecycle.WAITING_FOR_CONFIRMATION
    assert projection.actions[0].state == "awaiting-review"
    assert projection.actions[0].decision is None


def test_partial_legacy_group_resolution_remains_pending() -> None:
    projection = project_session(
        (
            _confirmation_event(0, "group-1", "call-1", "terminal"),
            _confirmation_event(1, "group-1", "call-2", "file_editor"),
            _event(
                2,
                EventKind.CONFIRMATION_RESOLVED,
                {
                    "group_id": "group-1",
                    "tool_call_id": "call-1",
                    "decision": "approved",
                },
            ),
        ),
        session_id="session-1",
    )

    assert projection.pending_approval is not None
    assert projection.lifecycle.status == SessionLifecycle.WAITING_FOR_CONFIRMATION
    assert [action.state for action in projection.actions] == [
        "approved",
        "awaiting-review",
    ]
    assert not [item for item in projection.conversation if item.label == "Approval"]


def test_mixed_group_resolutions_fail_closed() -> None:
    projection = project_session(
        (
            _confirmation_event(0, "group-1", "call-1", "terminal"),
            _confirmation_event(1, "group-1", "call-2", "file_editor"),
            _event(
                2,
                EventKind.CONFIRMATION_RESOLVED,
                {
                    "group_id": "group-1",
                    "tool_call_id": "call-1",
                    "decision": "approved",
                },
            ),
            _event(
                3,
                EventKind.CONFIRMATION_RESOLVED,
                {
                    "group_id": "group-1",
                    "tool_call_id": "call-2",
                    "decision": "denied",
                },
            ),
        ),
        session_id="session-1",
    )

    assert projection.pending_approval is not None
    assert projection.lifecycle.status == SessionLifecycle.WAITING_FOR_CONFIRMATION
    assert not [item for item in projection.conversation if item.label == "Approval"]


def test_execution_after_rejection_fails_closed_as_an_integrity_error() -> None:
    projection = project_session(
        (
            _event(
                0,
                EventKind.TOOL_CALL_PROPOSED,
                {
                    "tool_call_id": "call-1",
                    "tool_name": "terminal",
                    "kind": "terminal",
                    "risk": "medium",
                    "arguments": {"command": "printf synthetic"},
                },
            ),
            _confirmation_event(1, "group-1", "call-1", "terminal"),
            _event(
                2,
                EventKind.APPROVAL_RECORDED,
                {
                    "group_id": "group-1",
                    "decision": "denied",
                    "tool_call_ids": ["call-1"],
                },
            ),
            _event(
                3,
                EventKind.TOOL_EXECUTION_RECORDED,
                {
                    "tool_call_id": "call-1",
                    "tool_name": "terminal",
                    "exit_code": 0,
                },
            ),
        ),
        session_id="session-1",
    )

    assert projection.actions[0].state == "outcome-unknown"
    assert projection.actions[0].decision == "rejected"
    assert projection.actions[0].outcome is None
    assert projection.lifecycle.status == SessionLifecycle.ERROR
    assert projection.lifecycle.can_steer is False
    assert projection.available_commands == ()
    assert any(
        message.content == "Session history failed an integrity check"
        for message in projection.conversation
    )


def test_duplicate_action_proposal_fails_closed_without_replacing_identity() -> None:
    projection = project_session(
        (
            _event(
                0,
                EventKind.TOOL_CALL_PROPOSED,
                {
                    "tool_call_id": "call-1",
                    "action_id": "action-1",
                    "tool_name": "terminal",
                    "kind": "terminal",
                    "risk": "medium",
                    "arguments": {"command": "printf first"},
                },
            ),
            _event(
                1,
                EventKind.TOOL_CALL_PROPOSED,
                {
                    "tool_call_id": "call-1",
                    "action_id": "action-2",
                    "tool_name": "file_editor",
                    "kind": "file-editor",
                    "risk": "low",
                    "arguments": {"command": "create", "path": "replacement.txt"},
                },
            ),
        ),
        session_id="session-1",
    )

    assert len(projection.actions) == 1
    assert projection.actions[0].action_id == "action-1"
    assert projection.actions[0].tool_name == "terminal"
    assert projection.actions[0].state == "outcome-unknown"
    assert projection.lifecycle.status == SessionLifecycle.ERROR
    assert projection.available_commands == ()


@pytest.mark.parametrize(
    ("execution_action_id", "execution_tool_name"),
    [
        ("action-2", "terminal"),
        ("action-1", "file_editor"),
    ],
)
def test_action_execution_identity_mismatch_fails_closed(
    execution_action_id: str,
    execution_tool_name: str,
) -> None:
    projection = project_session(
        (
            _event(
                0,
                EventKind.TOOL_CALL_PROPOSED,
                {
                    "tool_call_id": "call-1",
                    "action_id": "action-1",
                    "tool_name": "terminal",
                    "kind": "terminal",
                    "risk": "medium",
                    "arguments": {"command": "printf safe"},
                },
            ),
            _event(
                1,
                EventKind.TOOL_EXECUTION_RECORDED,
                {
                    "tool_call_id": "call-1",
                    "action_id": execution_action_id,
                    "tool_name": execution_tool_name,
                    "exit_code": 0,
                    "result": "must not be accepted",
                },
            ),
        ),
        session_id="session-1",
    )

    assert projection.actions[0].action_id == "action-1"
    assert projection.actions[0].tool_name == "terminal"
    assert projection.actions[0].state == "outcome-unknown"
    assert projection.actions[0].outcome is None
    assert projection.lifecycle.status == SessionLifecycle.ERROR
    assert projection.available_commands == ()


def test_action_execution_recovers_an_action_id_missing_from_an_older_proposal() -> None:
    projection = project_session(
        (
            _event(
                0,
                EventKind.TOOL_CALL_PROPOSED,
                {
                    "tool_call_id": "call-1",
                    "tool_name": "terminal",
                    "kind": "terminal",
                    "risk": "low",
                    "arguments": {"command": "printf safe"},
                },
            ),
            _event(
                1,
                EventKind.TOOL_EXECUTION_RECORDED,
                {
                    "tool_call_id": "call-1",
                    "action_id": "action-1",
                    "tool_name": "terminal",
                    "exit_code": 0,
                },
            ),
        ),
        session_id="session-1",
    )

    action = projection.actions[0]
    assert action.action_id == "action-1"
    assert action.state == "succeeded"
    assert projection.lifecycle.status != SessionLifecycle.ERROR


def test_resolution_contradicting_atomic_group_decision_fails_closed() -> None:
    projection = project_session(
        (
            _confirmation_event(0, "group-1", "call-1", "terminal"),
            _confirmation_event(1, "group-1", "call-2", "file_editor"),
            _event(
                2,
                EventKind.APPROVAL_RECORDED,
                {
                    "group_id": "group-1",
                    "decision": "approved",
                    "tool_call_ids": ["call-1", "call-2"],
                },
            ),
            _event(
                3,
                EventKind.CONFIRMATION_RESOLVED,
                {
                    "group_id": "group-1",
                    "tool_call_id": "call-1",
                    "decision": "denied",
                },
            ),
        ),
        session_id="session-1",
    )

    assert {action.state for action in projection.actions} == {"outcome-unknown"}
    assert {action.decision for action in projection.actions} == {"approved"}
    assert projection.lifecycle.status == SessionLifecycle.ERROR
    assert projection.available_commands == ()


def test_executed_action_records_automatic_policy_decision_and_workspace_revision() -> None:
    projection = project_session(
        (
            _event(
                0,
                EventKind.TOOL_CALL_PROPOSED,
                {
                    "tool_call_id": "call-1",
                    "tool_name": "terminal",
                    "kind": "terminal",
                    "arguments": {"command": "printf synthetic"},
                    "risk": "low",
                },
            ),
            _event(
                1,
                EventKind.TOOL_EXECUTION_RECORDED,
                {
                    "tool_call_id": "call-1",
                    "tool_name": "terminal",
                    "exit_code": 0,
                    "summary": "terminal completed",
                },
            ),
        ),
        session_id="session-1",
    )

    assert projection.workspace_revision == 1
    assert projection.actions[0].decision == "approved"
    assert projection.actions[0].group_id is None


def test_projection_displays_stable_error_code_without_technical_details() -> None:
    projection = project_session(
        (
            _event(
                0,
                EventKind.ERROR_RECORDED,
                {
                    "backend_id": "openhands-sdk",
                    "code": "HW-AGENT-003",
                    "reason": "The agent conversation stopped",
                },
            ),
        ),
        session_id="session-1",
    )

    assert projection.lifecycle.status == SessionLifecycle.ERROR
    assert projection.lifecycle.can_steer is True
    assert projection.available_commands == ("chat",)
    assert projection.activity[0].detail == "HW-AGENT-003"
    assert projection.conversation[0].detail == ("HW-AGENT-003: The agent conversation stopped")


@pytest.mark.parametrize("error_code", ["HW-AGENT-006", "HW-AGENT-007"])
def test_unknown_execution_outcome_disables_further_session_commands(
    error_code: str,
) -> None:
    projection = project_session(
        (
            _event(0, EventKind.AGENT_LIFECYCLE_UPDATED, {"status": "error"}),
            _event(
                1,
                EventKind.ERROR_RECORDED,
                {
                    "backend_id": "openhands-sdk",
                    "code": error_code,
                    "reason": "The previous execution outcome is unknown",
                },
            ),
        ),
        session_id="session-1",
    )

    assert projection.lifecycle.status == SessionLifecycle.ERROR
    assert projection.lifecycle.can_steer is False
    assert projection.available_commands == ()


def test_later_stable_lifecycle_does_not_reopen_an_outcome_error() -> None:
    projection = project_session(
        (
            _event(
                0,
                EventKind.ERROR_RECORDED,
                {
                    "backend_id": "openhands-sdk",
                    "code": "HW-AGENT-007",
                    "reason": "The previous execution outcome is unknown",
                },
            ),
            _event(1, EventKind.AGENT_LIFECYCLE_UPDATED, {"status": "finished"}),
        ),
        session_id="session-1",
    )

    assert projection.lifecycle.status == SessionLifecycle.ERROR
    assert projection.lifecycle.can_steer is False
    assert projection.available_commands == ()


def test_later_command_error_does_not_reopen_a_fail_closed_session() -> None:
    projection = project_session(
        (
            _event(
                0,
                EventKind.ERROR_RECORDED,
                {
                    "backend_id": "openhands-sdk",
                    "code": "HW-AGENT-006",
                    "reason": "The previous action outcome is unknown",
                },
            ),
            _event(
                1,
                EventKind.ERROR_RECORDED,
                {"command": "chat", "reason": "chat is unavailable while the agent is error"},
            ),
        ),
        session_id="session-1",
    )

    assert projection.lifecycle.status == SessionLifecycle.ERROR
    assert projection.lifecycle.can_steer is False
    assert projection.available_commands == ()


def test_stale_confirmation_resolution_preserves_current_lifecycle() -> None:
    projection = project_session(
        (
            _event(0, EventKind.AGENT_LIFECYCLE_UPDATED, {"status": "running"}),
            _event(
                1,
                EventKind.CONFIRMATION_RESOLVED,
                {
                    "group_id": "stale-group",
                    "tool_call_id": "stale-call",
                    "decision": "denied",
                },
            ),
        ),
        session_id="session-1",
    )

    assert projection.lifecycle.status == SessionLifecycle.RUNNING
    assert projection.available_commands == ("chat", "pause")


def test_duplicate_confirmation_resolution_does_not_override_new_lifecycle() -> None:
    projection = project_session(
        (
            _confirmation_event(0, "group-1", "call-1", "terminal"),
            _event(
                1,
                EventKind.CONFIRMATION_RESOLVED,
                {
                    "group_id": "group-1",
                    "tool_call_id": "call-1",
                    "decision": "approved",
                },
            ),
            _event(2, EventKind.AGENT_LIFECYCLE_UPDATED, {"status": "running"}),
            _event(
                3,
                EventKind.CONFIRMATION_RESOLVED,
                {
                    "group_id": "group-1",
                    "tool_call_id": "call-1",
                    "decision": "approved",
                },
            ),
        ),
        session_id="session-1",
    )

    assert projection.lifecycle.status == SessionLifecycle.RUNNING
    assert projection.available_commands == ("chat", "pause")


def test_projection_owns_nonfatal_command_outcomes() -> None:
    projection = project_session(
        (
            _event(
                0,
                EventKind.COMMAND_RECEIVED,
                {
                    "command_id": "resume-1",
                    "command_kind": "resume",
                },
            ),
            _event(
                1,
                EventKind.ERROR_RECORDED,
                {
                    "code": "HW-AGENT-005",
                    "reason": "Resume is unavailable while the agent is running",
                    "affects_lifecycle": False,
                },
            ),
        ),
        session_id="session-1",
        stream_epoch="process-1",
    )

    assert projection.lifecycle.status == SessionLifecycle.IDLE
    assert projection.stream_epoch == "process-1"
    assert projection.last_command_outcome is not None
    assert projection.last_command_outcome.status == "rejected"
    assert projection.last_command_outcome.command_id == "resume-1"
    assert projection.last_command_outcome.error_code == "HW-AGENT-005"


def test_projection_preserves_tool_outcome_in_shared_activity() -> None:
    projection = project_session(
        (
            _event(
                0,
                EventKind.TOOL_EXECUTION_RECORDED,
                {
                    "tool_name": "terminal",
                    "tool_call_id": "call-1",
                    "exit_code": 1,
                },
            ),
        ),
        session_id="session-1",
    )

    assert projection.activity[0].detail == "terminal · exit=1"
    assert projection.actions[0].state == "outcome-unknown"
    assert projection.lifecycle.status == SessionLifecycle.ERROR


def test_projection_correlates_typed_action_review_execution_and_paths() -> None:
    projection = project_session(
        (
            _event(
                0,
                EventKind.TOOL_CALL_PROPOSED,
                {
                    "tool_call_id": "call-1",
                    "action_id": "action-1",
                    "tool_name": "file_editor",
                    "kind": "file-editor",
                    "risk": "medium",
                    "summary": "Create the synthetic result",
                    "arguments": {
                        "command": "create",
                        "path": "/project/results/summary.txt",
                        "file_text": "synthetic\n",
                    },
                    "affected_paths": [
                        "results/summary.txt",
                        "../outside.txt",
                        ".heartwood/private.txt",
                        ".git/config",
                        "results//duplicate.txt",
                        "results/\nunsafe.txt",
                    ],
                    "project_path": "results/summary.txt",
                },
            ),
            _confirmation_event(1, "group-1", "call-1", "file_editor"),
            _event(
                2,
                EventKind.CONFIRMATION_RESOLVED,
                {
                    "group_id": "group-1",
                    "tool_call_id": "call-1",
                    "decision": "approved",
                },
            ),
            _event(
                3,
                EventKind.TOOL_EXECUTION_RECORDED,
                {
                    "tool_call_id": "call-1",
                    "action_id": "action-1",
                    "tool_name": "file_editor",
                    "exit_code": 0,
                    "summary": "file editor completed",
                    "result": "Created results/summary.txt",
                    "result_truncated": False,
                },
            ),
        ),
        session_id="session-1",
    )

    assert len(projection.actions) == 1
    action = projection.actions[0]
    assert action.schema_version == "heartwood.action-record.v1"
    assert action.action_id == "action-1"
    assert action.tool_call_id == "call-1"
    assert action.group_id == "group-1"
    assert action.state == "succeeded"
    assert action.decision == "approved"
    assert action.details.kind == "file-editor"
    assert action.details.path == "results/summary.txt"
    assert [item.path for item in action.affected_paths] == ["results/summary.txt"]
    assert action.affected_paths[0].effect == "created"
    assert action.outcome is not None
    assert action.outcome.exit_code == 0
    assert action.outcome.result == "Created results/summary.txt"
    assert projection.pending_approval is None
    assert "/project/results/summary.txt" in str(action.arguments)
    assert "../outside.txt" not in str(action.affected_paths)
    assert ".heartwood" not in str(action.affected_paths)
    assert ".git" not in str(action.affected_paths)
    assert "duplicate" not in str(action.affected_paths)


def test_projection_keeps_a_safe_path_for_read_only_file_actions() -> None:
    projection = project_session(
        (
            _event(
                0,
                EventKind.TOOL_CALL_PROPOSED,
                {
                    "tool_call_id": "call-view",
                    "action_id": "action-view",
                    "tool_name": "file_editor",
                    "kind": "file-editor",
                    "risk": "low",
                    "summary": "Inspect the result",
                    "arguments": {
                        "command": "view",
                        "path": "/project/results/summary.txt",
                    },
                    "affected_paths": [],
                    "project_path": "results/summary.txt",
                },
            ),
        ),
        session_id="session-1",
    )

    action = projection.actions[0]
    assert action.details.kind == "file-editor"
    assert action.details.operation == "view"
    assert action.details.path == "results/summary.txt"
    assert action.affected_paths == ()


def test_projection_uses_same_action_records_for_grouped_review() -> None:
    projection = project_session(
        (
            _event(
                0,
                EventKind.TOOL_CALL_PROPOSED,
                {
                    "tool_call_id": "call-terminal",
                    "action_id": "action-terminal",
                    "tool_name": "terminal",
                    "kind": "terminal",
                    "risk": "low",
                    "summary": "Run focused tests",
                    "arguments": {
                        "command": "pytest tests/test_analysis.py",
                        "timeout": 120,
                    },
                    "affected_paths": [],
                },
            ),
            _confirmation_event(1, "group-1", "call-terminal", "terminal"),
            _event(
                2,
                EventKind.TOOL_CALL_PROPOSED,
                {
                    "tool_call_id": "call-task",
                    "action_id": "action-task",
                    "tool_name": "task",
                    "kind": "task",
                    "risk": "medium",
                    "summary": "Ask the research planner",
                    "arguments": {
                        "description": "Plan the analysis",
                        "prompt": "Review the synthetic cohort task",
                        "subagent_type": "research-planner",
                    },
                    "affected_paths": [],
                },
            ),
            _confirmation_event(3, "group-1", "call-task", "task"),
        ),
        session_id="session-1",
    )

    approval = projection.pending_approval
    assert approval is not None
    assert approval.decision_scope == "all"
    assert approval.actions == projection.actions
    assert [action.state for action in approval.actions] == [
        "awaiting-review",
        "awaiting-review",
    ]
    terminal = approval.actions[0]
    assert terminal.details.kind == "terminal"
    assert terminal.details.command == "pytest tests/test_analysis.py"
    assert terminal.details.timeout == 120
    task = approval.actions[1]
    assert task.details.kind == "task"
    assert task.details.subagent_type == "research-planner"


def test_projection_derives_every_recoverable_action_state_from_events() -> None:
    proposed = _event(
        0,
        EventKind.TOOL_CALL_PROPOSED,
        {
            "tool_call_id": "call-1",
            "action_id": "action-1",
            "tool_name": "terminal",
            "kind": "terminal",
            "risk": "low",
            "summary": "Run focused tests",
            "arguments": {"command": "pytest tests/test_analysis.py"},
            "affected_paths": [],
        },
    )
    awaiting = _confirmation_event(1, "group-1", "call-1", "terminal")
    approved = _event(
        2,
        EventKind.CONFIRMATION_RESOLVED,
        {
            "group_id": "group-1",
            "tool_call_id": "call-1",
            "decision": "approved",
        },
    )
    rejected = approved.model_copy(
        update={
            "event_id": "event-rejected",
            "payload": {
                **approved.payload,
                "decision": "denied",
            },
        }
    )
    running = _event(3, EventKind.AGENT_LIFECYCLE_UPDATED, {"status": "running"})
    failed = _event(
        3,
        EventKind.TOOL_EXECUTION_RECORDED,
        {
            "tool_call_id": "call-1",
            "action_id": "action-1",
            "tool_name": "terminal",
            "exit_code": 1,
            "summary": "focused tests failed",
        },
    )

    assert project_session((proposed,), session_id="session-1").actions[0].state == "proposed"
    assert (
        project_session((proposed, awaiting), session_id="session-1").actions[0].state
        == "awaiting-review"
    )
    assert (
        project_session((proposed, awaiting, approved), session_id="session-1").actions[0].state
        == "approved"
    )
    assert (
        project_session((proposed, awaiting, rejected), session_id="session-1").actions[0].state
        == "rejected"
    )
    running_action = project_session(
        (proposed, awaiting, approved, running),
        session_id="session-1",
    ).actions[0]
    assert running_action.state == "running"
    assert running_action.updated_sequence == 3
    assert (
        project_session(
            (proposed, awaiting, approved, failed),
            session_id="session-1",
        )
        .actions[0]
        .state
        == "failed"
    )


def test_projection_marks_unresolved_actions_outcome_unknown_and_fail_closed() -> None:
    projection = project_session(
        (
            _event(
                0,
                EventKind.TOOL_CALL_PROPOSED,
                {
                    "tool_call_id": "call-1",
                    "action_id": "action-1",
                    "tool_name": "terminal",
                    "kind": "terminal",
                    "risk": "medium",
                    "summary": "Run a command",
                    "arguments": {"command": "python analysis.py"},
                    "affected_paths": [],
                },
            ),
            _event(
                1,
                EventKind.CONFIRMATION_RESOLVED,
                {
                    "group_id": "group-1",
                    "tool_call_id": "call-1",
                    "decision": "approved",
                },
            ),
            _event(
                2,
                EventKind.ERROR_RECORDED,
                {
                    "code": "HW-AGENT-006",
                    "reason": "The previous action outcome is unknown",
                },
            ),
        ),
        session_id="session-1",
    )

    assert projection.actions[0].state == "outcome-unknown"
    assert projection.actions[0].updated_sequence == 2
    assert projection.lifecycle.can_steer is False
    assert projection.available_commands == ()


def test_failed_group_resolution_replays_as_a_fatal_unknown_outcome() -> None:
    projection = project_session(
        (
            _confirmation_event(0, "group-1", "call-1", "terminal"),
            _event(
                1,
                EventKind.APPROVAL_RECORDED,
                {
                    "group_id": "group-1",
                    "decision": "approved",
                    "tool_call_ids": ["call-1"],
                },
            ),
            _event(
                2,
                EventKind.ERROR_RECORDED,
                {
                    "code": "HW-AGENT-006",
                    "reason": "The approved action outcome is unknown",
                },
            ),
        ),
        session_id="session-1",
    )

    assert projection.pending_approval is None
    assert projection.actions[0].state == "outcome-unknown"
    assert projection.lifecycle.status == SessionLifecycle.ERROR
    assert projection.available_commands == ()


def test_projection_normalizes_incomplete_runtime_events_without_client_logic() -> None:
    events = (
        _event(0, EventKind.AGENT_MESSAGE_EMITTED, {"content": ""}),
        _event(
            1,
            EventKind.TASK_PLAN_UPDATED,
            {
                "tasks": [
                    {"title": "Finished", "status": "done"},
                    {"title": "Unrecognized", "status": "unexpected"},
                ]
            },
        ),
        _event(2, EventKind.MODEL_USAGE_UPDATED, {"usage": {"usage_id": "agent"}}),
        _event(
            3,
            EventKind.SUBAGENT_UPDATED,
            {
                "subagent": {
                    "invocation_id": "task-error-call",
                    "task_id": "task-error",
                    "agent_name": "research-planner",
                    "status": "error",
                    "parent_session_id": "session-1",
                    "parent_action_id": "action-1",
                }
            },
        ),
        _event(
            4,
            EventKind.SUBAGENT_UPDATED,
            {
                "subagent": {
                    "invocation_id": "task-running-call",
                    "task_id": "task-running",
                    "agent_name": "research-planner",
                    "status": "unexpected",
                    "parent_session_id": "session-1",
                    "parent_action_id": "action-2",
                }
            },
        ),
        _event(6, EventKind.USER_MESSAGE_RECORDED, {"command_id": 17, "content": 23}),
        _event(
            7,
            EventKind.MODEL_USAGE_UPDATED,
            {
                "usage": {
                    "usage_id": "total",
                    "model_name": "synthetic-model",
                    "call_count": -1,
                    "prompt_tokens": True,
                    "completion_tokens": 5,
                    "accumulated_cost": -2,
                }
            },
        ),
        _event(
            8,
            EventKind.CONFIRMATION_RESOLVED,
            {
                "group_id": "missing-group",
                "tool_call_id": "missing-call",
                "decision": "denied",
            },
        ),
        _event(9, EventKind.AGENT_LIFECYCLE_UPDATED, {"status": "unexpected"}),
    )

    projection = project_session(events, session_id="session-1")

    assert [task.status for task in projection.task_plan] == ["done", "todo"]
    assert [agent.status for agent in projection.subagents] == ["error", "running"]
    assert projection.lifecycle.status == SessionLifecycle.ERROR
    assert projection.conversation[-1].content == "23"
    assert projection.usage is not None
    assert projection.usage.call_count == 0
    assert projection.usage.prompt_tokens == 0
    assert projection.usage.accumulated_cost == 0


def test_projection_normalizes_malformed_action_enums_without_raising() -> None:
    projection = project_session(
        (
            _event(
                0,
                EventKind.TOOL_CALL_PROPOSED,
                {
                    "tool_call_id": "call-1",
                    "tool_name": "unexpected",
                    "kind": ["terminal"],
                    "risk": {"level": "high"},
                    "arguments": {},
                },
            ),
            _confirmation_event(1, "group-1", "call-1", "unexpected"),
            _event(
                2,
                EventKind.CONFIRMATION_RESOLVED,
                {
                    "group_id": "group-1",
                    "tool_call_id": "call-1",
                    "decision": ["approved"],
                },
            ),
        ),
        session_id="session-1",
    )

    action = projection.actions[0]
    assert action.details.kind == "other"
    assert action.risk == "unknown"
    assert action.state == "awaiting-review"
    assert projection.pending_approval is not None


def _event(
    sequence: int,
    kind: EventKind,
    payload: dict[str, JsonValue],
) -> SessionEvent:
    return SessionEvent(
        event_id=f"event-{sequence}",
        session_id="session-1",
        sequence=sequence,
        kind=kind,
        occurred_at="2026-01-01T00:00:00Z",
        payload=payload,
    )


def _usage_event(
    sequence: int,
    usage_id: str,
    *,
    calls: int,
    prompt_tokens: int,
    completion_tokens: int,
) -> SessionEvent:
    return _event(
        sequence,
        EventKind.MODEL_USAGE_UPDATED,
        {
            "usage": {
                "usage_id": usage_id,
                "model_name": "synthetic-model",
                "call_count": calls,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "reasoning_tokens": 0,
                "context_window": 32_768,
                "accumulated_cost": 0.0,
            }
        },
    )


def _confirmation_event(
    sequence: int,
    group_id: str,
    tool_call_id: str,
    tool_name: str,
) -> SessionEvent:
    return _event(
        sequence,
        EventKind.CONFIRMATION_REQUESTED,
        {
            "request": {
                "request_id": f"{tool_call_id}-confirm",
                "session_id": "session-1",
                "group_id": group_id,
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "risk": "medium",
                "summary": f"Run {tool_name}",
                "arguments": {},
            }
        },
    )
