# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Worker progress is read whole once and afterwards only after the admitted leaf."""

from __future__ import annotations

import asyncio
import json
import tracemalloc
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event, Lock
from typing import Any, cast

import httpx
import pytest
from openhands.sdk import Agent, RemoteConversation
from openhands.sdk.conversation import ConversationExecutionStatus
from openhands.sdk.conversation.conversation_stats import ConversationStats
from openhands.sdk.conversation.request import StartConversationRequest
from openhands.sdk.event import ActionEvent, MessageEvent, StreamingDeltaEvent
from openhands.sdk.event import Event as OpenHandsEvent
from openhands.sdk.event.conversation_state import ConversationStateUpdateEvent
from openhands.sdk.llm import Message, MessageToolCall, TextContent
from openhands.sdk.security import SecurityRisk
from openhands.sdk.testing import TestLLM
from openhands.sdk.workspace import LocalWorkspace
from openhands.tools.terminal import TerminalAction

import heartwood.gateway._openhands_sdk as openhands_sdk_module
import heartwood.gateway._remote_workspace as remote_workspace_module
from heartwood.core_adapter import (
    BackendAgentMessageEvent,
    BackendConfirmationRequestEvent,
    BackendErrorEvent,
    BackendEvent,
    BackendExecutionSettledEvent,
    BackendToolCallEvent,
)
from heartwood.core_adapter.worker_attempts import WorkerAttemptLease
from heartwood.gateway import ModelProfile, OpenHandsSdkBackend
from heartwood.gateway._openhands_sdk import ConversationFactory
from heartwood.gateway._remote_workspace import (
    AdmittedRemoteWorkspace,
    WorkerBranch,
    WorkerResponseBudget,
    WorkerResponseError,
    WorkerResponseTransport,
)

_HOST = "http://worker.test"


def _message(text: str, *, event_id: str | None = None) -> MessageEvent:
    return MessageEvent(
        id=event_id or str(uuid.uuid4()),
        source="agent",
        llm_message=Message(role="assistant", content=[TextContent(text=text)]),
    )


def _action(event_id: str, tool_call_id: str) -> ActionEvent:
    return ActionEvent(
        id=event_id,
        thought=[],
        action=TerminalAction(command="printf synthetic"),
        tool_name="terminal",
        tool_call_id=tool_call_id,
        tool_call=MessageToolCall(
            id=tool_call_id,
            name="terminal",
            arguments=json.dumps({"command": "printf synthetic"}),
            origin="completion",
        ),
        llm_response_id=f"response-{event_id}",
        security_risk=SecurityRisk.LOW,
        summary="Run a bounded command",
    )


class _Worker:
    """Answer branch reads as the pinned agent server does, behind the gateway's admission."""

    def __init__(self, attempt: WorkerAttemptLease | None = None) -> None:
        self.id = attempt.intent.conversation_id if attempt is not None else uuid.uuid4()
        self.events: list[OpenHandsEvent] = []
        self.status = ConversationExecutionStatus.IDLE
        self.requests: list[tuple[str, str | None]] = []
        self.sent = 0
        self.fail: Exception | None = None
        self.answer: Callable[[str, list[OpenHandsEvent]], list[OpenHandsEvent]] | None = None
        self.on_search: Callable[[], None] | None = None
        self.after_read: Callable[[], None] | None = None
        self.workspace = AdmittedRemoteWorkspace(
            host=_HOST, working_dir="/workspace", attempt=attempt
        )
        self.budget = self.workspace._response_budget
        self.workspace._admitted_client = httpx.Client(
            base_url=_HOST,
            transport=WorkerResponseTransport(
                host=_HOST,
                tool_modules={},
                budget=self.budget,
                transport=httpx.MockTransport(self._respond),
            ),
        )

    def append(self, *events: OpenHandsEvent) -> None:
        for event in events:
            self.events.append(event.model_copy(update={"parent_id": self._tip()}))

    def append_update(self) -> None:
        """Log a state update, which names the tip as parent but leaves the branch."""
        stats = ConversationStats().model_dump(mode="json")
        self.events.append(
            ConversationStateUpdateEvent(key="stats", value=stats, parent_id=self._tip())
        )

    def _tip(self) -> str | None:
        branch = self._branch(self.events)
        return branch[-1].id if branch else None

    @staticmethod
    def _branch(events: list[OpenHandsEvent]) -> list[OpenHandsEvent]:
        return [event for event in events if not isinstance(event, ConversationStateUpdateEvent)]

    def branch(self) -> WorkerBranch:
        return WorkerBranch(self.workspace, self.id)

    def state(self) -> tuple[ConversationExecutionStatus, ConversationStats]:
        self.requests.append(("status", None))
        return self.status, ConversationStats()

    def _respond(self, request: httpx.Request) -> httpx.Response:
        route = request.url.path.rsplit("/", 1)[-1]
        page = request.url.params.get("page_id")
        self.requests.append((route, page))
        if self.fail is not None:
            raise self.fail
        if route == "search" and self.on_search is not None:
            self.on_search()
        events = self.answer(route, list(self.events)) if self.answer else list(self.events)
        if self.after_read is not None:
            self.after_read()
        if route == "snapshot":
            branch = self._branch(events)
            body: dict[str, Any] = {
                "id": str(self.id),
                "leaf_event_id": branch[-1].id if branch else None,
                "execution_status": self.status.value,
                "stats": ConversationStats().model_dump(mode="json"),
                "events": [event.model_dump(mode="json") for event in branch],
            }
        else:
            identities = [event.id for event in events]
            start = identities.index(page) if page in identities else 0
            limit = int(request.url.params["limit"])
            selected = events[start : start + limit + 1]
            body = {
                "items": [
                    event.model_dump(mode="json", exclude_none=True) for event in selected[:limit]
                ],
                "next_page_id": selected[limit].id if len(selected) > limit else None,
            }
        content = json.dumps(body).encode()
        self.sent += len(content)
        return httpx.Response(200, content=content, headers={"Content-Type": "application/json"})


