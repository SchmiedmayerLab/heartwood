# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Review, authorization, and dispatch of outside operations and result releases.

The session service calls every function here while it holds its command lock.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Literal, cast
from uuid import UUID, uuid4

from heartwood.core_adapter._facade import BackendErrorCode, BackendErrorEvent, BackendEvent
from heartwood.core_adapter._state import SessionRecoveryError
from heartwood.core_adapter.external_actions import (
    ExternalActionAdapter,
    ExternalActionDecision,
    ExternalActionError,
    ExternalActionProposal,
    ExternalActionRecord,
    ExternalActionRequest,
    canonical_request,
    external_action_records,
    validate_external_transition,
)
from heartwood.core_adapter.external_results import (
    ExternalResultProposal,
    ExternalResultRecord,
    external_result_records,
    validate_result_transition,
)
from heartwood.core_adapter.worker_attempts import gateway_policy_sha256
from heartwood.schemas import JsonValue
from heartwood.session import CommandKind, EventKind, SessionCommand, SessionEvent

if TYPE_CHECKING:
    from heartwood.core_adapter._service import SessionService


def propose_external_action_locked(
    service: SessionService, source_event_id: str
) -> tuple[SessionEvent, ...]:
    """Record review of a retained worker proposal, after any expiry it replaces."""
    service._require_worker_owner_locked()
    lease = service._worker_lease
    adapter = service._external_actions
    if lease is None or adapter is None or service._worker_admission_error_locked() is not None:
        raise ExternalActionError("outside operation requires an admitted worker and service")
    events = service.replay_events()
    existing = next(
        (
            item
            for item in external_action_records(events).values()
            if item.proposal.source_event_id == source_event_id and item.phase != "expired"
        ),
        None,
    )
    closed = _expire_external_review(service, existing)
    if existing is not None and closed is None:
        raise ExternalActionError("worker proposal already has an outside operation")
    source = next((event for event in events if event.event_id == source_event_id), None)
    if (
        source is None
        or source.kind != EventKind.TOOL_CALL_PROPOSED
        or source.payload.get("worker_attempt_id") != str(lease.intent.attempt_id)
    ):
        raise ExternalActionError("outside operation requires a current worker proposal")
    tool_name, arguments = source.payload.get("tool_name"), source.payload.get("arguments")
    if (
        not isinstance(tool_name, str)
        or tool_name not in adapter.tool_names
        or not isinstance(arguments, dict)
    ):
        raise ExternalActionError("worker tool has no trusted outside service binding")
    policy = adapter.policy_sha256
    request = ExternalActionRequest.model_validate(adapter.prepare(tool_name, arguments))
    if request.tool_name != tool_name:
        raise ExternalActionError("service request changed the proposed tool")
    # A lost reply or timeout may have caused an effect; reconcile it before another call.
    if any(
        item.proposal.request.service_id == request.service_id
        and item.phase in {"submitting", "unknown"}
        for item in external_action_records(events).values()
    ):
        raise ExternalActionError(
            "an earlier operation on this service has an unknown outcome; reconcile it first"
        )
    created_at = external_now(service.clock)
    proposal = ExternalActionProposal(
        operation_id=uuid4(),
        worker=lease.intent,
        source_event_id=source_event_id,
        service_policy_sha256=policy,
        request=request,
        created_at=created_at,
        expires_at=created_at + timedelta(minutes=5),
    )
    _check_external_authority(service, proposal)
    event = _record_external(service, ExternalActionRecord(phase="proposed", proposal=proposal))
    return (event,) if closed is None else (closed, event)


