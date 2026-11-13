# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Native MCP conformance with a synthetic privileged service, not deployment qualification."""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import sqlite3
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Thread
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
import uvicorn
from fastmcp import FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server.auth.providers.jwt import JWTVerifier, RSAKeyPair
from fastmcp.server.dependencies import get_access_token
from mcp.types import CallToolResult
from openhands.sdk.mcp import MCPClient
from pydantic import AnyHttpUrl
from starlette.types import ASGIApp, Message, Receive, Scope, Send

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
    ExternalActionObservation,
    ExternalActionProposal,
    ExternalActionRecord,
    ExternalActionRequest,
    ExternalInputReference,
    canonical_request,
    external_action_records,
)
from heartwood.core_adapter.worker_attempts import WorkerAttemptIntent, WorkerRuntimeBinding
from heartwood.schemas import JsonValue, PlatformOperationObservation
from heartwood.session import CommandKind, EventKind, SessionCommand

_SERVICE = "synthetic-records"
_ISSUER = "https://identity.synthetic.invalid"
_AUDIENCE = "https://records.synthetic.invalid"
_POLICY = "a" * 64
_INPUT = b"synthetic analysis result"


class SyntheticRecords:
    """Test-only domain service: authenticate independently and atomically record one effect."""

    def __init__(self, root: Path) -> None:
        self.database = root / "service.sqlite"
        self.keys = RSAKeyPair.generate()
        self.clock: Callable[[], datetime] = lambda: datetime.now(UTC)
        self.submission_started = Event()
        self.drop_response = Event()
        self.drop_after_submit = False
        self.submit_calls = 0
        self.observe_calls = 0
        self.response_statuses: list[int] = []
        with sqlite3.connect(self.database) as database:
            database.execute(
                "CREATE TABLE IF NOT EXISTS records (project TEXT, operation TEXT, digest TEXT, "
                "input_digest TEXT, command TEXT, actor TEXT, PRIMARY KEY(project, operation), "
                "UNIQUE(project, command))"
            )
            database.execute(
                "CREATE TABLE IF NOT EXISTS authority "
                "(id INTEGER PRIMARY KEY CHECK(id=1), intent TEXT, policy TEXT NOT NULL)"
            )
            database.execute("INSERT OR IGNORE INTO authority VALUES (1, NULL, ?)", (_POLICY,))
        self.server = FastMCP(
            "Synthetic records",
            auth=JWTVerifier(
                public_key=self.keys.public_key,
                algorithm="RS256",
                issuer=_ISSUER,
                audience=_AUDIENCE,
                required_scopes=["records"],
            ),
        )
        self.server.tool(self.submit_record)
        self.server.tool(self.observe_record)

    @property
    def active(self) -> WorkerAttemptIntent | None:
        with sqlite3.connect(self.database) as database:
            value = database.execute("SELECT intent FROM authority WHERE id=1").fetchone()[0]
        return WorkerAttemptIntent.model_validate_json(value) if value is not None else None

    @active.setter
    def active(self, value: WorkerAttemptIntent | None) -> None:
        with sqlite3.connect(self.database) as database:
            database.execute(
                "UPDATE authority SET intent=? WHERE id=1",
                (value.model_dump_json() if value else None,),
            )

    @property
    def policy_sha256(self) -> str:
        with sqlite3.connect(self.database) as database:
            return str(database.execute("SELECT policy FROM authority WHERE id=1").fetchone()[0])

    @policy_sha256.setter
    def policy_sha256(self, value: str) -> None:
        with sqlite3.connect(self.database) as database:
            database.execute("UPDATE authority SET policy=? WHERE id=1", (value,))

    def token(self, project: UUID, *, actor: str = "researcher", audience: str = _AUDIENCE) -> str:
        return self.keys.create_token(
            subject=actor,
            issuer=_ISSUER,
            audience=audience,
            scopes=["records"],
            additional_claims={"project": str(project)},
        )

    def _actor(self, project: str) -> str:
        token = get_access_token()
        if (
            token is None
            or token.claims.get("project") != project
            or token.expires_at is None
            or token.expires_at <= self.clock().timestamp()
        ):
            raise ValueError("project authorization denied")
        return str(token.claims["sub"])

    def _authorize(
        self,
        database: sqlite3.Connection,
        proposal: ExternalActionProposal,
        decision: ExternalActionDecision,
    ) -> str:
        actor = self._actor(str(proposal.worker.project_id))
        intent_json, policy = database.execute(
            "SELECT intent, policy FROM authority WHERE id=1"
        ).fetchone()
        active = WorkerAttemptIntent.model_validate_json(intent_json) if intent_json else None
        # The service does not accept gateway success flags or worker approval messages.
        ExternalActionRecord(phase="submitting", proposal=proposal, decision=decision)
        now = self.clock()
        if (
            active != proposal.worker
            or policy != proposal.service_policy_sha256
            or actor != decision.actor_id
            or not proposal.created_at <= now < proposal.expires_at
            or decision.decided_at > now
            or decision.decision != "approved"
            or proposal.request.service_id != _SERVICE
            or proposal.request.destination != "synthetic-results"
            or proposal.request.tool_name != "propose_record"
            or proposal.request.request_json != '{"record":"synthetic-result"}'
        ):
            raise ValueError("operation authorization denied")
        return actor

    def submit_record(
        self, proposal_json: str, decision_json: str, content_base64: str
    ) -> dict[str, Any]:
        proposal = ExternalActionProposal.model_validate_json(proposal_json)
        decision = ExternalActionDecision.model_validate_json(decision_json)
        self.submit_calls += 1
        project = str(proposal.worker.project_id)
        content = base64.b64decode(content_base64, validate=True)
        references = proposal.request.inputs
        digest = hashlib.sha256(content).hexdigest()
        if (
            len(references) != 1
            or len(content) != references[0].size_bytes
            or digest != references[0].sha256
        ):
            raise ValueError("input binding denied")
        operation = str(proposal.operation_id)
        self.submission_started.set()
        with sqlite3.connect(self.database, timeout=5) as database:
            database.execute("BEGIN IMMEDIATE")
            actor = self._authorize(database, proposal, decision)
            previous = database.execute(
                "SELECT digest FROM records WHERE project=? AND operation=?", (project, operation)
            ).fetchone()
            if previous is not None:
                if previous[0] != proposal.sha256:
                    raise ValueError("operation identifier conflict")
            else:
                database.execute(
                    "INSERT INTO records VALUES (?, ?, ?, ?, ?, ?)",
                    (project, operation, proposal.sha256, digest, decision.command_id, actor),
                )
        if self.drop_after_submit:
            self.drop_after_submit = False
            self.drop_response.set()
        return self._observation(project, operation, proposal.sha256)

    def observe_record(self, project: str, operation: str, digest: str) -> dict[str, Any]:
        self._actor(project)
        self.observe_calls += 1
        with sqlite3.connect(self.database) as database:
            existing = database.execute(
                "SELECT digest FROM records WHERE project=? AND operation=?", (project, operation)
            ).fetchone()
        if existing is None or existing[0] != digest:
            raise ValueError("operation observation unavailable")
        return self._observation(project, operation, digest)

    def _observation(self, project: str, operation: str, digest: str) -> dict[str, Any]:
        return ExternalActionObservation(
            proposal_sha256=digest,
            operation=PlatformOperationObservation(
                project_context_id=project,
                operation_id=operation,
                service_id=_SERVICE,
                backend_operation_ref=operation,
                observed_at=datetime.now(UTC),
                status="completed",
                platform_record_ref=f"synthetic-record:{operation}",
            ),
        ).model_dump(mode="json")

    def effects(self) -> int:
        with sqlite3.connect(self.database) as database:
            return int(database.execute("SELECT COUNT(*) FROM records").fetchone()[0])


