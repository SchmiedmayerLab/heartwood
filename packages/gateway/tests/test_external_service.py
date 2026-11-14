# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Native MCP conformance with a synthetic privileged service, not deployment qualification."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from fastmcp.server.auth.providers.jwt import RSAKeyPair
from pydantic import AnyHttpUrl

from heartwood.core_adapter import (
    BackendEventSink,
    BackendToolCallEvent,
    DeterministicAgentBackend,
    ProposedToolCall,
    SessionService,
    TokenDeltaSink,
)
from heartwood.core_adapter.external_actions import (
    ExternalActionDecision,
    ExternalActionError,
    ExternalActionRecord,
    external_action_records,
)
from heartwood.core_adapter.worker_attempts import WorkerAttemptIntent, WorkerRuntimeBinding
from heartwood.session import CommandKind, EventKind, SessionCommand

if TYPE_CHECKING:
    from deploy.tests.external_service_fixture import (
        _AUDIENCE,
        _INPUT,
        _ISSUER,
        RecordsAdapter,
        SyntheticRecords,
        records_client,
        serve_records,
    )
else:
    # Load the shared fixture without exposing checkout application sources.
    _spec = importlib.util.spec_from_file_location(
        "external_service_fixture",
        Path(__file__).resolve().parents[3] / "deploy/tests/external_service_fixture.py",
    )
    assert _spec is not None
    assert _spec.loader is not None
    _fixture = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_fixture)
    _AUDIENCE = _fixture._AUDIENCE
    _INPUT = _fixture._INPUT
    _ISSUER = _fixture._ISSUER
    RecordsAdapter = _fixture.RecordsAdapter
    SyntheticRecords = _fixture.SyntheticRecords
    records_client = _fixture.records_client
    serve_records = _fixture.serve_records


class ProposalWorker(DeterministicAgentBackend):
    def bind_runtime(self, *, event_sink: BackendEventSink, token_sink: TokenDeltaSink) -> None:
        super().bind_runtime(event_sink=event_sink, token_sink=token_sink)
        self.emit = event_sink


@contextmanager
def worker_session(
    root: Path, state: SyntheticRecords, adapter: RecordsAdapter
) -> Iterator[tuple[SessionService, ExternalActionRecord]]:
    worker = ProposalWorker()
    assert state.active is not None
    service = SessionService.local_default(
        root, backend=worker, worker_project_id=state.active.project_id, external_actions=adapter
    )
    intent = service.prepare_worker_attempt(
        image_digest="sha256:" + "b" * 64,
        sandbox_policy_sha256="c" * 64,
        input_manifest_sha256="d" * 64,
    )
    state.active = intent
    service.bind_worker_runtime(
        attempt_id=intent.attempt_id,
        runtime=WorkerRuntimeBinding(
            sandbox_id="synthetic-worker",
            endpoint=AnyHttpUrl("http://worker.test"),
            image_digest=intent.image_digest,
            sandbox_policy_sha256=intent.sandbox_policy_sha256,
        ),
    )
    try:
        worker.emit(
            (
                BackendToolCallEvent(
                    tool_call=ProposedToolCall(
                        tool_call_id="native-record-proposal",
                        tool_name="propose_record",
                        risk="unknown",
                        summary="Propose a synthetic record",
                        arguments={"record": "synthetic-result"},
                    ),
                    source_event_id="native-proposal-event",
                ),
            )
        )
        event = service.propose_external_action(service.replay_events()[-1].event_id)
        yield service, ExternalActionRecord.model_validate_json(json.dumps(event.payload["record"]))
    finally:
        service.close()


def approval(service: SessionService, record: ExternalActionRecord) -> SessionCommand:
    return SessionCommand(
        command_id=str(uuid4()),
        session_id=service.store.session_id,
        kind=CommandKind.APPROVE,
        actor_id="researcher",
        created_at=datetime.now(UTC).isoformat(),
        payload={"target_type": "action-set", "target_id": record.proposal.group_id},
    )


def retained(service: SessionService) -> ExternalActionRecord:
    return list(external_action_records(service.replay_events()).values())[-1]