def propose_external_result_locked(
    service: SessionService, operation_id: UUID
) -> tuple[SessionEvent, ...]:
    """Record review of an observed outside result, after any expiry it replaces."""
    service._require_worker_owner_locked()
    adapter, lease = service._external_actions, service._worker_lease
    events = service.replay_events()
    outside = external_action_records(events).get(operation_id)
    closed: SessionEvent | None = None
    existing = next(
        (
            item
            for item in external_result_records(events).values()
            if item.proposal.operation_id == operation_id and item.phase != "expired"
        ),
        None,
    )
    if (
        adapter is None
        or lease is None
        or outside is None
        or not outside.terminal
        or outside.observation is None
        or outside.proposal.worker != lease.intent
        or (existing is not None and (closed := _expire_external_review(service, existing)) is None)
    ):
        raise ExternalActionError("result release requires a unique current worker outcome")
    source = next(
        event
        for event in reversed(events)
        if event.kind == EventKind.EXTERNAL_ACTION_RECORDED
        and ExternalActionRecord.model_validate_json(json.dumps(event.payload["record"])) == outside
    )
    policy = adapter.policy_sha256
    route = _result_model_route(service)
    created_at = external_now(service.clock)
    proposal = ExternalResultProposal(
        release_id=uuid4(),
        operation_id=operation_id,
        operation_sha256=outside.proposal.sha256,
        observation_event_id=source.event_id,
        worker=lease.intent,
        service_policy_sha256=policy,
        model_route_json=route,
        content_json="{}",
        created_at=created_at,
        expires_at=created_at + timedelta(minutes=5),
    )
    _check_result_authority(service, proposal)
    try:
        content = canonical_request(adapter.prepare_result(outside))
        proposal = ExternalResultProposal.model_validate(
            proposal.model_copy(update={"content_json": content})
        )
    except Exception:
        raise ExternalActionError("outside result is unavailable or invalid") from None
    _check_result_authority(service, proposal)
    event = _record_external(service, ExternalResultRecord(phase="proposed", proposal=proposal))
    return (event,) if closed is None else (closed, event)


def decide_external_action(
    service: SessionService, command: SessionCommand, current: ExternalActionRecord
) -> tuple[SessionEvent, ...]:
    """Record the decision on an outside operation and submit an approved one once."""
    proposal, adapter = current.proposal, service._external_actions
    approved = command.kind == CommandKind.APPROVE.value
    if current.phase == "authorized" and not approved:
        return _withdraw_external_authorization(service, command, current)
    _check_retained_authorization(command, current)
    if current.phase not in {"proposed", "authorized"}:
        return (
            service._record_event(
                EventKind.ERROR_RECORDED,
                {
                    "reason": "outside operation already has a decision",
                    "affects_lifecycle": False,
                },
            ),
        )
    inputs: list[bytes] = []
    try:
        if approved:
            _check_external_authority(service, proposal)
            assert adapter is not None
            for reference in proposal.request.inputs:
                content = adapter.read_input(reference)
                if (
                    type(content) is not bytes
                    or len(content) != reference.size_bytes
                    or hashlib.sha256(content).hexdigest() != reference.sha256
                ):
                    raise ExternalActionError("reviewed outside input changed")
                inputs.append(content)
            adapter.check_policy(proposal, command.actor_id, external_now(service.clock))
            _check_external_authority(service, proposal)
        decision = _external_decision(service, command, proposal, approved=approved)
        intent = ExternalActionRecord(
            phase="authorized" if approved else "denied",
            proposal=proposal,
            decision=current.decision or decision,
        )
    except Exception:
        return (
            service._record_event(
                EventKind.ERROR_RECORDED,
                {
                    "reason": "outside authorization or input validation failed",
                    "affects_lifecycle": False,
                },
            ),
        )
    # Commit the decision before any service effect. Journal failures must propagate.
    emitted = _commit_external_decision(service, current, intent, approved=approved)
    if not approved:
        return tuple(emitted)
    assert adapter is not None
    assert intent.decision is not None
    decision = intent.decision
    emitted.append(_record_external(service, intent.model_copy(update={"phase": "submitting"})))
    try:
        _check_external_authority(service, proposal)
        observation = adapter.submit(proposal, decision, tuple(inputs))
        result = ExternalActionRecord(
            phase="observed", proposal=proposal, decision=decision, observation=observation
        )
        result = ExternalActionRecord.model_validate_json(result.model_dump_json())
    except Exception:
        result = ExternalActionRecord(phase="unknown", proposal=proposal, decision=decision)
    emitted.append(_record_external(service, result))
    return tuple(emitted)


