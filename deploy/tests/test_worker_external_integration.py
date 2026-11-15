# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Native worker proposals require outside authorization before a service effect."""

from __future__ import annotations

import json
import time
from contextlib import suppress
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from openhands.sdk.tool.client_tool import ClientToolSpec
from test_worker_native_integration import _application, _assert_absent, _stop, _submit
from test_worker_native_integration import infrastructure as infrastructure
from worker_native_fixture import NativeFixture

from deploy.tests.external_service_fixture import (
    RecordsAdapter,
    SyntheticRecords,
    records_client,
    serve_records,
)
from heartwood.core_adapter.action_review import REVIEW_MODES
from heartwood.core_adapter.external_actions import ExternalActionRecord, external_action_records
from heartwood.gateway._openhands_sdk import OpenHandsSdkBackend
from heartwood.session import CommandKind, EventKind, SessionCommand


@pytest.mark.parametrize("decision", ["approved", "denied", "pending", "lost-reply"])
def test_native_outside_effect_requires_retained_authorization(
    infrastructure: NativeFixture, decision: str
) -> None:
    fixture = infrastructure
    records = SyntheticRecords(fixture.private)
    spec = ClientToolSpec(
        name="propose_record",
        description="Propose a synthetic record for outside review without storing it.",
        parameters={
            "type": "object",
            "properties": {"record": {"type": "string", "enum": ["synthetic-result"]}},
            "required": ["record"],
            "additionalProperties": False,
        },
    )
    fixture.provider.script = [
        ("propose_record", {"record": "synthetic-result"}),
        ("finish", {"message": "Synthetic proposal dispatched; no external effect is implied."}),
    ]
    adapter: RecordsAdapter | None = None

    def outside_adapter(_session_id: str) -> RecordsAdapter:
        assert adapter is not None
        return adapter

    application = _application(
        fixture,
        retain_checkpoints=True,
        external_actions_factory=outside_adapter,
        client_tools=(spec,),
    )
    assert application.project.project_id is not None
    token = records.token(application.project.project_id)
    fixture.provider.forbidden_values += (token,)
    with serve_records(records) as url, records_client(url, token) as client:
        adapter = RecordsAdapter(client)
        service = application._service("composition")
        try:
            _submit(
                application,
                CommandKind.CHAT,
                {"prompt": "Propose the synthetic result for outside review."},
            )
            backend = service.backend
            assert isinstance(backend, OpenHandsSdkBackend)
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                service.reconcile()
                proposals = [
                    event
                    for event in service.replay_events()
                    if event.kind == EventKind.TOOL_CALL_PROPOSED
                    and event.payload.get("tool_name") == "propose_record"
                ]
                if proposals:
                    break
                time.sleep(0.1)
            else:
                raise AssertionError("Native worker did not emit the expected service proposal")
            assert len(proposals) == 1
            attempt = service.worker_attempt()
            assert attempt is not None
            assert attempt.phase == "active"
            assert proposals[0].payload["worker_attempt_id"] == str(attempt.intent.attempt_id)
            records.active = attempt.intent
            event = service.propose_external_action(proposals[0].event_id)
            record = ExternalActionRecord.model_validate(event.payload["record"])
            assert record.phase == "proposed"
            assert records.effects() == records.submit_calls == 0
            assert len(fixture.provider.observations) == 1
            assert fixture.evidence is not None
            evidence = fixture.evidence("composition")
            retained_before = len(evidence.records.history())

            def require_retention() -> None:
                assert len(evidence.records.history()) > retained_before

            adapter.before_submit = require_retention
            if decision != "pending":
                request = SessionCommand(
                    command_id=str(uuid4()),
                    session_id="composition",
                    kind=CommandKind.DENY if decision == "denied" else CommandKind.APPROVE,
                    actor_id="researcher",
                    created_at=datetime.now(UTC).isoformat(),
                    payload={"target_type": "action-set", "target_id": record.proposal.group_id},
                )
                records.drop_after_submit = decision == "lost-reply"
                application.handle(request)
                if decision == "lost-reply":
                    assert (
                        external_action_records(service.replay_events())[
                            record.proposal.operation_id
                        ].phase
                        == "unknown"
                    )
                    assert records.effects() == records.submit_calls == 1
                    service.reconcile_external_actions()
                    assert records.observe_calls >= 1
                assert application.handle(request).replayed
            retained = external_action_records(service.replay_events())[
                record.proposal.operation_id
            ]
            expected = int(decision in {"approved", "lost-reply"})
            assert records.effects() == records.submit_calls == expected
            assert retained.phase == (
                "observed" if expected else decision if decision == "denied" else "proposed"
            )
            if expected:
                assert retained.observation is not None
                assert retained.observation.operation.status == "completed"
                assert evidence.records.history()
                # A repeated native request uses the service's independent idempotency key.
                assert retained.decision is not None
                adapter.submit(retained.proposal, retained.decision, (adapter.input,))
                assert records.effects() == 1
            assert token not in str(service.replay_events())
            assert token not in str(service.audit_log.read())
            worker_id = str(attempt.intent.attempt_id)
            _stop(application)
            # Revoking the worker does not authorize pending or denied operations.
            assert records.effects() == expected
            (fixture.output / "evidence.json").write_text(
                json.dumps(
                    {
                        "drill_id": "native-outside-effect.v1",
                        "decision": decision,
                        "worker_attempt_id": worker_id,
                        "worker_image": fixture.worker,
                        "operation_id": str(record.proposal.operation_id),
                        "proposal_sha256": record.proposal.sha256,
                        "effect_count": records.effects(),
                        "submission_calls": records.submit_calls,
                        "observation_calls": records.observe_calls,
                        "retained_checkpoints": len(evidence.records.history()),
                        "native_proposal_admitted": True,
                        "replayed_approval_resubmitted": False,
                        "sensitive_data_ready": False,
                    },
                    indent=2,
                )
                + "\n"
            )
        finally:
            _stop(application)


