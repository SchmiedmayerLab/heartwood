# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Conformance tests for the gateway-owned interface projection."""

from __future__ import annotations

import pytest

from heartwood.core_adapter import BackendErrorCode
from heartwood.core_adapter.action_review import AUTOMATIC_REVIEW_ACTOR
from heartwood.core_adapter.session_activity import session_activity
from heartwood.gateway import (
    ProjectionTaskActionDetails,
    SessionLifecycle,
    action_heading,
    project_session,
    specialist_review_lines,
)
from heartwood.schemas.execution import NativeTaskExecution
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
                    "native_execution": {
                        "clock_id": "025c5e8f-b5dd-4c1e-a86e-85f122abfbc6",
                        "started_seconds": 1.0,
                        "finished_seconds": 2.0,
                    },
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
        "status_label": "In Progress",
    }
    assert projection.usage is not None
    assert projection.usage.call_count == 3
    assert projection.usage.purpose_label == "Total Model Activity"
    assert [usage.usage_id for usage in projection.usage_by_purpose] == [
        "agent",
        "condenser",
    ]
    assert projection.subagents[0].status == "completed"
    assert projection.subagents[0].role_label == "Research Planner"
    assert projection.subagents[0].status_label == "Complete"
    assert projection.subagents[0].parent_session_id == "session-1"
    native_subagent = events[7].payload["subagent"]
    assert isinstance(native_subagent, dict)
    assert projection.subagents[0].native_execution == NativeTaskExecution.model_validate(
        native_subagent["native_execution"]
    )
    assert projection.researcher_status.code == "complete"
    assert [item.suggestion_id for item in projection.suggestions] == [
        "continue-plan",
        "verify-work",
    ]
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