def decide_external_result(
    service: SessionService, command: SessionCommand, current: ExternalResultRecord
) -> tuple[SessionEvent, ...]:
    """Record the decision on a result release and submit approved content to the model once."""
    proposal, adapter = current.proposal, service._external_actions
    approved = command.kind == CommandKind.APPROVE.value
    if current.phase == "authorized" and not approved:
        return _withdraw_external_authorization(service, command, current)
    _check_retained_authorization(command, current)
    emitted: list[SessionEvent] = []
    try:
        if current.phase not in {"proposed", "authorized"}:
            raise ExternalActionError("result release already has a decision")
        if approved:
            _check_result_authority(service, proposal)
            if (
                not service.backend.wait_for_idle(0)
                or service._worker_request(
                    lambda: service.backend.pending_action_group(session_id=command.session_id),
                    stale=None,
                )
                is not None
            ):
                raise ExternalActionError("worker has unfinished execution or review")
            assert adapter is not None
            outside = external_action_records(service.replay_events())[proposal.operation_id]
            adapter.check_result_policy(
                outside,
                proposal.content_json,
                proposal.model_route_json,
                command.actor_id,
                external_now(service.clock),
            )
            _check_result_authority(service, proposal)
            authorized, authorization_events = service._authorize_backend(
                command, purpose="release reviewed outside result to the worker model"
            )
            emitted.extend(authorization_events)
            if not authorized:
                return tuple(emitted)
            _check_result_authority(service, proposal)
        decision = _external_decision(service, command, proposal, approved=approved)
        intent = ExternalResultRecord(
            phase="authorized" if approved else "denied",
            proposal=proposal,
            decision=current.decision or decision,
        )
    except Exception:
        return (
            *emitted,
            service._record_event(
                EventKind.ERROR_RECORDED,
                {
                    "reason": "outside result release validation failed",
                    "affects_lifecycle": False,
                },
            ),
        )
    # Journal failure must prevent disclosure, not be reduced to a delivery error.
    emitted.extend(_commit_external_decision(service, current, intent, approved=approved))
    if not approved:
        return tuple(emitted)
    assert intent.decision is not None
    decision = intent.decision
    emitted.append(_record_external(service, intent.model_copy(update={"phase": "submitting"})))
    stream: tuple[BackendEvent, ...] = ()
    phase: Literal["unknown", "submitted"]
    try:
        _check_result_authority(service, proposal)
        stream = service._worker_request(
            lambda: service.backend.submit_context(
                session_id=command.session_id, content=proposal.message
            ),
            stale=(BackendErrorEvent(error_code=BackendErrorCode.WORKER_STOPPED),),
        )
        phase = (
            "unknown"
            if any(isinstance(event, BackendErrorEvent) for event in stream)
            else "submitted"
        )
    except Exception:
        phase = "unknown"
    emitted.append(
        _record_external(
            service, ExternalResultRecord(phase=phase, proposal=proposal, decision=decision)
        )
    )
    emitted.extend(service._translate_backend_events(stream))
    return tuple(emitted)


