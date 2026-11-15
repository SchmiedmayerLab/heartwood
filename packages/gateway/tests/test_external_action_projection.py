# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from heartwood.core_adapter.external_actions import (
    ExternalActionDecision,
    ExternalActionObservation,
    ExternalActionProposal,
    ExternalActionRecord,
    ExternalActionRequest,
)
from heartwood.core_adapter.external_results import ExternalResultProposal, ExternalResultRecord
from heartwood.core_adapter.worker_attempts import WorkerAttemptIntent
from heartwood.gateway._session_projection import project_session
from heartwood.schemas import PlatformOperationObservation
from heartwood.session import EventKind, SessionEvent

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _event(record: ExternalActionRecord, sequence: int) -> SessionEvent:
    return SessionEvent(
        event_id=f"event-{sequence}",
        session_id="synthetic-session",
        sequence=sequence,
        kind=EventKind.EXTERNAL_ACTION_RECORDED,
        occurred_at=_NOW.isoformat(),
        payload={"record": record.model_dump(mode="json")},
    )


@pytest.fixture
def proposal() -> ExternalActionProposal:
    return ExternalActionProposal(
        operation_id=uuid4(),
        source_event_id="event-0",
        service_policy_sha256="a" * 64,
        worker=WorkerAttemptIntent(
            project_id=uuid4(),
            session_id="synthetic-session",
            attempt_id=uuid4(),
            conversation_id=uuid4(),
            image_digest="sha256:" + "b" * 64,
            gateway_policy_sha256="c" * 64,
            sandbox_policy_sha256="d" * 64,
            input_manifest_sha256="e" * 64,
        ),
        request=ExternalActionRequest(
            service_id="synthetic-service",
            tool_name="synthetic-analysis",
            destination="synthetic-results",
            summary="Submit synthetic analysis",
            request_json="{}",
        ),
        created_at=_NOW,
        expires_at=_NOW + timedelta(minutes=5),
    )


def _source(proposal: ExternalActionProposal) -> SessionEvent:
    return SessionEvent(
        event_id="event-0",
        session_id="synthetic-session",
        sequence=0,
        kind=EventKind.TOOL_CALL_PROPOSED,
        occurred_at=_NOW.isoformat(),
        payload={
            "tool_call_id": "worker-native-id",
            "tool_name": "synthetic-analysis",
            "worker_attempt_id": str(proposal.worker.attempt_id),
            "worker_project_id": str(proposal.worker.project_id),
        },
    )


def test_outside_review_uses_shared_action_group(proposal: ExternalActionProposal) -> None:
    events = (
        _source(proposal),
        _event(ExternalActionRecord(phase="proposed", proposal=proposal), 1),
    )
    projection = project_session(events, session_id="synthetic-session")
    assert projection.pending_approval is not None
    assert projection.pending_approval.group_id == proposal.group_id
    action = projection.actions[-1]
    assert action.action_id == proposal.action_id
    assert action.state == "awaiting-review"
    assert action.arguments == proposal.review_call.arguments
    assert projection.actions[0].tool_call_id == "worker-native-id"
    assert projection.activity[-1].label == "Outside operation"


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("submitted", "running"),
        ("queued", "running"),
        ("running", "running"),
        ("completed", "succeeded"),
        ("failed", "failed"),
        ("cancelled", "failed"),
        ("denied", "failed"),
        ("unknown", "outcome-unknown"),
    ],
)
def test_only_platform_observation_settles_outside_action(
    proposal: ExternalActionProposal,
    status: str,
    expected: str,
) -> None:
    decision = ExternalActionDecision(
        command_id="synthetic-approval",
        actor_id="researcher",
        decision="approved",
        decided_at=_NOW,
        proposal_sha256=proposal.sha256,
    )
    events = (
        _source(proposal),
        _event(ExternalActionRecord(phase="proposed", proposal=proposal), 1),
        _event(ExternalActionRecord(phase="submitting", proposal=proposal, decision=decision), 2),
    )
    uncertain = project_session(events, session_id="synthetic-session")
    assert uncertain.pending_approval is None
    assert uncertain.actions[-1].state == "outcome-unknown"
    assert uncertain.actions[-1].outcome is None
    operation = PlatformOperationObservation.model_validate(
        {
            "project_context_id": str(proposal.worker.project_id),
            "operation_id": str(proposal.operation_id),
            "service_id": proposal.request.service_id,
            "backend_operation_ref": "synthetic-operation",
            "observed_at": _NOW,
            "status": status,
            "platform_record_ref": "synthetic-audit-record",
        }
    )
    observation = ExternalActionRecord(
        phase="observed",
        proposal=proposal,
        decision=decision,
        observation=ExternalActionObservation(proposal_sha256=proposal.sha256, operation=operation),
    )
    projection = project_session((*events, _event(observation, 3)), session_id="synthetic-session")
    assert projection.actions[-1].state == expected
    assert projection.actions[0].state == "proposed"
    assert (projection.actions[-1].outcome is not None) == observation.terminal