def test_rejected_action_set_owns_the_researcher_status() -> None:
    projection = project_session(
        (
            _confirmation_event(0, "group-1", "call-1", "terminal"),
            _event(
                1,
                EventKind.APPROVAL_RECORDED,
                {
                    "group_id": "group-1",
                    "decision": "denied",
                    "tool_call_ids": ["call-1"],
                },
            ),
            _event(
                2,
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

    assert projection.lifecycle.status == SessionLifecycle.IDLE
    assert projection.researcher_status.code == "denied"
    assert projection.researcher_status.label == "Action Set Rejected"
    assert projection.researcher_notice is None
    assert projection.available_commands == ("chat",)


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


@pytest.mark.parametrize("approved_first", [False, True])
def test_confirmation_before_first_proposal_preserves_the_review_state(
    approved_first: bool,
) -> None:
    payload: dict[str, JsonValue] = {
        "tool_call_id": "call-1",
        "action_id": "action-1",
        "tool_name": "terminal",
        "kind": "terminal",
        "risk": "medium",
        "arguments": {"command": "printf synthetic"},
    }
    events = [
        _event(
            0,
            EventKind.CONFIRMATION_REQUESTED,
            {"request": {**payload, "group_id": "group-1", "request_id": "request-1"}},
        )
    ]
    if approved_first:
        events.append(
            _event(
                1,
                EventKind.APPROVAL_RECORDED,
                {"group_id": "group-1", "decision": "approved", "tool_call_ids": ["call-1"]},
            )
        )
    events.append(_event(len(events), EventKind.TOOL_CALL_PROPOSED, payload))
    projection = project_session(tuple(events), session_id="session-1")
    assert projection.lifecycle.status != SessionLifecycle.ERROR
    assert projection.actions[0].group_id == "group-1"
    assert projection.actions[0].arguments == payload["arguments"]
    assert projection.actions[0].decision == ("approved" if approved_first else None)
    if not approved_first:
        assert projection.pending_approval is not None
        assert projection.pending_approval.group_id == "group-1"
    duplicate = project_session(
        (*events, _event(len(events), EventKind.TOOL_CALL_PROPOSED, payload)),
        session_id="session-1",
    )
    assert duplicate.lifecycle.status == SessionLifecycle.ERROR


@pytest.mark.parametrize("changed_field", ["action_id", "tool_name", "arguments"])
def test_late_proposal_cannot_replace_previously_reviewed_action_details(
    changed_field: str,
) -> None:
    payload: dict[str, JsonValue] = {
        "tool_call_id": "call-1",
        "action_id": "action-1",
        "tool_name": "terminal",
        "kind": "terminal",
        "risk": "medium",
        "arguments": {"command": "printf synthetic"},
    }
    proposal = dict(payload)
    proposal[changed_field] = (
        {"command": "different"} if changed_field == "arguments" else "different"
    )
    projection = project_session(
        (
            _event(
                0, EventKind.CONFIRMATION_REQUESTED, {"request": {**payload, "group_id": "group-1"}}
            ),
            _event(1, EventKind.TOOL_CALL_PROPOSED, proposal),
        ),
        session_id="session-1",
    )
    assert projection.lifecycle.status == SessionLifecycle.ERROR
    assert projection.actions[0].state == "outcome-unknown"
    assert projection.actions[0].arguments == payload["arguments"]


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


def test_execution_without_a_recorded_decision_is_never_shown_as_approved() -> None:
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
    assert projection.actions[0].decision is None
    assert projection.actions[0].automatic_rule is None
    assert projection.actions[0].group_id is None


def _reviewed_events(approval: dict[str, JsonValue]) -> tuple[SessionEvent, ...]:
    action: dict[str, JsonValue] = {
        "tool_call_id": "call-1",
        "tool_name": "file_editor",
        "kind": "file-editor",
        "arguments": {"command": "view", "path": "data.csv"},
        "risk": "low",
    }
    return (
        _event(0, EventKind.TOOL_CALL_PROPOSED, action),
        _event(1, EventKind.CONFIRMATION_REQUESTED, {"request": {**action, "group_id": "set-1"}}),
        _event(
            2,
            EventKind.APPROVAL_RECORDED,
            {"group_id": "set-1", "decision": "approved", "tool_call_ids": ["call-1"], **approval},
        ),
        _event(
            3,
            EventKind.CONFIRMATION_RESOLVED,
            {"group_id": "set-1", "tool_call_id": "call-1", "decision": "approved"},
        ),
        _event(
            4,
            EventKind.TOOL_EXECUTION_RECORDED,
            {"tool_call_id": "call-1", "tool_name": "file_editor", "exit_code": 0},
        ),
    )


def test_a_withdrawn_gateway_decision_returns_the_action_set_to_the_researcher() -> None:
    reviewed = _reviewed_events(
        {
            "actor_id": "gateway",
            "command_id": "automatic-review-set-1",
            "review": {"mode": "review-changes", "rule": "read.file-view"},
        }
    )[:3]
    withdrawn = project_session(
        (
            *reviewed,
            _event(
                3,
                EventKind.ERROR_RECORDED,
                {
                    "command": "approve",
                    "command_id": "automatic-review-set-1",
                    "group_id": "set-1",
                    "automatic_review": "not-applied",
                    "affects_lifecycle": False,
                },
            ),
        ),
        session_id="session-1",
    )

    assert withdrawn.pending_approval is not None
    assert withdrawn.pending_approval.group_id == "set-1"
    assert withdrawn.actions[0].state == "awaiting-review"
    assert withdrawn.actions[0].decision is None
    assert withdrawn.actions[0].automatic_rule is None
    assert withdrawn.lifecycle.status == SessionLifecycle.WAITING_FOR_CONFIRMATION
    assert project_session(reviewed, session_id="session-1").pending_approval is None


def test_gateway_decisions_are_distinct_from_researcher_approvals() -> None:
    review: dict[str, JsonValue] = {"mode": "review-changes", "rule": "read.file-view"}
    automatic = project_session(
        _reviewed_events({"actor_id": "gateway", "review": review}), session_id="session-1"
    )
    researcher = project_session(_reviewed_events({"actor_id": "human"}), session_id="session-1")
    forged = project_session(
        _reviewed_events({"actor_id": "human", "review": review}), session_id="session-1"
    )

    assert automatic.actions[0].decision == researcher.actions[0].decision == "approved"
    assert automatic.actions[0].state == researcher.actions[0].state == "succeeded"
    assert automatic.actions[0].automatic_rule == "read.file-view"
    assert researcher.actions[0].automatic_rule is None
    assert forged.actions[0].automatic_rule is None
    assert automatic.pending_approval is None
    assert [message.content for message in automatic.conversation] != [
        message.content for message in researcher.conversation
    ]
    assert automatic.model_dump(mode="json", by_alias=True)["actions"][0]["automaticRule"] == (
        "read.file-view"
    )


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
                    "code": BackendErrorCode.ACTION_OUTCOME_UNKNOWN.value,
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
    assert projection.researcher_status.code == "ready"
    assert projection.researcher_notice is not None
    assert projection.researcher_notice.notice_id == "command:resume-1:rejected"
    assert projection.researcher_notice.code == "request-not-applied"
    assert projection.researcher_notice.label == "Request Not Applied"
    assert projection.researcher_notice.tone == "attention"


def test_nonfatal_command_rejection_does_not_hide_active_lifecycle() -> None:
    projection = project_session(
        (
            _event(0, EventKind.AGENT_LIFECYCLE_UPDATED, {"status": "running"}),
            _event(
                1,
                EventKind.COMMAND_RECEIVED,
                {
                    "command_id": "pause-race",
                    "command_kind": "pause",
                },
            ),
            _event(
                2,
                EventKind.ERROR_RECORDED,
                {
                    "code": "HW-AGENT-005",
                    "reason": "The task reached a stable boundary before pause was applied",
                    "affects_lifecycle": False,
                },
            ),
        ),
        session_id="session-1",
    )

    assert projection.lifecycle.status == SessionLifecycle.RUNNING
    assert projection.researcher_status.code == "working"
    assert projection.researcher_notice is not None
    assert projection.researcher_notice.label == "Request Not Applied"
    assert projection.researcher_notice.detail == (
        "HW-AGENT-005: The task reached a stable boundary before pause was applied"
    )


@pytest.mark.parametrize(
    ("scenario", "expected_suggestion_ids"),
    [
        ("ready", ("inspect-project", "plan-project")),
        ("running", ()),
        ("recoverable-error", ("recover-task",)),
        ("terminal-error", ()),
    ],
    ids=("ready", "running", "recoverable-error", "terminal-error"),
)
def test_projection_owns_bounded_contextual_suggestions(
    scenario: str,
    expected_suggestion_ids: tuple[str, ...],
) -> None:
    events: tuple[SessionEvent, ...] = ()
    if scenario == "running":
        events = (_event(0, EventKind.AGENT_LIFECYCLE_UPDATED, {"status": "running"}),)
    elif scenario == "recoverable-error":
        events = (
            _event(
                0,
                EventKind.ERROR_RECORDED,
                {
                    "code": "HW-AGENT-003",
                    "reason": "The agent conversation stopped",
                },
            ),
        )
    elif scenario == "terminal-error":
        events = (
            _event(
                0,
                EventKind.ERROR_RECORDED,
                {
                    "code": "HW-AGENT-007",
                    "reason": "The agent outcome is unknown",
                },
            ),
        )
    projection = project_session(events, session_id="session-1")

    assert tuple(item.suggestion_id for item in projection.suggestions) == (expected_suggestion_ids)


def test_projection_enriches_specialist_presentation_without_client_reducers() -> None:
    projection = project_session(
        (
            _event(
                0,
                EventKind.TOOL_CALL_PROPOSED,
                {
                    "tool_call_id": "task-call-parent",
                    "action_id": "action-1",
                    "tool_name": "task",
                    "kind": "task",
                    "risk": "low",
                    "specialist_label": "Research Planner",
                    "specialist_capability": "advisory",
                    "arguments": {
                        "description": "Do not use this parent-action fallback",
                        "subagent_type": "research-planner",
                    },
                },
            ),
            _event(
                1,
                EventKind.TOOL_CALL_PROPOSED,
                {
                    "tool_call_id": "task-call-1",
                    "action_id": "action-2",
                    "tool_name": "task",
                    "kind": "task",
                    "risk": "low",
                    "specialist_label": "Research Planner",
                    "specialist_capability": "advisory",
                    "arguments": {
                        "description": "Review the synthetic analysis plan",
                        "subagent_type": "research-planner",
                    },
                },
            ),
            _event(
                2,
                EventKind.SUBAGENT_UPDATED,
                {
                    "subagent": {
                        "invocation_id": "task-call-1",
                        "task_id": "task-1",
                        "agent_name": "research-planner",
                        "status": "completed",
                        "parent_session_id": "session-1",
                        "parent_action_id": "action-1",
                    }
                },
            ),
            _event(
                3,
                EventKind.TOOL_EXECUTION_RECORDED,
                {
                    "tool_call_id": "task-call-1",
                    "action_id": "action-2",
                    "tool_name": "task",
                    "exit_code": 0,
                    "summary": "Plan review completed",
                    "result": "Verify denominators and record assumptions.",
                },
            ),
        ),
        session_id="session-1",
    )

    specialist = projection.subagents[0]
    assert specialist.role_label == "Research Planner"
    assert specialist.status_label == "Complete"
    assert specialist.task_summary == "Review the synthetic analysis plan"
    assert specialist.result_summary == "Verify denominators and record assumptions."
    details = projection.actions[1].details
    assert details.kind == "task"
    assert details.role_label == "Research Planner"
    assert details.capability == "advisory"


def test_projection_derives_rejected_specialist_state_from_the_action_decision() -> None:
    projection = project_session(
        (
            _event(
                0,
                EventKind.TOOL_CALL_PROPOSED,
                {
                    "tool_call_id": "task-call-1",
                    "action_id": "action-1",
                    "tool_name": "task",
                    "kind": "task",
                    "risk": "medium",
                    "specialist_label": "Research Planner",
                    "specialist_capability": "advisory",
                    "arguments": {
                        "description": "Review the plan",
                        "prompt": "Review the supplied synthetic analysis plan.",
                        "subagent_type": "research-planner",
                    },
                },
            ),
            _event(
                1,
                EventKind.SUBAGENT_UPDATED,
                {
                    "subagent": {
                        "invocation_id": "task-call-1",
                        "task_id": None,
                        "agent_name": "research-planner",
                        "role_label": "Research Planner",
                        "status": "proposed",
                        "parent_session_id": "session-1",
                        "parent_action_id": "action-1",
                    }
                },
            ),
            _confirmation_event(2, "group-1", "task-call-1", "task"),
            _event(
                3,
                EventKind.CONFIRMATION_RESOLVED,
                {
                    "group_id": "group-1",
                    "tool_call_id": "task-call-1",
                    "decision": "denied",
                },
            ),
        ),
        session_id="session-1",
    )

    specialist = projection.subagents[0]
    assert specialist.status == "rejected"
    assert specialist.status_label == "Not Run"
    assert specialist.task_summary == "Review the plan"
    assert specialist.result_summary is None


def test_projection_uses_parent_action_as_specialist_presentation_fallback() -> None:
    projection = project_session(
        (
            _event(
                0,
                EventKind.TOOL_CALL_PROPOSED,
                {
                    "tool_call_id": "task-call-parent",
                    "action_id": "action-parent",
                    "tool_name": "task",
                    "kind": "task",
                    "risk": "low",
                    "arguments": {
                        "description": "Review the synthetic analysis plan",
                        "subagent_type": "research-planner",
                    },
                },
            ),
            _event(
                1,
                EventKind.SUBAGENT_UPDATED,
                {
                    "subagent": {
                        "invocation_id": "sdk-generated-invocation",
                        "task_id": "task-1",
                        "agent_name": "research-planner",
                        "status": "completed",
                        "parent_session_id": "session-1",
                        "parent_action_id": "action-parent",
                    }
                },
            ),
            _event(
                2,
                EventKind.TOOL_EXECUTION_RECORDED,
                {
                    "tool_call_id": "task-call-parent",
                    "action_id": "action-parent",
                    "tool_name": "task",
                    "exit_code": 0,
                    "summary": "Plan review completed",
                },
            ),
        ),
        session_id="session-1",
    )

    specialist = projection.subagents[0]
    assert specialist.task_summary == "Review the synthetic analysis plan"
    assert specialist.result_summary == "Plan review completed"


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
            _event(
                4,
                EventKind.AGENT_MESSAGE_EMITTED,
                {"content": "The synthetic result is ready for review."},
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
    assert [item.suggestion_id for item in projection.suggestions] == ["review-changes"]


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
                    "code": BackendErrorCode.ACTION_OUTCOME_UNKNOWN.value,
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
                    "code": BackendErrorCode.ACTION_OUTCOME_UNKNOWN.value,
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


def test_specialist_review_labels_every_model_written_field() -> None:
    arguments: dict[str, JsonValue] = {
        "description": "Check the plan",
        "prompt": "Review the synthetic plan.\nReport gaps.",
        "subagent_type": "research-planner",
    }
    proposed: dict[str, JsonValue] = {
        "tool_call_id": "task-1",
        "tool_name": "task",
        "kind": "task",
        "summary": "Delegate the review",
        "arguments": arguments,
    }
    uncatalogued = project_session(
        (_event(0, EventKind.TOOL_CALL_PROPOSED, proposed),), session_id="session-1"
    ).actions[0]
    catalogued = project_session(
        (
            _event(
                0,
                EventKind.TOOL_CALL_PROPOSED,
                {
                    **proposed,
                    "specialist_label": "Research Planner",
                    "specialist_capability": "advisory",
                },
            ),
        ),
        session_id="session-1",
    ).actions[0]

    assert specialist_review_lines(catalogued) == (
        "Specialist: Research Planner",
        "Capability: Advisory",
        "Model-provided task description: Check the plan",
        "Model-provided task prompt: Review the synthetic plan.\nReport gaps.",
    )
    assert specialist_review_lines(uncatalogued)[0] == (
        "Model-provided specialist name: research-planner"
    )
    # Only the catalog role heads an action unlabelled; every fallback names the model.
    assert action_heading(catalogued) == "Research Planner"
    assert action_heading(uncatalogued) == "Model-provided task description: Check the plan"
    without_description = uncatalogued.model_copy(
        update={"details": uncatalogued.details.model_copy(update={"description": None})}
    )
    assert action_heading(without_description) == (
        "Model-provided specialist name: research-planner"
    )
    bare = uncatalogued.model_copy(update={"details": ProjectionTaskActionDetails()})
    assert action_heading(bare) == "Model-provided summary: Delegate the review"


def _command(sequence: int, kind: str, *, actor_id: str = "researcher") -> SessionEvent:
    return _event(
        sequence,
        EventKind.COMMAND_RECEIVED,
        {"command_id": f"command-{sequence}", "command_kind": kind, "actor_id": actor_id},
    )


def test_only_a_researcher_command_ends_a_projected_pause() -> None:
    paused = (
        _event(0, EventKind.AGENT_LIFECYCLE_UPDATED, {"status": "running"}),
        _event(
            1, EventKind.TOOL_CALL_PROPOSED, {"tool_call_id": "call-1", "tool_name": "terminal"}
        ),
        _command(2, "pause"),
        _event(3, EventKind.SESSION_PAUSED, {"command_id": "command-2"}),
    )
    backend_reports = (
        _event(4, EventKind.AGENT_LIFECYCLE_UPDATED, {"status": "running"}),
        _event(5, EventKind.AGENT_LIFECYCLE_UPDATED, {"status": "finished"}),
        _event(
            6,
            EventKind.ERROR_RECORDED,
            {"reason": "synthetic", "code": BackendErrorCode.ACTION_FAILED.value},
        ),
        _command(7, "approve", actor_id=AUTOMATIC_REVIEW_ACTOR),
        _event(8, EventKind.AGENT_LIFECYCLE_UPDATED, {"status": "running"}),
    )
    events: tuple[SessionEvent, ...] = paused
    for event in backend_reports:
        events = (*events, event)
        projection = project_session(events, session_id="session-1")
        # The service still treats the session as paused, so every interface must too.
        assert session_activity(events).paused
        assert projection.lifecycle.status == SessionLifecycle.PAUSED
        assert (projection.lifecycle.can_pause, projection.lifecycle.can_resume) == (False, True)
        assert projection.available_commands == ("chat", "resume")
        assert projection.researcher_status.code == "paused"
        assert projection.actions[0].state == "proposed"

    resumed = project_session(
        (*events, _command(9, "resume"), _event(10, EventKind.SESSION_RESUMED, {})),
        session_id="session-1",
    )
    assert resumed.lifecycle.status == SessionLifecycle.RUNNING
    assert resumed.available_commands == ("chat", "pause")
    continued = project_session(
        (
            *events,
            _command(9, "chat"),
            _event(10, EventKind.AGENT_LIFECYCLE_UPDATED, {"status": "running"}),
        ),
        session_id="session-1",
    )
    assert continued.lifecycle.status == SessionLifecycle.RUNNING
    assert continued.lifecycle.can_pause


def test_a_review_requested_during_a_pause_is_decided_first_and_a_rejection_keeps_the_pause() -> (
    None
):
    requested = (
        _command(0, "pause"),
        _event(1, EventKind.SESSION_PAUSED, {}),
        _event(
            2, EventKind.TOOL_CALL_PROPOSED, {"tool_call_id": "call-1", "tool_name": "terminal"}
        ),
        _confirmation_event(3, "group-1", "call-1", "terminal"),
    )
    projection = project_session(requested, session_id="session-1")

    # The gateway accepts only the decision, so nothing may offer resume or guidance.
    assert session_activity(requested).paused
    assert projection.lifecycle.status == SessionLifecycle.WAITING_FOR_CONFIRMATION
    assert projection.lifecycle.model_dump() == {
        "status": SessionLifecycle.WAITING_FOR_CONFIRMATION,
        "can_pause": False,
        "can_resume": False,
        "can_steer": False,
    }
    assert projection.researcher_status.code == "waiting-for-review"
    assert projection.pending_approval is not None
    assert projection.available_commands == ("approve", "deny")

    rejected = (
        *requested,
        _command(4, "deny"),
        _event(
            5,
            EventKind.APPROVAL_RECORDED,
            {"group_id": "group-1", "decision": "denied", "tool_call_ids": ["call-1"]},
        ),
        _event(
            6,
            EventKind.CONFIRMATION_RESOLVED,
            {"group_id": "group-1", "tool_call_id": "call-1", "decision": "denied"},
        ),
    )
    still_paused = project_session(rejected, session_id="session-1")
    # A rejection does not continue the agent, so the pause is still in effect.
    assert session_activity(rejected).paused
    assert still_paused.paused
    assert still_paused.available_commands == ("chat", "resume")

    approved = project_session((*requested, _command(4, "approve")), session_id="session-1")
    assert not session_activity((*requested, _command(4, "approve"))).paused
    assert approved.available_commands == ("approve", "deny")


@pytest.mark.parametrize("kind", ["chat", "workflow", "resume"])
def test_a_researcher_command_ends_the_projected_pause_when_it_ends_the_service_pause(
    kind: str,
) -> None:
    events = (
        _event(0, EventKind.AGENT_LIFECYCLE_UPDATED, {"status": "running"}),
        _command(1, "pause"),
        _event(2, EventKind.SESSION_PAUSED, {}),
        _command(3, kind),
    )

    projection = project_session(events, session_id="session-1")

    assert not session_activity(events).paused
    assert not projection.paused
    assert projection.lifecycle.status == SessionLifecycle.RUNNING
    assert projection.available_commands == ("chat", "pause")


@pytest.mark.parametrize("cause", ["fatal-error", "integrity"])
def test_an_unrecoverable_session_advertises_no_decision_on_an_open_review(cause: str) -> None:
    unrecoverable = (
        _event(
            2,
            EventKind.ERROR_RECORDED,
            {
                "reason": "synthetic",
                "code": BackendErrorCode.ACTION_OUTCOME_UNKNOWN.value,
                "affects_lifecycle": False,
            },
        )
        if cause == "fatal-error"
        else _event(
            2,
            EventKind.TOOL_CALL_PROPOSED,
            {"tool_call_id": "call-1", "tool_name": "terminal"},
        )
    )
    projection = project_session(
        (
            _event(
                0, EventKind.TOOL_CALL_PROPOSED, {"tool_call_id": "call-1", "tool_name": "terminal"}
            ),
            _confirmation_event(1, "group-1", "call-1", "terminal"),
            unrecoverable,
        ),
        session_id="session-1",
    )

    # The gateway refuses every decision once the session cannot continue.
    assert projection.pending_approval is not None
    assert projection.lifecycle.status == SessionLifecycle.ERROR
    assert not projection.lifecycle.can_steer
    assert projection.researcher_status.code == "terminal-failure"
    assert projection.available_commands == ()


def test_a_fatal_error_ends_steering_whatever_its_lifecycle_flag() -> None:
    events = (
        _command(0, "pause"),
        _event(1, EventKind.SESSION_PAUSED, {}),
        _event(
            2,
            EventKind.ERROR_RECORDED,
            {
                "reason": "synthetic",
                "code": BackendErrorCode.ACTION_OUTCOME_UNKNOWN.value,
                "affects_lifecycle": False,
            },
        ),
    )
    projection = project_session(events, session_id="session-1")

    assert session_activity(events).fatal
    assert projection.lifecycle.status == SessionLifecycle.ERROR
    assert not projection.lifecycle.can_steer
    assert projection.available_commands == ()


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


@pytest.mark.parametrize("mode", ["required", "buffered"])
def test_projection_discloses_evidence_gap_and_retention(mode: str) -> None:
    pending = _event(0, EventKind.AUDIT_EVIDENCE_RECORDED, {"mode": mode, "status": "pending"})
    retained = _event(1, EventKind.AUDIT_EVIDENCE_RECORDED, {"mode": mode, "status": "retained"})
    projection = project_session((pending, retained), session_id="session-main")
    assert "pending" in projection.activity[0].detail.lower()
    assert ("blocked" in projection.activity[0].detail.lower()) == (mode == "required")
    assert "retained" in projection.activity[1].detail.lower()


def _attempt_event(sequence: int, attempt_id: str, phase: str) -> SessionEvent:
    return _event(
        sequence,
        EventKind.WORKER_ATTEMPT_RECORDED,
        {"phase": phase, "intent": {"attempt_id": attempt_id}},
    )


def test_replacement_keeps_earlier_worker_usage_and_supersedes_its_unfinished_plan() -> None:
    plan: dict[str, JsonValue] = {
        "tasks": [
            {"title": "Inspect the cohort", "status": "done"},
            {"title": "Fit the model", "status": "in_progress"},
            {"title": "Write the summary", "status": "todo"},
        ]
    }
    earlier = (
        _attempt_event(0, "attempt-1", "active"),
        _event(1, EventKind.TASK_PLAN_UPDATED, plan),
        _usage_event(2, "total", calls=3, prompt_tokens=150, completion_tokens=30),
        _usage_event(3, "agent", calls=3, prompt_tokens=150, completion_tokens=30),
        _attempt_event(4, "attempt-1", "fenced"),
        _attempt_event(5, "attempt-1", "revoked"),
    )
    revoked = project_session(earlier, session_id="session-1")
    assert revoked.researcher_notice is None
    assert [task.status for task in revoked.task_plan] == ["done", "in-progress", "todo"]

    replaced = (*earlier, _attempt_event(6, "attempt-2", "active"))
    projection = project_session(replaced, session_id="session-1")
    assert [(task.status, task.status_label) for task in projection.task_plan] == [
        ("done", "Complete"),
        ("superseded", "Superseded"),
        ("superseded", "Superseded"),
    ]
    assert projection.usage is not None
    assert projection.usage.call_count == 3
    by_purpose = {usage.usage_id: usage for usage in projection.usage_by_purpose}
    assert by_purpose["replaced-workers"].purpose_label == "Earlier Workers"
    assert by_purpose["replaced-workers"].prompt_tokens == 150

    # The successor reports from zero; the session total keeps what came before.
    continued = project_session(
        (
            *replaced,
            _usage_event(7, "total", calls=1, prompt_tokens=40, completion_tokens=8),
            _usage_event(8, "agent", calls=1, prompt_tokens=40, completion_tokens=8),
            _event(
                9, EventKind.TASK_PLAN_UPDATED, {"tasks": [{"title": "Refit", "status": "todo"}]}
            ),
        ),
        session_id="session-1",
    )
    assert continued.usage is not None
    assert (continued.usage.call_count, continued.usage.prompt_tokens) == (4, 190)
    assert continued.usage.completion_tokens == 38
    by_purpose = {usage.usage_id: usage for usage in continued.usage_by_purpose}
    assert by_purpose["agent"].prompt_tokens == 190
    assert by_purpose["replaced-workers"].prompt_tokens == 150
    assert [task.status for task in continued.task_plan] == ["todo"]
    assert continued.execution_usage(elapsed_seconds=1).model_calls == 4


def test_revoked_worker_with_an_unsettled_run_reports_the_replacement_blocker() -> None:
    events = (
        _attempt_event(0, "attempt-1", "active"),
        _event(1, EventKind.AGENT_LIFECYCLE_UPDATED, {"status": "running"}),
        _attempt_event(2, "attempt-1", "fenced"),
    )
    assert project_session(events, session_id="session-1").researcher_notice is None
    projection = project_session(
        (*events, _attempt_event(3, "attempt-1", "revoked")), session_id="session-1"
    )
    notice = projection.researcher_notice
    assert notice is not None
    assert (notice.code, notice.tone) == ("worker-replacement-unavailable", "attention")
    assert "run did not settle" in notice.detail