def test_native_contained_action_is_automatic_and_outside_effect_waits(
    infrastructure: NativeFixture,
) -> None:
    """Under the sandbox default the gateway allows in-worker actions and nothing that leaves."""
    fixture = infrastructure
    records = SyntheticRecords(fixture.private)
    spec = ClientToolSpec(
        name="propose_record",
        description="Propose a synthetic record for outside review without storing it.",
        parameters={
            "type": "object",
            "properties": {"record": {"type": "string", "enum": ["synthetic-result"]}},
            "required": ["record"],
            "additionalProperties": False,
        },
    )
    terminal = "printf contained > /workspace/contained.txt"
    fixture.provider.script = [
        ("terminal", {"command": terminal}),
        ("propose_record", {"record": "synthetic-result"}),
        ("finish", {"message": "Synthetic proposal dispatched; no external effect is implied."}),
    ]
    adapter: RecordsAdapter | None = None

    def outside_adapter(_session_id: str) -> RecordsAdapter:
        assert adapter is not None
        return adapter

    application = _application(
        fixture,
        retain_checkpoints=True,
        external_actions_factory=outside_adapter,
        client_tools=(spec,),
        action_review_modes=REVIEW_MODES,
    )
    assert application.project.project_id is not None
    assert application.action_settings()["review_mode"] == "review-outside-effects"
    token = records.token(application.project.project_id)
    fixture.provider.forbidden_values += (token,)
    with serve_records(records) as url, records_client(url, token) as client:
        adapter = RecordsAdapter(client)
        service = application._service("composition")
        try:
            _submit(
                application,
                CommandKind.CHAT,
                {"prompt": "Write the contained marker, then propose the synthetic result."},
            )
            backend = service.backend
            assert isinstance(backend, OpenHandsSdkBackend)
            # No researcher command is sent: only the gateway can have let the command run.
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                service.reconcile()
                proposals = [
                    event
                    for event in service.replay_events()
                    if event.kind == EventKind.TOOL_CALL_PROPOSED
                    and event.payload.get("tool_name") == "propose_record"
                ]
                if proposals and backend.wait_for_idle(0):
                    break
                time.sleep(0.1)
            else:
                raise AssertionError("Native worker did not reach the outside proposal")
            assert len(proposals) == 1
            assert len(fixture.provider.observations) == 2
            attempt = service.worker_attempt()
            assert attempt is not None
            assert attempt.phase == "active"
            assert service.action_review_mode() == "review-outside-effects"

            events = service.replay_events()
            (contained,) = [
                event
                for event in events
                if event.kind == EventKind.TOOL_CALL_PROPOSED
                and event.payload.get("tool_name") == "terminal"
            ]
            assert contained.payload["arguments"] == {"command": terminal}
            assert contained.payload["review_class"] == "workspace"
            assert proposals[0].payload["review_class"] == "other"
            (approval,) = [event for event in events if event.kind == EventKind.APPROVAL_RECORDED]
            assert approval.payload["actor_id"] == "gateway"
            assert approval.payload["decision"] == "approved"
            assert approval.payload["review"] == {
                "mode": "review-outside-effects",
                "rule": "sandbox.contained",
            }
            assert approval.payload["tool_call_ids"] == [contained.payload["tool_call_id"]]
            assert approval.payload["worker_attempt_id"] == str(attempt.intent.attempt_id)
            assert approval.payload["evidence_mode"] == "required"
            assert [
                event.payload["actor_id"]
                for event in events
                if event.kind == EventKind.COMMAND_RECEIVED
                and event.payload.get("command_kind") == CommandKind.APPROVE.value
            ] == ["gateway"]
            executed = [
                event.payload["tool_call_id"]
                for event in events
                if event.kind == EventKind.TOOL_EXECUTION_RECORDED
            ]
            assert executed == [contained.payload["tool_call_id"]]
            observation = fixture.control.exec(
                f"hw-{attempt.intent.attempt_id.hex[:16]}",
                ["/usr/bin/test", "-s", "/workspace/contained.txt"],
                workspace="default",
                timeout_seconds=15,
                no_login_shell=True,
            )
            assert observation.exit_code == 0, observation.stderr
            assert fixture.evidence is not None
            evidence = fixture.evidence("composition")
            assert evidence.records.history()

            # The action that leaves the worker waits, on its own digest-bound review.
            pending = backend.pending_action_group(session_id="composition")
            assert pending is not None
            assert [action.tool_name for action in pending.actions] == ["propose_record"]
            records.active = attempt.intent
            outside = service.propose_external_action(proposals[0].event_id)
            record = ExternalActionRecord.model_validate(outside.payload["record"])
            assert record.phase == "proposed"
            for _ in range(20):
                service.reconcile()
                time.sleep(0.1)
            events = service.replay_events()
            assert len([e for e in events if e.kind == EventKind.APPROVAL_RECORDED]) == 1
            retained = external_action_records(events)[record.proposal.operation_id]
            assert retained.phase == "proposed"
            assert retained.decision is None
            assert records.effects() == records.submit_calls == 0
            assert len(fixture.provider.observations) == 2
            assert token not in str(events)
            assert token not in str(service.audit_log.read())
            service.audit_log.verify()
            worker_id = str(attempt.intent.attempt_id)
            _stop(application)
            # Ending the session never dispatches the operation the researcher did not decide.
            assert records.effects() == records.submit_calls == 0
            (fixture.output / "evidence.json").write_text(
                json.dumps(
                    {
                        "drill_id": "native-contained-review.v1",
                        "review_mode": "review-outside-effects",
                        "worker_attempt_id": worker_id,
                        "worker_image": fixture.worker,
                        "automatic_actor": approval.payload["actor_id"],
                        "automatic_rule": "sandbox.contained",
                        "automatic_group_id": approval.payload["group_id"],
                        "contained_effect_present": True,
                        "operation_id": str(record.proposal.operation_id),
                        "proposal_sha256": record.proposal.sha256,
                        "outside_decision_recorded": False,
                        "effect_count": records.effects(),
                        "submission_calls": records.submit_calls,
                        "sensitive_data_ready": False,
                    },
                    indent=2,
                )
                + "\n"
            )
        finally:
            _stop(application)


