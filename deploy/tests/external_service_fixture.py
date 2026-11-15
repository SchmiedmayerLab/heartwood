# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Synthetic independently authenticated records service for boundary qualification."""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Thread
from typing import Any, cast
from uuid import UUID

import pytest
import uvicorn
from fastmcp import FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server.auth.providers.jwt import JWTVerifier, RSAKeyPair
from fastmcp.server.dependencies import get_access_token
from mcp.types import CallToolResult
from openhands.sdk.mcp import MCPClient
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from heartwood.core_adapter.external_actions import (
    ExternalActionDecision,
    ExternalActionError,
    ExternalActionObservation,
    ExternalActionProposal,
    ExternalActionRecord,
    ExternalActionRequest,
    ExternalInputReference,
    canonical_request,
)
from heartwood.core_adapter.worker_attempts import WorkerAttemptIntent
from heartwood.schemas import JsonValue, PlatformOperationObservation

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

    def prepare_result(self, record: ExternalActionRecord) -> str:
        return canonical_request(self.observe(record.proposal).model_dump_json())

    def check_result_policy(
        self,
        record: ExternalActionRecord,
        content_json: str,
        model_route_json: str,
        actor_id: str,
        now: datetime,  # noqa: ARG002
    ) -> None:
        content = json.loads(content_json)
        route = json.loads(model_route_json)
        if (
            actor_id != "researcher"
            or record.observation is None
            or content["proposal_sha256"] != record.proposal.sha256
            or content["operation"]["operation_id"] != str(record.proposal.operation_id)
            or route["endpoint"] != "http://127.0.0.1:8765/v1/chat/completions"
        ):
            raise ExternalActionError("synthetic release authorization denied")

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