class _RemoteState:
    def __init__(self, worker: _Worker) -> None:
        self._worker = worker

    @property
    def execution_status(self) -> ConversationExecutionStatus:
        return self._worker.state()[0]

    @property
    def stats(self) -> ConversationStats:
        return ConversationStats()


class _Remote(RemoteConversation):
    def __init__(self, worker: _Worker) -> None:
        self._id = worker.id
        self._state = cast(Any, _RemoteState(worker))
        self.workspace = worker.workspace

    def close(self) -> None:
        pass


def _backend(tmp_path: Path, conversation: RemoteConversation) -> OpenHandsSdkBackend:
    return OpenHandsSdkBackend(
        profile=ModelProfile(
            profile_id="local",
            model="openai/synthetic",
            base_url="http://127.0.0.1:9/v1",
            policy_endpoint="http://127.0.0.1:9/v1/chat/completions",
            credential_kind="none",
        ),
        workspace=tmp_path / "workspace",
        persistence_dir=tmp_path / "openhands",
        conversation_key="worker-progress",
        env={},
        conversation_factory=cast(ConversationFactory, lambda *_: conversation),
    )


def test_first_read_is_one_snapshot_and_later_reads_transfer_only_new_events(
    tmp_path: Path,
) -> None:
    worker = _Worker()
    worker.status = ConversationExecutionStatus.WAITING_FOR_CONFIRMATION
    worker.append(_message("Planning."), _action("pending-action", "pending-call"))
    worker.append_update()
    backend = _backend(tmp_path, _Remote(worker))
    try:
        events = backend.reconcile(session_id="session-1", known_source_event_ids=frozenset())
        assert worker.requests == [("snapshot", None)]
        assert any(isinstance(event, BackendToolCallEvent) for event in events)
        requests = [event for event in events if isinstance(event, BackendConfirmationRequestEvent)]
        assert [request.tool_call.tool_call_id for request in requests] == ["pending-call"]
        known = frozenset(event.source_event_id for event in events if event.source_event_id)

        # Nothing new: one page holding the admitted leaf and a logged state update.
        worker.requests.clear()
        sent = worker.sent
        assert backend.reconcile(session_id="session-1", known_source_event_ids=known) == ()
        assert worker.requests == [("status", None), ("search", "pending-action")]
        page = sum(len(event.model_dump_json(exclude_none=True)) for event in worker.events[-2:])
        assert worker.sent - sent < page + 128

        worker.requests.clear()
        worker.status = ConversationExecutionStatus.FINISHED
        worker.append(_message("Finished.", event_id="final-message"))
        events = backend.reconcile(session_id="session-1", known_source_event_ids=known)
        assert worker.requests == [("status", None), ("search", "pending-action")]
        assert [
            event.message for event in events if isinstance(event, BackendAgentMessageEvent)
        ] == ["Finished."]
    finally:
        backend.close()


