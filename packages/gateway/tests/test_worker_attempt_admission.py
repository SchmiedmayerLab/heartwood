# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import asyncio
from collections.abc import Callable
from uuid import uuid4

import httpx
import pytest
from openhands.sdk.event.conversation_state import FULL_STATE_KEY, ConversationStateUpdateEvent
from pydantic import ValidationError
from websockets.asyncio.server import ServerConnection, serve

from heartwood.core_adapter.worker_attempts import WorkerAttemptLease
from heartwood.gateway._remote_workspace import (
    AdmittedRemoteWorkspace,
    WorkerResponseBudget,
    WorkerResponseError,
    WorkerResponseTransport,
)
from heartwood.gateway._worker_events import WorkerEventAdmission


@pytest.fixture
def attempt(worker_attempt: Callable[[str], WorkerAttemptLease]) -> WorkerAttemptLease:
    return worker_attempt("http://worker.test")


def _transport(
    attempt: WorkerAttemptLease, handler: Callable[[httpx.Request], httpx.Response]
) -> WorkerResponseTransport:
    return WorkerResponseTransport(
        host="http://worker.test",
        tool_modules={},
        budget=WorkerResponseBudget(attempt=attempt),
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.parametrize(
    "operation", ["foreign", "encoded", "fork", "workspace", "create-foreign", "create-missing"]
)
def test_other_conversation_requests_never_reach_worker(
    attempt: WorkerAttemptLease, operation: str
) -> None:
    calls: list[httpx.Request] = []

    def receive(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={})

    identity = str(attempt.intent.conversation_id)
    paths = {
        "foreign": f"/api/conversations/{uuid4()}",
        "encoded": f"/api/conversations/{identity}%2Fevents",
        "fork": f"/api/conversations/{identity}/fork",
        "workspace": "/api/execute",
        "create-foreign": "/api/conversations",
        "create-missing": "/api/conversations",
    }
    payload = {"conversation_id": str(uuid4())} if operation == "create-foreign" else {}
    with (
        httpx.Client(
            transport=_transport(attempt, receive), base_url="http://worker.test"
        ) as client,
        pytest.raises(WorkerResponseError, match="retained conversation"),
    ):
        client.post(paths[operation], json=payload)
    assert not calls
    assert not attempt.active


def test_create_and_snapshot_preserve_retained_identity(attempt: WorkerAttemptLease) -> None:
    identity = str(attempt.intent.conversation_id)
    calls: list[str] = []

    def receive(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={"id": identity})

    with httpx.Client(
        transport=_transport(attempt, receive), base_url="http://worker.test"
    ) as client:
        client.post("/api/conversations", json={"conversation_id": identity})
        client.get(f"/api/conversations/{identity}")
        client.get(f"/api/conversations/{identity}/events/snapshot")
    assert len(calls) == 3
    assert attempt.active


@pytest.mark.parametrize("path", ["", "/events/snapshot"])
def test_response_cannot_change_native_identity(attempt: WorkerAttemptLease, path: str) -> None:
    with (
        httpx.Client(
            transport=_transport(attempt, lambda _: httpx.Response(200, json={"id": str(uuid4())})),
            base_url="http://worker.test",
        ) as client,
        pytest.raises(WorkerResponseError, match="another conversation"),
    ):
        client.get(f"/api/conversations/{attempt.intent.conversation_id}{path}")
    assert not attempt.active


@pytest.mark.parametrize("empty", [False, True])
def test_fencing_rejects_response_already_in_flight(
    attempt: WorkerAttemptLease, empty: bool
) -> None:
    def receive(_request: httpx.Request) -> httpx.Response:
        attempt.fence()
        return httpx.Response(204) if empty else httpx.Response(200, json={"items": []})

    with (
        httpx.Client(
            transport=_transport(attempt, receive), base_url="http://worker.test"
        ) as client,
        pytest.raises(WorkerResponseError, match="closed"),
    ):
        client.get(f"/api/conversations/{attempt.intent.conversation_id}/events/search")


def test_reset_cannot_replenish_or_move_attempt(attempt: WorkerAttemptLease) -> None:
    workspace = AdmittedRemoteWorkspace(
        host="http://worker.test",
        working_dir="/workspace",
        attempt=attempt,
        api_key="synthetic-private-key",
    )
    assert "attempt" not in workspace.model_dump()
    assert "synthetic-private-key" not in repr(workspace)
    budget = workspace._response_budget
    budget.close()
    workspace.reset_client()
    with pytest.raises(ValidationError, match="closed"):
        AdmittedRemoteWorkspace.model_validate(workspace)
    assert workspace._response_budget is budget
    try:
        with pytest.raises(WorkerResponseError, match="closed"):
            workspace.client.get(f"/api/conversations/{attempt.intent.conversation_id}")
    finally:
        workspace.reset_client()


def test_budget_exhaustion_also_fences_callbacks(attempt: WorkerAttemptLease) -> None:
    budget = WorkerResponseBudget(attempt=attempt, max_responses=1)
    budget.charge(0, new_response=True)
    with pytest.raises(WorkerResponseError, match="exhausted"):
        budget.charge(0, new_response=True)
    assert not attempt.active


def test_transports_require_observed_runtime_origin(attempt: WorkerAttemptLease) -> None:
    budget = WorkerResponseBudget(attempt=attempt)
    with pytest.raises(WorkerResponseError, match="retained binding"):
        WorkerResponseTransport(
            host="http://other.test",
            tool_modules={},
            budget=budget,
            transport=httpx.MockTransport(lambda _: httpx.Response(204)),
        )
    with pytest.raises(WorkerResponseError, match="retained binding"):
        WorkerEventAdmission(host="http://other.test", tool_modules={}, budget=budget)


def test_event_connection_cannot_select_another_conversation(attempt: WorkerAttemptLease) -> None:
    policy = WorkerEventAdmission(
        host="http://worker.test", tool_modules={}, budget=WorkerResponseBudget(attempt=attempt)
    )
    with pytest.raises(WorkerResponseError, match="endpoint"):
        policy.connect(f"ws://worker.test/sockets/events/{uuid4()}", max_size=4096)
    assert not attempt.active


@pytest.mark.parametrize("key", ["id", FULL_STATE_KEY])
def test_state_events_cannot_replace_conversation_identity(
    attempt: WorkerAttemptLease, key: str
) -> None:
    policy = WorkerEventAdmission(
        host="http://worker.test", tool_modules={}, budget=WorkerResponseBudget(attempt=attempt)
    )
    identity = str(uuid4())
    message = ConversationStateUpdateEvent(
        key=key, value={"id": identity} if key == FULL_STATE_KEY else identity
    ).model_dump_json()
    with pytest.raises(WorkerResponseError, match="failed admission"):
        policy.validate_message(message)
    assert not attempt.active


def test_queued_events_are_rejected_after_fencing(attempt: WorkerAttemptLease) -> None:
    policy = WorkerEventAdmission(
        host="http://worker.test", tool_modules={}, budget=WorkerResponseBudget(attempt=attempt)
    )
    message = ConversationStateUpdateEvent(key="execution_status", value="idle").model_dump_json()
    policy.validate_message(message)
    attempt.fence()
    with pytest.raises(WorkerResponseError, match="failed admission"):
        policy.validate_message(message)


def test_changed_workspace_host_cannot_move_credentials(attempt: WorkerAttemptLease) -> None:
    workspace = AdmittedRemoteWorkspace(
        host="http://worker.test",
        working_dir="/workspace",
        attempt=attempt,
        api_key="synthetic-private-key",
    )
    try:
        _ = workspace.client
        workspace.host = "http://other.test"
        with pytest.raises(WorkerResponseError, match="retained binding"):
            _ = workspace.client
        assert not attempt.active
    finally:
        workspace.reset_client()


def test_fencing_during_handshake_prevents_connection_admission(
    worker_attempt: Callable[[str], WorkerAttemptLease],
) -> None:
    async def run() -> None:
        async def connected(socket: ServerConnection) -> None:
            bound_attempt.fence()
            await socket.wait_closed()

        async with serve(connected, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            host = f"http://127.0.0.1:{port}"
            bound_attempt = worker_attempt(host)
            admission = WorkerEventAdmission(
                host=host, tool_modules={}, budget=WorkerResponseBudget(attempt=bound_attempt)
            )
            with pytest.raises(WorkerResponseError, match="connection failed admission"):
                async with admission.connect(
                    f"ws://127.0.0.1:{port}/sockets/events/{bound_attempt.intent.conversation_id}",
                    max_size=4096,
                ):
                    pytest.fail("fenced socket was admitted")

    asyncio.run(run())


def test_rejected_binding_does_not_include_credentials(attempt: WorkerAttemptLease) -> None:
    with pytest.raises(ValidationError, match="retained binding") as error:
        AdmittedRemoteWorkspace(
            host="http://other.test",
            working_dir="/workspace",
            attempt=attempt,
            api_key="synthetic-private-key",
        )
    assert "synthetic-private-key" not in str(error.value)