def test_denied_outside_operation_has_no_execution_result(proposal: ExternalActionProposal) -> None:
    denied = ExternalActionRecord(
        phase="denied",
        proposal=proposal,
        decision=ExternalActionDecision(
            command_id="synthetic-denial",
            actor_id="researcher",
            decision="denied",
            decided_at=_NOW,
            proposal_sha256=proposal.sha256,
        ),
    )
    projection = project_session(
        (
            _source(proposal),
            _event(ExternalActionRecord(phase="proposed", proposal=proposal), 1),
            _event(denied, 2),
        ),
        session_id="synthetic-session",
    )
    assert projection.pending_approval is None
    assert projection.actions[-1].state == "rejected"
    assert projection.actions[-1].outcome is None


@pytest.mark.parametrize(
    ("phase", "expected"),
    [
        ("proposed", "awaiting-review"),
        ("submitting", "outcome-unknown"),
        ("unknown", "outcome-unknown"),
        ("submitted", "succeeded"),
        ("denied", "rejected"),
    ],
)
def test_result_release_has_independent_review_and_delivery_outcome(
    proposal: ExternalActionProposal, phase: str, expected: str
) -> None:
    decision = ExternalActionDecision(
        command_id="outside-approval",
        actor_id="researcher",
        decision="approved",
        decided_at=_NOW,
        proposal_sha256=proposal.sha256,
    )
    outside = ExternalActionRecord(
        phase="observed",
        proposal=proposal,
        decision=decision,
        observation=ExternalActionObservation(
            proposal_sha256=proposal.sha256,
            operation=PlatformOperationObservation(
                project_context_id=str(proposal.worker.project_id),
                operation_id=str(proposal.operation_id),
                service_id=proposal.request.service_id,
                backend_operation_ref="native-operation",
                observed_at=_NOW,
                status="completed",
                platform_record_ref="native-audit",
            ),
        ),
    )
    events = [
        _source(proposal),
        _event(ExternalActionRecord(phase="proposed", proposal=proposal), 1),
        _event(ExternalActionRecord(phase="submitting", proposal=proposal, decision=decision), 2),
        _event(outside, 3),
    ]
    release = ExternalResultProposal(
        release_id=uuid4(),
        operation_id=proposal.operation_id,
        operation_sha256=proposal.sha256,
        observation_event_id="event-3",
        worker=proposal.worker,
        service_policy_sha256=proposal.service_policy_sha256,
        content_json='{"aggregate":7}',
        model_route_json='{"endpoint":"https://model.invalid"}',
        created_at=_NOW,
        expires_at=_NOW + timedelta(minutes=5),
    )
    release_decision = ExternalActionDecision(
        command_id="result-approval",
        actor_id="researcher",
        decision="denied" if phase == "denied" else "approved",
        decided_at=_NOW,
        proposal_sha256=release.sha256,
    )
    phases = ["proposed"]
    if phase != "proposed":
        phases.append("denied" if phase == "denied" else "submitting")
    if phase in {"submitted", "unknown"}:
        phases.append(phase)
    for current in phases:
        record = ExternalResultRecord.model_validate(
            {
                "phase": current,
                "proposal": release,
                "decision": None if current == "proposed" else release_decision,
            }
        )
        events.append(
            SessionEvent(
                event_id=f"event-{len(events)}",
                session_id="synthetic-session",
                sequence=len(events),
                kind=EventKind.EXTERNAL_RESULT_RECORDED,
                occurred_at=_NOW.isoformat(),
                payload={"record": record.model_dump(mode="json")},
            )
        )
    projected = project_session(tuple(events), session_id="synthetic-session")
    action = projected.actions[-1]
    assert action.action_id == release.action_id
    assert action.arguments == release.review_call.arguments
    assert action.state == expected
    assert projected.activity[-1].label == "Outside result release"
    if phase == "proposed":
        assert projected.pending_approval is not None
        assert projected.pending_approval.group_id == release.group_id
    else:
        assert projected.pending_approval is None
    if phase == "submitted":
        assert action.outcome is not None
        assert action.outcome.result is not None
        assert "execution is not established" in action.outcome.result