def test_native_contained_action_is_refused_after_revocation(
    infrastructure: NativeFixture,
) -> None:
    """A contained action proposed before its worker is revoked can never run afterwards."""
    fixture = infrastructure
    terminal = "printf revoked > /workspace/revoked.txt"
    fixture.provider.script = [
        ("terminal", {"command": terminal}),
        ("finish", {"message": "Synthetic contained step completed."}),
    ]
    application = _application(fixture, action_review_modes=REVIEW_MODES)
    # Under the sandbox default the gateway would allow this action in the operation that
    # journals it; reviewing every action keeps the proposal undecided until revocation.
    assert application.action_settings()["default_mode"] == "review-outside-effects"
    application.select_action_review_mode("review-all")
    service = application._service("composition")
    try:
        _submit(application, CommandKind.CHAT, {"prompt": "Write the contained marker."})
        backend = service.backend
        assert isinstance(backend, OpenHandsSdkBackend)
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            service.reconcile()
            requests = [
                event.payload["request"]
                for event in service.replay_events()
                if event.kind == EventKind.CONFIRMATION_REQUESTED
                and isinstance(event.payload.get("request"), dict)
            ]
            if requests and backend.wait_for_idle(0):
                break
            time.sleep(0.1)
        else:
            raise AssertionError("Native worker did not propose the contained action")
        (request,) = requests
        events = service.replay_events()
        (proposal,) = [
            event
            for event in events
            if event.kind == EventKind.TOOL_CALL_PROPOSED
            and event.payload.get("tool_name") == "terminal"
        ]
        assert proposal.payload["arguments"] == {"command": terminal}
        assert proposal.payload["review_class"] == "workspace"
        attempt = service.worker_attempt()
        assert attempt is not None
        assert attempt.phase == "active"
        assert proposal.payload["worker_attempt_id"] == str(attempt.intent.attempt_id)
        assert not [e for e in events if e.kind == EventKind.APPROVAL_RECORDED]
        assert len(fixture.provider.observations) == 1

        # The same fence a failed worker exchange raises; prompt revocation follows from it.
        service.fence_worker_attempt(attempt_id=attempt.intent.attempt_id)
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            # A fenced worker exchange may fail, which is the expected refusal.
            with suppress(Exception):
                service.reconcile()
            revoked = service.worker_attempt()
            assert revoked is not None
            if revoked.phase == "revoked":
                break
            time.sleep(0.5)
        else:
            raise AssertionError("The fenced worker was not revoked before its deadline")
        assert revoked.revocation is not None
        assert revoked.revocation.outcome == "revoked"
        _assert_absent(str(attempt.intent.attempt_id))

        # The researcher's approval of the action proposed before revocation is refused.
        before = len(service.replay_events())
        _submit(
            application,
            CommandKind.APPROVE,
            {"target_type": "action-set", "target_id": request["group_id"]},
        )
        later = service.replay_events()[before:]
        assert any(event.kind == EventKind.ERROR_RECORDED for event in later)
        assert not [e for e in service.replay_events() if e.kind == EventKind.APPROVAL_RECORDED]
        # An executed action would return its result to the model in another request.
        assert len(fixture.provider.observations) == 1
        _stop(application)
        assert not [
            e for e in service.replay_events() if e.kind == EventKind.TOOL_EXECUTION_RECORDED
        ]
        (fixture.output / "evidence.json").write_text(
            json.dumps(
                {
                    "drill_id": "native-revoked-contained.v2",
                    "default_review_mode": "review-outside-effects",
                    "review_mode": "review-all",
                    "worker_attempt_id": str(attempt.intent.attempt_id),
                    "worker_image": fixture.worker,
                    "contained_proposals_before_revocation": 1,
                    "review_class": "workspace",
                    "decisions_before_revocation": 0,
                    "researcher_approvals_refused": 1,
                    "executions": 0,
                    "provider_requests": len(fixture.provider.observations),
                    "attempt_resources_absent": True,
                    "sensitive_data_ready": False,
                },
                indent=2,
            )
            + "\n"
        )
    finally:
        _stop(application)
