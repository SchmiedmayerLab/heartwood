# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Native worker proposals require outside authorization before a service effect."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from openhands.sdk.tool.client_tool import ClientToolSpec
from test_worker_native_integration import _application, _stop, _submit
from test_worker_native_integration import infrastructure as infrastructure
from worker_native_fixture import NativeFixture

from deploy.tests.external_service_fixture import (
    RecordsAdapter,
    SyntheticRecords,
    records_client,
    serve_records,
)
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