def observe_external_actions_locked(
    service: SessionService, adapter: ExternalActionAdapter
) -> tuple[SessionEvent, ...]:
    """Query retained outside operation IDs and record each outcome that changed."""
    emitted: list[SessionEvent] = []
    for record in external_action_records(service.replay_events()).values():
        if record.phase in {"proposed", "authorized", "denied"} or record.terminal:
            continue
        if record.proposal.worker.project_id != service._worker_project_id:
            raise ExternalActionError("outside journal belongs to another project")
        try:
            observed = ExternalActionRecord(
                phase="observed",
                proposal=record.proposal,
                decision=record.decision,
                observation=adapter.observe(record.proposal),
            )
            # Validate even an adapter returning an unchecked model_copy.
            observed = ExternalActionRecord.model_validate_json(observed.model_dump_json())
        except Exception:
            observed = ExternalActionRecord(
                phase="unknown",
                proposal=record.proposal,
                decision=record.decision,
            )
        if observed != record:
            emitted.append(_record_external(service, observed))
    return tuple(emitted)


def close_external(
    service: SessionService,
    record: ExternalActionRecord | ExternalResultRecord,
    phase: Literal["expired", "withdrawn"],
    withdrawal: ExternalActionDecision | None = None,
) -> SessionEvent:
    """Record that a review or undispatched authorization can no longer cause an effect."""
    if isinstance(record, ExternalActionRecord):
        return _record_external(
            service,
            ExternalActionRecord(
                phase=phase,
                proposal=record.proposal,
                decision=record.decision,
                withdrawal=withdrawal,
            ),
        )
    return _record_external(
        service,
        ExternalResultRecord(
            phase=phase,
            proposal=record.proposal,
            decision=record.decision,
            withdrawal=withdrawal,
        ),
    )


def external_decision_boundary(
    service: SessionService, record: ExternalActionRecord | ExternalResultRecord
) -> SessionEvent:
    """Locate the original durable decision, never a request-supplied checkpoint."""
    kind = (
        EventKind.EXTERNAL_ACTION_RECORDED
        if isinstance(record, ExternalActionRecord)
        else EventKind.EXTERNAL_RESULT_RECORDED
    )
    payload = record.model_dump(mode="json")
    return next(
        event
        for event in service.replay_events()
        if event.kind == kind and event.payload.get("record") == payload
    )


def external_now(clock: Callable[[], str]) -> datetime:
    """Read the session clock as the timezone-aware time outside decisions are valid at."""
    value = datetime.fromisoformat(clock().replace("Z", "+00:00"))
    if value.tzinfo is None or value.utcoffset() is None:
        raise ExternalActionError("outside operation requires a timezone-aware clock")
    return value


def _expire_external_review(
    service: SessionService, record: ExternalActionRecord | ExternalResultRecord | None
) -> SessionEvent | None:
    """Close an undecided review after its validity so the same content can be re-proposed.

    The expired proposal can never be approved; a fresh review has its own identity,
    validity interval and decision.
    """
    if (
        record is None
        or record.phase != "proposed"
        or external_now(service.clock) < record.proposal.expires_at
    ):
        return None
    return close_external(service, record, "expired")


def _withdraw_external_authorization(
    service: SessionService,
    command: SessionCommand,
    current: ExternalActionRecord | ExternalResultRecord,
) -> tuple[SessionEvent, ...]:
    """Let the researcher's denial close an approval that provably never dispatched."""
    assert current.decision is not None
    boundary = close_external(
        service,
        current,
        "withdrawn",
        _external_decision(service, command, current.proposal, approved=False),
    )
    emitted = (boundary, *service._checkpoint_boundary(boundary))
    service._complete_external_command(current.decision.command_id)
    return emitted


def _check_retained_authorization(
    command: SessionCommand, current: ExternalActionRecord | ExternalResultRecord
) -> None:
    """Allow only the command that recorded a retained authorization to dispatch it."""
    if current.phase == "authorized" and (
        current.decision is None
        or current.decision.command_id != command.command_id
        or current.decision.actor_id != command.actor_id
    ):
        raise SessionRecoveryError("Retained outside authorization belongs to another command")


def _external_decision(
    service: SessionService,
    command: SessionCommand,
    proposal: ExternalActionProposal | ExternalResultProposal,
    *,
    approved: bool,
) -> ExternalActionDecision:
    """Bind the deciding command and actor to the exact reviewed proposal."""
    return ExternalActionDecision(
        command_id=command.command_id,
        actor_id=command.actor_id,
        decision="approved" if approved else "denied",
        decided_at=external_now(service.clock),
        proposal_sha256=proposal.sha256,
    )