class LostReply:
    """Drop only a test operation's response after the service committed its effect."""

    def __init__(self, app: ASGIApp, state: SyntheticRecords) -> None:
        self.app, self.state = app, state

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        dropped = False

        async def sending(message: Message) -> None:
            nonlocal dropped
            if message["type"] == "http.response.start":
                self.state.response_statuses.append(message["status"])
            if message["type"] == "http.response.start" and self.state.drop_response.is_set():
                self.state.drop_response.clear()
                dropped = True
                await send({"type": "http.response.start", "status": 503, "headers": []})
                await send({"type": "http.response.body", "body": b""})
            elif not dropped:
                await send(message)

        await self.app(scope, receive, sending)


@contextmanager
def serve_records(state: SyntheticRecords) -> Iterator[str]:
    app = LostReply(
        state.server.http_app(path="/mcp", json_response=True, stateless_http=True), state
    )
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
        thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while not server.started and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server.started, "synthetic MCP service did not start"
            yield f"http://127.0.0.1:{listener.getsockname()[1]}/mcp"
        finally:
            server.should_exit = True
            thread.join(5)
            assert not thread.is_alive(), "synthetic MCP service did not stop"


@contextmanager
def records_client(url: str, token: str) -> Iterator[MCPClient]:
    # Keep the loopback fixture independent of workstation proxy configuration.
    with pytest.MonkeyPatch.context() as environment:
        environment.setenv("NO_PROXY", "127.0.0.1,localhost")
        environment.setenv("no_proxy", "127.0.0.1,localhost")
        client = MCPClient(StreamableHttpTransport(url, auth=token), timeout=5)  # type: ignore[no-untyped-call]
        try:
            client.call_async_from_sync(client.connect, timeout=5)
            yield client
        finally:
            client.sync_close()