@pytest.mark.parametrize("failure", ["unreadable", "other-conversation"])
def test_unavailable_branch_is_content_safe_and_never_projects_actions(
    tmp_path: Path, failure: str
) -> None:
    worker = _Worker()
    worker.status = ConversationExecutionStatus.WAITING_FOR_CONFIRMATION
    worker.append(_action("other-action", "other-call"))
    if failure == "unreadable":
        worker.fail = ValueError("synthetic-private-worker-content")
    conversation = _Remote(worker)
    if failure == "other-conversation":
        # The worker answers with its own identity for another conversation's branch.
        conversation._id = uuid.uuid4()
    backend = _backend(tmp_path, conversation)
    try:
        events = backend.reconcile(session_id="session-1", known_source_event_ids=frozenset())
        assert len(events) == 1
        assert isinstance(events[0], BackendErrorEvent)
        assert "synthetic-private-worker-content" not in repr(events)
    finally:
        backend.close()


def _forge(kind: str) -> Callable[[str, list[OpenHandsEvent]], list[OpenHandsEvent]]:
    def answer(route: str, events: list[OpenHandsEvent]) -> list[OpenHandsEvent]:
        if route == "snapshot":
            return events[:2]
        first, leaf, *later = events
        if kind == "replaced-leaf":
            return [first, leaf.model_copy(update={"source": "user"}), *later]
        if kind == "missing":
            return [first, leaf, later[1]]
        if kind == "reordered":
            return [first, leaf, later[1], later[0]]
        if kind == "repeated":
            return [first, leaf, first.model_copy(update={"parent_id": leaf.id})]
        if kind == "repeated-new":
            return [first, leaf, later[0], later[0].model_copy(update={"parent_id": later[0].id})]
        if kind == "removed-leaf":
            return [first, *later]
        if kind == "misplaced-update":
            update = ConversationStateUpdateEvent(key="stats", value={}, parent_id=first.id)
            return [first, leaf, update, *later]
        assert kind == "forked"
        return [first, leaf, later[0].model_copy(update={"parent_id": first.id})]

    return answer


@pytest.mark.parametrize(
    "kind",
    [
        "replaced-leaf",
        "missing",
        "reordered",
        "repeated",
        "repeated-new",
        "removed-leaf",
        "misplaced-update",
        "forked",
    ],
)
def test_repeated_missing_reordered_or_forged_events_fence_the_attempt(
    kind: str, worker_attempt: Callable[[str], WorkerAttemptLease]
) -> None:
    attempt = worker_attempt(_HOST)
    worker = _Worker(attempt)
    worker.append(*(_message(f"Step {index}.") for index in range(4)))
    worker.answer = _forge(kind)
    branch = worker.branch()
    assert len(branch.read(worker.state).events) == 2
    with pytest.raises(WorkerResponseError):
        branch.read(worker.state)
    assert not attempt.active
    with pytest.raises(WorkerResponseError, match="closed"):
        worker.budget.charge(0)


def test_reads_walk_every_page_after_the_leaf() -> None:
    worker = _Worker()
    worker.append(_message("Start."))
    branch = worker.branch()
    branch.read(worker.state)
    worker.append(*(_message(f"Step {index}.") for index in range(250)))
    worker.requests.clear()
    assert [event.id for event in branch.read(worker.state).events] == [
        event.id for event in worker.events
    ]
    searches = [page for route, page in worker.requests if route == "search"]
    assert searches == [worker.events[0].id, worker.events[100].id, worker.events[200].id]
    # A later walk starts again from the new leaf, so the walk bound never accumulates.
    for _ in range(3):
        worker.append(_message("More."))
        branch.read(worker.state)