@pytest.fixture
def records(tmp_path: Path) -> SyntheticRecords:
    state = SyntheticRecords(tmp_path)
    state.active = WorkerAttemptIntent(
        project_id=uuid4(),
        session_id="session-main",
        attempt_id=uuid4(),
        conversation_id=uuid4(),
        image_digest="sha256:" + "b" * 64,
        gateway_policy_sha256="c" * 64,
        sandbox_policy_sha256="d" * 64,
        input_manifest_sha256="e" * 64,
    )
    return state


def test_native_service_applies_one_authorized_effect(
    tmp_path: Path, records: SyntheticRecords
) -> None:
    assert records.active is not None
    with (
        serve_records(records) as url,
        records_client(url, records.token(records.active.project_id)) as client,
    ):
        adapter = RecordsAdapter(client)
        with worker_session(tmp_path / "gateway", records, adapter) as (service, record):
            request = approval(service, record)
            service.handle(request)
            observed = retained(service)
            assert observed.terminal
            assert observed.observation is not None
            assert observed.observation.operation.status == "completed"
            assert records.effects() == records.submit_calls == 1
            assert service.handle(request).replayed
            assert records.effects() == records.submit_calls == 1
            assert observed.decision is not None
            again = adapter.submit(record.proposal, observed.decision, (_INPUT,))
            assert again.proposal_sha256 == observed.proposal.sha256
            assert records.effects() == 1


@pytest.mark.parametrize("changed", ["attempt", "policy", "actor", "project", "expiry"])
def test_service_rechecks_authority_after_gateway_decision(
    tmp_path: Path,
    records: SyntheticRecords,
    changed: str,
) -> None:
    assert records.active is not None
    project = records.active.project_id if changed != "project" else uuid4()
    token = records.token(project, actor="other-researcher" if changed == "actor" else "researcher")
    with serve_records(records) as url, records_client(url, token) as client:
        adapter = RecordsAdapter(client)
        with worker_session(tmp_path / "gateway", records, adapter) as (service, record):

            def changed_at_dispatch() -> None:
                assert retained(service).phase == "submitting"
                if changed == "attempt":
                    records.active = None
                elif changed == "policy":
                    records.policy_sha256 = "f" * 64
                elif changed == "expiry":
                    records.clock = lambda: datetime.now(UTC) + timedelta(minutes=6)

            adapter.before_submit = changed_at_dispatch
            service.handle(approval(service, record))
            assert records.submit_calls == 1
            assert records.effects() == 0
            assert retained(service).phase == "unknown"
            assert token not in str(service.replay_events())
            assert token not in str(service.audit_log.read())


@pytest.mark.parametrize("invalid", ["audience", "signature", "expired"])
def test_native_authentication_rejects_invalid_credentials(
    records: SyntheticRecords,
    invalid: str,
) -> None:
    assert records.active is not None
    keys = RSAKeyPair.generate() if invalid == "signature" else records.keys
    token = keys.create_token(
        subject="researcher",
        issuer=_ISSUER,
        audience="https://other.synthetic.invalid" if invalid == "audience" else _AUDIENCE,
        scopes=["records"],
        expires_in_seconds=-60 if invalid == "expired" else 300,
        additional_claims={"project": str(records.active.project_id)},
    )
    with (
        serve_records(records) as url,
        pytest.raises(
            Exception,
            match=r"401 Unauthorized|Server returned an error response|MCP Connection Failure",
        ),
        records_client(url, token),
    ):
        pytest.fail("invalid credential opened the privileged service")
    assert 401 in records.response_statuses
    assert records.submit_calls == records.effects() == 0