class RecordsAdapter:
    """Test-only mapping to this service's native tools; MCP owns transport and lifecycle."""

    tool_names = frozenset({"propose_record"})
    policy_sha256 = _POLICY

    def __init__(self, client: MCPClient) -> None:
        self.client = client
        self.input = _INPUT
        self.before_submit: Callable[[], None] = lambda: None

    def prepare(self, tool_name: str, arguments: dict[str, JsonValue]) -> ExternalActionRequest:
        if tool_name not in self.tool_names or arguments != {"record": "synthetic-result"}:
            raise ExternalActionError("unsupported synthetic request")
        return ExternalActionRequest(
            service_id=_SERVICE,
            tool_name=tool_name,
            destination="synthetic-results",
            summary="Store a synthetic result",
            request_json=canonical_request(json.dumps(arguments)),
            inputs=(
                ExternalInputReference(
                    reference="synthetic-input",
                    size_bytes=len(self.input),
                    sha256=hashlib.sha256(self.input).hexdigest(),
                ),
            ),
        )

    def check_policy(self, proposal: ExternalActionProposal, actor_id: str, now: datetime) -> None:
        if actor_id != "researcher" or not proposal.created_at <= now < proposal.expires_at:
            raise ExternalActionError("synthetic authorization denied")

    def read_input(self, reference: ExternalInputReference) -> bytes:
        if reference.reference != "synthetic-input":
            raise ExternalActionError("unknown synthetic input")
        return self.input

    def call(self, name: str, arguments: dict[str, Any]) -> ExternalActionObservation:
        result = cast(
            CallToolResult,
            self.client.call_async_from_sync(
                self.client.call_tool_mcp,
                name,
                arguments,
                timeout=5,
            ),
        )
        wire = result.model_dump(mode="json", by_alias=True)
        if wire.get("isError") is not False or wire.get("structuredContent") is None:
            raise ExternalActionError("synthetic service did not establish an outcome")
        return ExternalActionObservation.model_validate_json(json.dumps(wire["structuredContent"]))

    def submit(
        self,
        proposal: ExternalActionProposal,
        decision: ExternalActionDecision,
        inputs: tuple[bytes, ...],
    ) -> ExternalActionObservation:
        if len(inputs) != 1:
            raise ExternalActionError("synthetic service requires one input")
        self.before_submit()
        return self.call(
            "submit_record",
            {
                "proposal_json": proposal.model_dump_json(),
                "decision_json": decision.model_dump_json(),
                "content_base64": base64.b64encode(inputs[0]).decode("ascii"),
            },
        )

    def observe(self, proposal: ExternalActionProposal) -> ExternalActionObservation:
        return self.call(
            "observe_record",
            {
                "project": str(proposal.worker.project_id),
                "operation": str(proposal.operation_id),
                "digest": proposal.sha256,
            },
        )


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
