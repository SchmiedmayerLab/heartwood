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


def test_later_stable_lifecycle_recovers_from_an_outcome_error() -> None:
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

    assert projection.lifecycle.status == SessionLifecycle.FINISHED
    assert projection.lifecycle.can_steer is True
    assert projection.available_commands == ("chat",)


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