def test_lost_reply_is_observed_after_restart_without_second_submission(
    tmp_path: Path,
    records: SyntheticRecords,
) -> None:
    assert records.active is not None
    project = records.active.project_id
    token = records.token(project)
    with serve_records(records) as url:
        with (
            records_client(url, token) as client,
            worker_session(
                tmp_path / "gateway",
                records,
                RecordsAdapter(client),
            ) as (service, record),
        ):
            records.drop_after_submit = True
            request = approval(service, record)
            service.handle(request)
            assert records.effects() == records.submit_calls == 1
            assert retained(service).phase == "unknown"
        records.active = None

    restarted = SyntheticRecords(tmp_path)
    assert restarted.active is None
    with (
        serve_records(restarted) as restored_url,
        records_client(restored_url, restarted.token(project)) as restored_client,
    ):
        restored = SessionService.local_default(
            tmp_path / "gateway",
            worker_project_id=project,
            external_actions=RecordsAdapter(restored_client),
        )
        try:
            restored.reconcile_external_actions()
            observation = retained(restored).observation
            assert observation is not None
            assert observation.operation.operation_id == str(record.proposal.operation_id)
            assert observation.operation.status == "completed"
            assert restored.handle(request).replayed
            assert (
                records.submit_calls
                == records.effects()
                == restarted.effects()
                == restarted.observe_calls
                == 1
            )
            assert restarted.submit_calls == 0
            assert not any(
                event.kind == EventKind.ERROR_RECORDED for event in restored.replay_events()
            )
        finally:
            restored.close()


def test_concurrent_service_replay_and_changed_content_have_one_effect(
    tmp_path: Path,
    records: SyntheticRecords,
) -> None:
    assert records.active is not None
    token = records.token(records.active.project_id)
    with (
        serve_records(records) as url,
        records_client(url, token) as first,
        records_client(url, token) as second,
    ):
        adapter = RecordsAdapter(first)
        with worker_session(tmp_path / "gateway", records, adapter) as (service, record):
            service.handle(approval(service, record))
            approved = retained(service)
            assert approved.decision is not None
            decision = approved.decision
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [
                    pool.submit(RecordsAdapter(client).submit, record.proposal, decision, (_INPUT,))
                    for client in (first, second)
                ]
                results = [future.result(timeout=10) for future in futures]
            assert all(result.proposal_sha256 == record.proposal.sha256 for result in results)
            assert records.submit_calls == 3
            assert records.effects() == 1
            changed = record.proposal.model_copy(update={"source_event_id": "different-proposal"})
            changed_decision = decision.model_copy(update={"proposal_sha256": changed.sha256})
            with pytest.raises(ExternalActionError, match="did not establish"):
                adapter.submit(changed, changed_decision, (_INPUT,))
            another = record.proposal.model_copy(update={"operation_id": uuid4()})
            reused = decision.model_copy(update={"proposal_sha256": another.sha256})
            with pytest.raises(ExternalActionError, match="did not establish"):
                adapter.submit(another, reused, (_INPUT,))
            assert records.effects() == 1


def test_service_checks_input_bytes_independently(
    tmp_path: Path, records: SyntheticRecords
) -> None:
    assert records.active is not None
    with (
        serve_records(records) as url,
        records_client(url, records.token(records.active.project_id)) as client,
    ):
        adapter = RecordsAdapter(client)
        with worker_session(tmp_path / "gateway", records, adapter) as (_service, record):
            decision = ExternalActionDecision(
                command_id=str(uuid4()),
                actor_id="researcher",
                decision="approved",
                decided_at=datetime.now(UTC),
                proposal_sha256=record.proposal.sha256,
            )
            with pytest.raises(ExternalActionError, match="did not establish"):
                adapter.submit(record.proposal, decision, (b"changed after gateway verification",))
            assert records.submit_calls == 1
            assert records.effects() == 0


def test_service_revocation_is_atomic_with_a_queued_effect(
    tmp_path: Path,
    records: SyntheticRecords,
) -> None:
    assert records.active is not None
    with (
        serve_records(records) as url,
        records_client(url, records.token(records.active.project_id)) as client,
        worker_session(tmp_path / "gateway", records, RecordsAdapter(client)) as (service, record),
    ):
        with (
            sqlite3.connect(records.database) as authority,
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            authority.execute("BEGIN IMMEDIATE")
            pending = pool.submit(service.handle, approval(service, record))
            try:
                assert records.submission_started.wait(5)
                authority.execute("UPDATE authority SET intent=NULL WHERE id=1")
            finally:
                authority.commit()
            pending.result(timeout=10)
        assert records.submit_calls == 1
        assert records.effects() == 0
        assert retained(service).phase == "unknown"