def test_long_session_retains_a_bounded_branch_and_reads_stay_small(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bound = 2 * 1024 * 1024
    monkeypatch.setattr(remote_workspace_module, "_MAX_SNAPSHOT_BYTES", bound)
    worker = _Worker()
    worker.append(_message("Start."))
    branch = worker.branch()
    branch.read(worker.state)
    step = "x" * 4000
    retained = len(worker.events[0].model_dump_json())
    peaks: list[int] = []
    transfers: list[int] = []

    def grow() -> None:
        nonlocal retained
        while True:
            worker.append(*(_message(step) for _ in range(3)))
            worker.append_update()
            retained += sum(len(event.model_dump_json()) for event in worker.events[-4:-1])
            sent = worker.sent
            tracemalloc.start()
            try:
                branch.read(worker.state)
            finally:
                peaks.append(tracemalloc.get_traced_memory()[1])
                tracemalloc.stop()
            transfers.append(worker.sent - sent)

    with pytest.raises(WorkerResponseError, match="snapshot may carry"):
        grow()
    # The read that would retain more than a snapshot may carry is the one that fails.
    assert retained > bound
    assert retained - 3 * len(worker.events[-2].model_dump_json()) <= bound
    assert len(transfers) > 150
    # Each read transfers the leaf and the new events, however long the session is.
    assert max(transfers) < 6 * 4096
    assert max(peaks[-20:]) < 1024 * 1024
    with pytest.raises(WorkerResponseError, match="closed"):
        worker.budget.charge(0)


def test_status_is_read_before_the_events_it_describes() -> None:
    worker = _Worker()
    worker.append(_message("Start."))
    branch = worker.branch()
    branch.read(worker.state)
    worker.requests.clear()
    worker.append(_message("More."))
    branch.read(worker.state)
    assert worker.requests == [("status", None), ("search", worker.events[0].id)]


def test_concurrent_reads_share_one_consistent_branch() -> None:
    worker = _Worker()
    worker.append(_message("Start."))
    branch = worker.branch()
    branch.read(worker.state)
    worker.append(_message("Early."))
    both_reading, answered, claim = Barrier(2, timeout=5), Event(), Lock()
    claimed = False

    def before() -> None:
        nonlocal claimed
        both_reading.wait()
        with claim:
            later, claimed = not claimed, True
        if later:
            # This reader is answered after the other, with one more event.
            assert answered.wait(5)
            worker.append(_message("Late."))

    worker.on_search, worker.after_read = before, answered.set
    with ThreadPoolExecutor(max_workers=2) as executor:
        reads = [executor.submit(branch.read, worker.state) for _ in range(2)]
        results = sorted((read.result(timeout=5).events for read in reads), key=len)
    worker.on_search = worker.after_read = None
    identities = [event.id for event in worker.events]
    assert [[event.id for event in events] for events in results] == [
        identities[:-1],
        identities,
    ]
    worker.requests.clear()
    assert [event.id for event in branch.read(worker.state).events] == identities
    assert worker.requests == [("status", None), ("search", identities[-1])]


@pytest.mark.parametrize(
    ("signal_progress", "concurrent_event"), [(False, False), (True, False), (True, True)]
)
def test_remote_progress_reads_are_bounded_and_always_finish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, signal_progress: bool, concurrent_event: bool
) -> None:
    monkeypatch.setattr(openhands_sdk_module, "_AGENT_PROGRESS_POLL_SECONDS", 0.01)
    monkeypatch.setattr(
        openhands_sdk_module,
        "_AGENT_REMOTE_PROGRESS_FALLBACK_SECONDS",
        60.0 if signal_progress else 0.3,
    )
    started, release, progress_published = Event(), Event(), Event()
    emitted: list[BackendEvent] = []
    worker = _Worker()
    worker.workspace._response_budget = worker.budget = WorkerResponseBudget(max_responses=10)
    worker.workspace.reset_client()
    worker.workspace._admitted_client = httpx.Client(
        base_url=_HOST,
        transport=WorkerResponseTransport(
            host=_HOST,
            tool_modules={},
            budget=worker.budget,
            transport=httpx.MockTransport(worker._respond),
        ),
    )
    worker.status = ConversationExecutionStatus.RUNNING
    worker.append(_message("Accepted."))

    def reads() -> int:
        return sum(route in {"snapshot", "search"} for route, _page in worker.requests)

    if concurrent_event:

        def concurrent() -> None:
            # Persisted after the first read was answered, so only a later read sees it.
            if reads() == 1:
                persisted = _message("More progress.", event_id="concurrent-message")
                worker.append(persisted)
                backend._handle_sdk_event(persisted)

        worker.after_read = concurrent

    async def run_remote(_conversation: _Remote) -> None:
        started.set()
        while not release.is_set():
            await asyncio.sleep(0.005)
        # No callback: the final boundary must still read the worker's state.
        worker.append(_message("Finished.", event_id="final-message"))
        worker.status = ConversationExecutionStatus.FINISHED

    monkeypatch.setattr(_Remote, "arun", run_remote)
    conversation = _Remote(worker)
    backend = _backend(tmp_path, conversation)

    def publish(events: tuple[BackendEvent, ...]) -> None:
        emitted.extend(events)
        if any(
            isinstance(event, BackendAgentMessageEvent)
            and event.message == ("More progress." if concurrent_event else "Working.")
            for event in events
        ):
            progress_published.set()

    backend.bind_runtime(event_sink=publish, token_sink=lambda _delta: None)
    try:
        assert backend._start_run(session_id="session-1", conversation=conversation)
        assert started.wait(2)
        for _ in range(6):
            backend._handle_sdk_event(StreamingDeltaEvent(content="transient"))
            assert not backend.wait_for_idle(0.01)
        if signal_progress:
            assert reads() == 0
        event = _message("Working.", event_id="progress-message")
        worker.append(event)
        if signal_progress:
            backend._handle_sdk_event(event)
        assert progress_published.wait(2)
    finally:
        release.set()
        assert backend.wait_for_idle(2)
        backend.close()
    assert 2 <= reads() <= 8
    assert sum(route == "snapshot" for route, _page in worker.requests) == 1
    assert any(isinstance(event, BackendExecutionSettledEvent) for event in emitted)
    assert not any(isinstance(event, BackendErrorEvent) for event in emitted)
    assert any(
        isinstance(event, BackendAgentMessageEvent) and event.message == "Finished."
        for event in emitted
    )


