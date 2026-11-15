# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from heartwood.core_adapter.external_actions import (
    ExternalActionDecision,
    ExternalActionObservation,
    ExternalActionProposal,
    ExternalActionRecord,
    ExternalActionRequest,
    ExternalInputReference,
)
from heartwood.core_adapter.external_results import ExternalResultProposal, ExternalResultRecord
from heartwood.core_adapter.worker_attempts import WorkerAttemptIntent
from heartwood.gateway._action_presentation import (
    action_summary_label,
    outside_review_lines,
    outside_review_title,
)
from heartwood.gateway._session_projection import (
    ProjectionOutsideOperationActionDetails,
    ProjectionOutsideResultActionDetails,
    project_session,
)
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


def test_outside_operation_review_states_what_leaves_and_where(
    proposal: ExternalActionProposal,
) -> None:
    request_json = '{"cohort":"aggregate","threshold":5}'
    proposal = proposal.model_copy(
        update={
            "request": proposal.request.model_copy(
                update={
                    "request_json": request_json,
                    "inputs": (
                        ExternalInputReference(
                            reference="synthetic-input", sha256="f" * 64, size_bytes=42
                        ),
                    ),
                }
            )
        }
    )
    projection = project_session(
        (
            _source(proposal),
            _event(ExternalActionRecord(phase="proposed", proposal=proposal), 1),
        ),
        session_id="synthetic-session",
    )
    native, outside = projection.actions
    assert native.details.kind == "other"
    assert native.summary_source == "model"
    assert outside.summary == "Submit synthetic analysis"
    assert outside.summary_source == "gateway"
    details = outside.details
    assert isinstance(details, ProjectionOutsideOperationActionDetails)
    assert details.service_id == "synthetic-service"
    assert details.destination == "synthetic-results"
    assert details.expires_at == proposal.expires_at.isoformat()
    assert json.loads(details.request.preview) == json.loads(request_json)
    assert not details.request.truncated
    assert details.request.size_bytes == len(request_json)
    assert details.request.sha256 == hashlib.sha256(request_json.encode()).hexdigest()
    assert [(item.reference, item.sha256, item.size_bytes) for item in details.inputs] == [
        ("synthetic-input", "f" * 64, 42)
    ]
    assert projection.pending_approval is not None
    assert projection.pending_approval.actions == (outside,)
    assert outside_review_title(native) is None
    assert outside_review_lines(native) == ()
    assert action_summary_label(native) == "Model-provided summary"
    assert outside_review_title(outside) == "Outside Operation"
    assert action_summary_label(outside) == "Gateway-provided summary"
    assert outside_review_lines(outside) == (
        "Leaves the sandbox",
        "Service: synthetic-service",
        "Destination: synthetic-results",
        "Operation: synthetic-analysis",
        f"Input: synthetic-input · 42 bytes · SHA-256 {'f' * 64}",
        f"Request sent to the service: {len(request_json)} bytes of canonical JSON"
        f" · SHA-256 {details.request.sha256}",
        f"Review valid until: {details.expires_at}",
    )


def _release(proposal: ExternalActionProposal, content_json: str) -> ExternalResultProposal:
    return ExternalResultProposal(
        release_id=uuid4(),
        operation_id=proposal.operation_id,
        operation_sha256=proposal.sha256,
        observation_event_id="event-3",
        worker=proposal.worker,
        service_policy_sha256=proposal.service_policy_sha256,
        content_json=content_json,
        model_route_json='{"endpoint":"https://model.invalid","model_id":"synthetic-model"}',
        created_at=_NOW,
        expires_at=_NOW + timedelta(minutes=5),
    )


def test_long_result_release_preview_is_marked_truncated_with_size_and_digest(
    proposal: ExternalActionProposal,
) -> None:
    decision = ExternalActionDecision(
        command_id="outside-approval",
        actor_id="researcher",
        decision="approved",
        decided_at=_NOW,
        proposal_sha256=proposal.sha256,
    )
    observed = ExternalActionRecord(
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
    content = {"rows": [f"synthetic aggregate row {index}" for index in range(400)]}
    content_json = json.dumps(content, sort_keys=True, separators=(",", ":"))
    release = _release(proposal, content_json)
    projection = project_session(
        (
            _source(proposal),
            _event(ExternalActionRecord(phase="proposed", proposal=proposal), 1),
            _event(
                ExternalActionRecord(phase="submitting", proposal=proposal, decision=decision), 2
            ),
            _event(observed, 3),
            SessionEvent(
                event_id="event-4",
                session_id="synthetic-session",
                sequence=4,
                kind=EventKind.EXTERNAL_RESULT_RECORDED,
                occurred_at=_NOW.isoformat(),
                payload={
                    "record": ExternalResultRecord(phase="proposed", proposal=release).model_dump(
                        mode="json"
                    )
                },
            ),
        ),
        session_id="synthetic-session",
    )
    assert projection.pending_approval is not None
    (action,) = projection.pending_approval.actions
    assert action.summary_source == "gateway"
    details = action.details
    assert isinstance(details, ProjectionOutsideResultActionDetails)
    assert details.model_route == {
        "endpoint": "https://model.invalid",
        "model_id": "synthetic-model",
    }
    assert details.expires_at == release.expires_at.isoformat()
    complete = json.dumps(content, indent=2, sort_keys=True)
    assert details.content.truncated
    assert len(details.content.preview) == 2_000
    assert complete.startswith(details.content.preview)
    assert details.content.size_bytes == len(content_json.encode())
    assert details.content.sha256 == hashlib.sha256(content_json.encode()).hexdigest()
    assert action.arguments["content"] == content
    assert outside_review_title(action) == "Outside Result Release"
    assert outside_review_lines(action) == (
        "Leaves Heartwood for the model route",
        'Model route: {"endpoint": "https://model.invalid", "model_id": "synthetic-model"}',
        f"Content sent to the worker and its model: {details.content.size_bytes:,} bytes"
        f" of canonical JSON · SHA-256 {details.content.sha256}",
        f"Review valid until: {details.expires_at}",
    )


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


@pytest.mark.parametrize("phase", ["expired", "expired-authorization", "withdrawn"])
def test_outside_review_closed_before_dispatch_is_not_pending_or_uncertain(
    proposal: ExternalActionProposal, phase: str
) -> None:
    decisions = [
        ExternalActionDecision(
            command_id=f"synthetic-{decision}",
            actor_id="researcher",
            decision=decision,
            decided_at=_NOW,
            proposal_sha256=proposal.sha256,
        )
        for decision in ("approved", "denied")
    ]
    closed = ExternalActionRecord(
        phase="withdrawn" if phase == "withdrawn" else "expired",
        proposal=proposal,
        decision=None if phase == "expired" else decisions[0],
        withdrawal=decisions[1] if phase == "withdrawn" else None,
    )
    events = [
        _source(proposal),
        _event(ExternalActionRecord(phase="proposed", proposal=proposal), 1),
    ]
    if phase != "expired":
        events.append(
            _event(
                ExternalActionRecord(phase="authorized", proposal=proposal, decision=decisions[0]),
                2,
            )
        )
    final = _event(closed, len(events)).model_copy(
        update={"occurred_at": proposal.expires_at.isoformat()}
    )
    projection = project_session((*events, final), session_id="synthetic-session")
    assert projection.pending_approval is None
    action = projection.actions[-1]
    assert action.state == "failed"
    assert action.outcome is not None
    assert "before dispatch" in action.outcome.summary


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
    release = _release(proposal, '{"aggregate":7}')
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