def _commit_external_decision(
    service: SessionService,
    current: ExternalActionRecord | ExternalResultRecord,
    intent: ExternalActionRecord | ExternalResultRecord,
    *,
    approved: bool,
) -> list[SessionEvent]:
    """Journal a new decision, or locate the retained one, and checkpoint it before any effect."""
    emitted: list[SessionEvent] = []
    if current.phase == "authorized":
        boundary = external_decision_boundary(service, current)
    else:
        boundary = _record_external(service, intent)
        emitted.append(boundary)
    emitted.extend(service._checkpoint_boundary(boundary, required=approved))
    return emitted


def _authorized_adapter(
    service: SessionService, proposal: ExternalActionProposal | ExternalResultProposal
) -> ExternalActionAdapter | None:
    """Return the outside service only while the proposing worker and both policies still hold."""
    lease, adapter = service._worker_lease, service._external_actions
    policy_sha256 = gateway_policy_sha256(service.policy_profile)
    if (
        lease is None
        or adapter is None
        or not lease.active
        or lease.intent != proposal.worker
        or service._worker_project_id != proposal.worker.project_id
        or service._worker_admission_error_locked() is not None
        or policy_sha256 != proposal.worker.gateway_policy_sha256
        or adapter.policy_sha256 != proposal.service_policy_sha256
    ):
        return None
    return adapter


def _check_external_authority(service: SessionService, proposal: ExternalActionProposal) -> None:
    adapter = _authorized_adapter(service, proposal)
    if (
        adapter is None
        or proposal.request.tool_name not in adapter.tool_names
        or not proposal.created_at <= external_now(service.clock) < proposal.expires_at
    ):
        raise ExternalActionError("outside operation authority is stale or unavailable")


def _check_result_authority(service: SessionService, proposal: ExternalResultProposal) -> None:
    if (
        _authorized_adapter(service, proposal) is None
        or _result_model_route(service) != proposal.model_route_json
        or not proposal.created_at <= external_now(service.clock) < proposal.expires_at
    ):
        raise ExternalActionError("result release authority or destination changed")


def _result_model_route(service: SessionService) -> str:
    return canonical_request(
        json.dumps(
            {
                "backend_id": service.backend.backend_id,
                "model_id": service.backend.model_id,
                "profile_id": service.backend.model_profile_id,
                "endpoint": service.backend.model_endpoint,
                "credential_reference": service.backend.credential_reference,
                "capability_tier": service.backend.capability_tier,
                "action_review_mode": service._review_mode_locked(
                    sandboxed=service._sandboxed_locked()
                ),
                "data_handling": (
                    None
                    if service.model_data_handling is None
                    else service.model_data_handling.model_dump(mode="json")
                ),
            }
        )
    )


def _record_external(
    service: SessionService, record: ExternalActionRecord | ExternalResultRecord
) -> SessionEvent:
    """Journal one validated transition of an outside operation or result release."""
    events = service.replay_events()
    if isinstance(record, ExternalActionRecord):
        validate_external_transition(
            external_action_records(events).get(record.proposal.operation_id), record
        )
        kind = EventKind.EXTERNAL_ACTION_RECORDED
    else:
        validate_result_transition(
            external_result_records(events).get(record.proposal.release_id), record
        )
        kind = EventKind.EXTERNAL_RESULT_RECORDED
    return service._record_event(
        kind,
        {
            "record": cast(dict[str, JsonValue], record.model_dump(mode="json")),
            **service._evidence_policy(required=record.phase == "authorized"),
            "worker_attempt_id": str(record.proposal.worker.attempt_id),
            "worker_project_id": str(record.proposal.worker.project_id),
        },
    )