@pytest.fixture
def agent_server(tmp_path: Path) -> Iterator[httpx.Client]:
    # The agent server is installed only with the worker dependency group.
    server_api = pytest.importorskip("openhands.agent_server.api")
    server_config = pytest.importorskip("openhands.agent_server.config")
    testclient = pytest.importorskip("fastapi.testclient")
    workspace = tmp_path / "worker-workspace"
    workspace.mkdir()
    app = server_api.create_app(
        server_config.Config(
            session_api_keys=[],
            conversations_path=tmp_path / "worker-conversations",
            workspace_path=workspace,
        )
    )
    with testclient.TestClient(app) as client:
        yield client


def test_pinned_agent_server_branch_extends_by_its_parent_links(
    agent_server: httpx.Client, tmp_path: Path
) -> None:
    request = StartConversationRequest(
        agent=Agent(llm=TestLLM(model="test-model"), tools=[]),
        workspace=LocalWorkspace(working_dir=tmp_path / "worker-workspace"),
        autotitle=False,
    )
    created = agent_server.post(
        "/api/conversations",
        content=request.model_dump_json(exclude_none=True),
        headers={"Content-Type": "application/json"},
    )
    assert created.status_code == 201
    identity = uuid.UUID(created.json()["id"])

    def send(text: str) -> None:
        response = agent_server.post(
            f"/api/conversations/{identity}/events",
            json={"role": "user", "content": [{"type": "text", "text": text}], "run": False},
        )
        assert response.status_code == 200

    send("First synthetic message.")
    host = str(agent_server.base_url).rstrip("/")
    workspace = AdmittedRemoteWorkspace(host=host, working_dir="/workspace")
    paths: list[str] = []

    class Observed(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path.rsplit("/", 1)[-1])
            response = agent_server._transport.handle_request(request)
            return httpx.Response(
                response.status_code, headers=response.headers, content=response.read()
            )

    workspace._admitted_client = httpx.Client(
        base_url=host,
        transport=WorkerResponseTransport(
            host=host, tool_modules={}, budget=workspace._response_budget, transport=Observed()
        ),
    )
    branch = WorkerBranch(workspace, identity)

    def status() -> tuple[ConversationExecutionStatus, ConversationStats]:
        return ConversationExecutionStatus.IDLE, ConversationStats()

    first = branch.read(status).events
    assert first
    assert paths == ["snapshot"]
    for index in range(3):
        send(f"Synthetic message {index}.")
    later = branch.read(status).events
    assert paths == ["snapshot", "search"]
    assert later[: len(first)] == first
    assert len(later) == len(first) + 3
    snapshot = agent_server.get(f"/api/conversations/{identity}/events/snapshot").json()
    assert [event.id for event in later] == [event["id"] for event in snapshot["events"]]
