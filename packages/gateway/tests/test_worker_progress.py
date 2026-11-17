# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Worker progress is read whole once and afterwards only after the admitted leaf."""

from __future__ import annotations

import asyncio
import json
import time
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
from openhands.sdk.conversation.impl.remote_conversation import RemoteEventsList
from openhands.sdk.conversation.request import StartConversationRequest
from openhands.sdk.conversation.state import ConversationState
from openhands.sdk.event import ActionEvent, MessageEvent, ObservationEvent, StreamingDeltaEvent
from openhands.sdk.event import Event as OpenHandsEvent
from openhands.sdk.event.conversation_state import ConversationStateUpdateEvent
from openhands.sdk.llm import LLM, Message, MessageToolCall, TextContent
from openhands.sdk.security import SecurityRisk
from openhands.sdk.security.confirmation_policy import AlwaysConfirm
from openhands.sdk.testing import TestLLM
from openhands.sdk.tool.builtins.think import ThinkAction, ThinkExecutor, ThinkObservation
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
        # What the SDK's event connection last delivered, which can lag the worker.
        self.cached: ConversationExecutionStatus | None = None
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

    def append_update(self, status: ConversationExecutionStatus | None = None) -> None:
        """Log a state update, which names the tip as parent but leaves the branch."""
        key, value = (
            ("stats", ConversationStats().model_dump(mode="json"))
            if status is None
            else ("execution_status", status.value)
        )
        self.events.append(
            ConversationStateUpdateEvent(key=key, value=value, parent_id=self._tip())
        )

    def _tip(self) -> str | None:
        branch = self._branch(self.events)
        return branch[-1].id if branch else None

    @staticmethod
    def _branch(events: list[OpenHandsEvent]) -> list[OpenHandsEvent]:
        return [event for event in events if not isinstance(event, ConversationStateUpdateEvent)]

    def branch(self) -> WorkerBranch:
        return WorkerBranch(self.workspace, self.id)

    def _respond(self, request: httpx.Request) -> httpx.Response:
        route = request.url.path.rsplit("/", 1)[-1]
        if route == str(self.id):
            route = "status"
        page = request.url.params.get("page_id")
        self.requests.append((route, page))
        if self.fail is not None:
            raise self.fail
        if route == "status":
            return self._json(
                {
                    "id": str(self.id),
                    "execution_status": self.status.value,
                    "stats": ConversationStats().model_dump(mode="json"),
                }
            )
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
        return self._json(body)

    def _json(self, body: dict[str, Any]) -> httpx.Response:
        content = json.dumps(body).encode()
        self.sent += len(content)
        return httpx.Response(200, content=content, headers={"Content-Type": "application/json"})


class _RemoteState:
    def __init__(self, worker: _Worker) -> None:
        self._worker = worker

    @property
    def execution_status(self) -> ConversationExecutionStatus:
        return self._worker.cached or self._worker.status

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

        # Nothing new: the status and one page holding the admitted leaf and a logged
        # state update.
        worker.requests.clear()
        sent = worker.sent
        assert (
            backend.reconcile(session_id="session-1", known_source_event_ids=frozenset(known)) == ()
        )
        assert worker.requests == [("status", None), ("search", "pending-action")]
        page = sum(len(event.model_dump_json(exclude_none=True)) for event in worker.events[-2:])
        assert worker.sent - sent < page + 1024

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


def test_status_is_read_from_the_worker_not_from_the_sdk_cache(tmp_path: Path) -> None:
    worker = _Worker()
    worker.append(_message("Planning."))
    backend = _backend(tmp_path, _Remote(worker))
    try:
        backend.reconcile(session_id="session-1", known_source_event_ids=frozenset())
        # The worker now waits for confirmation, but no state update reached the cache.
        worker.cached = ConversationExecutionStatus.RUNNING
        worker.status = ConversationExecutionStatus.WAITING_FOR_CONFIRMATION
        worker.append(_action("pending-action", "pending-call"))
        group = backend.pending_action_group(session_id="session-1")
        assert group is not None
        assert [action.tool_call_id for action in group.actions] == ["pending-call"]
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
    assert len(branch.read().events) == 2
    with pytest.raises(WorkerResponseError):
        branch.read()
    assert not attempt.active
    with pytest.raises(WorkerResponseError, match="closed"):
        worker.budget.charge(0)


def test_reads_walk_every_page_after_the_leaf() -> None:
    worker = _Worker()
    worker.append(_message("Start."))
    branch = worker.branch()
    branch.read()
    worker.append(*(_message(f"Step {index}.") for index in range(250)))
    worker.requests.clear()
    assert [event.id for event in branch.read().events] == [event.id for event in worker.events]
    searches = [page for route, page in worker.requests if route == "search"]
    assert searches == [worker.events[0].id, worker.events[100].id, worker.events[200].id]
    # A later walk starts again from the new leaf, so the walk bound never accumulates.
    for _ in range(3):
        worker.append(_message("More."))
        branch.read()


def test_long_session_retains_a_bounded_branch_and_reads_stay_small(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bound = 2 * 1024 * 1024
    monkeypatch.setattr(remote_workspace_module, "_MAX_SNAPSHOT_BYTES", bound)
    worker = _Worker()
    worker.append(_message("Start."))
    branch = worker.branch()
    branch.read()
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
                branch.read()
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


def test_sdk_history_cache_is_bounded_by_the_walk_and_outgrowing_it_fences(
    monkeypatch: pytest.MonkeyPatch, worker_attempt: Callable[[str], WorkerAttemptLease]
) -> None:
    bound = 256 * 1024
    monkeypatch.setattr(remote_workspace_module, "_MAX_HISTORY_BYTES", bound)
    attempt = worker_attempt(_HOST)
    worker = _Worker(attempt)
    worker.append(_message("Start."))
    # The SDK's own cache, filled as it is at attachment and after every run.
    cache = RemoteEventsList(worker.workspace.client, str(worker.id))
    step = "x" * 4000
    walks = 0
    while attempt.active:
        worker.append(*(_message(step) for _ in range(3)))
        worker.append_update()
        history = sum(len(event.model_dump_json(exclude_none=True)) for event in worker.events)
        # The complete history read when a run ends.
        cache.reconcile()
        cached = sum(len(event.model_dump_json(exclude_none=True)) for event in cache)
        if attempt.active:
            walks += 1
            assert history <= bound
            assert cached == history
    # The walk that would admit more than the bound fences the attempt, and the cache
    # holds no more than the last complete walk admitted.
    assert walks > 10
    assert history > bound
    assert cached <= bound
    with pytest.raises(WorkerResponseError, match="closed"):
        worker.budget.charge(0)


def test_status_is_read_before_the_events_it_describes() -> None:
    worker = _Worker()
    worker.append(_message("Start."))
    branch = worker.branch()
    branch.read()
    worker.requests.clear()
    worker.append(_message("More."))
    branch.read()
    assert worker.requests == [("status", None), ("search", worker.events[0].id)]


def test_logged_status_replaces_the_status_the_worker_last_saved() -> None:
    # The agent server saves its status only when a step releases the conversation, but
    # logs each change when it is made.
    worker = _Worker()
    worker.append(_message("Start."), _action("action-1", "call-1"))
    worker.status = ConversationExecutionStatus.WAITING_FOR_CONFIRMATION
    branch = worker.branch()
    assert branch.read().execution_status == ConversationExecutionStatus.WAITING_FOR_CONFIRMATION
    worker.append_update(ConversationExecutionStatus.RUNNING)
    assert branch.read().execution_status == ConversationExecutionStatus.RUNNING
    # Once the step's result advances the leaf past the update, the status stays running
    # until the worker logs another change.
    worker.append(_message("Partial result."))
    assert branch.read().execution_status == ConversationExecutionStatus.RUNNING
    worker.append_update()
    worker.append_update(ConversationExecutionStatus.FINISHED)
    assert branch.read().execution_status == ConversationExecutionStatus.FINISHED
    worker.status = ConversationExecutionStatus.FINISHED
    assert branch.read().execution_status == ConversationExecutionStatus.FINISHED


def test_an_invalid_logged_status_fences_the_attempt() -> None:
    worker = _Worker()
    worker.append(_message("Start."))
    branch = worker.branch()
    branch.read()
    worker.events.append(
        ConversationStateUpdateEvent(
            key="execution_status", value="unknown", parent_id=worker.events[-1].id
        )
    )
    with pytest.raises(WorkerResponseError, match="state update"):
        branch.read()
    with pytest.raises(WorkerResponseError, match="closed"):
        worker.budget.charge(0)


def test_a_read_never_pairs_a_later_status_with_its_own_older_events() -> None:
    worker = _Worker()
    worker.append(_message("Start."))
    branch = worker.branch()
    branch.read()
    answered, later_read = Event(), Event()
    first = True

    def hold_first_answer() -> None:
        nonlocal first
        if first:
            first = False
            answered.set()
            assert later_read.wait(5)

    worker.after_read = hold_first_answer
    with ThreadPoolExecutor(max_workers=1) as executor:
        earlier = executor.submit(branch.read)
        assert answered.wait(5)
        # The worker proposes an action and logs that it waits for confirmation, which only
        # the later read receives; the earlier read was answered before both.
        worker.append(_action("action-1", "call-1"))
        worker.append_update(ConversationExecutionStatus.WAITING_FOR_CONFIRMATION)
        later = branch.read()
        later_read.set()
        result = earlier.result(timeout=5)
    worker.after_read = None
    assert later.execution_status == ConversationExecutionStatus.WAITING_FOR_CONFIRMATION
    assert result.execution_status == ConversationExecutionStatus.WAITING_FOR_CONFIRMATION
    # The status is returned with the events it describes, so the action awaits review.
    assert result.events == later.events
    assert [event.id for event in ConversationState.get_unmatched_actions(result.events)] == [
        "action-1"
    ]


def test_concurrent_reads_share_one_consistent_branch() -> None:
    worker = _Worker()
    worker.append(_message("Start."))
    branch = worker.branch()
    branch.read()
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
        reads = [executor.submit(branch.read) for _ in range(2)]
        results = sorted((read.result(timeout=5).events for read in reads), key=len)
    worker.on_search = worker.after_read = None
    identities = [event.id for event in worker.events]
    assert [[event.id for event in events] for events in results] == [
        identities[:-1],
        identities,
    ]
    worker.requests.clear()
    assert [event.id for event in branch.read().events] == identities
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


def _create(agent_server: httpx.Client, request: StartConversationRequest) -> uuid.UUID:
    created = agent_server.post(
        "/api/conversations",
        content=request.model_dump_json(exclude_none=True),
        headers={"Content-Type": "application/json"},
    )
    assert created.status_code == 201
    return uuid.UUID(created.json()["id"])


def _admitted_branch(
    agent_server: httpx.Client, request: StartConversationRequest, identity: uuid.UUID
) -> tuple[WorkerBranch, list[str]]:
    """Read the server's branch through the gateway's admission, recording each route."""
    host = str(agent_server.base_url).rstrip("/")
    workspace = AdmittedRemoteWorkspace(host=host, working_dir="/workspace")
    workspace.authorize_request(request)
    paths: list[str] = []

    class Observed(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            route = request.url.path.rsplit("/", 1)[-1]
            paths.append("status" if route == str(identity) else route)
            response = agent_server._transport.handle_request(request)
            return httpx.Response(
                response.status_code, headers=response.headers, content=response.read()
            )

    workspace._admitted_client = httpx.Client(
        base_url=host,
        transport=WorkerResponseTransport(
            host=host,
            tool_modules={},
            request=lambda: workspace._request_contract,
            budget=workspace._response_budget,
            transport=Observed(),
        ),
    )
    return WorkerBranch(workspace, identity), paths


def _snapshot_ids(agent_server: httpx.Client, identity: uuid.UUID) -> list[str]:
    snapshot = agent_server.get(f"/api/conversations/{identity}/events/snapshot").json()
    return [event["id"] for event in snapshot["events"]]


def test_pinned_agent_server_branch_extends_by_its_parent_links(
    agent_server: httpx.Client, tmp_path: Path
) -> None:
    request = StartConversationRequest(
        agent=Agent(llm=TestLLM(model="test-model"), tools=[]),
        workspace=LocalWorkspace(working_dir=tmp_path / "worker-workspace"),
        autotitle=False,
    )
    identity = _create(agent_server, request)

    def send(text: str) -> None:
        response = agent_server.post(
            f"/api/conversations/{identity}/events",
            json={"role": "user", "content": [{"type": "text", "text": text}], "run": False},
        )
        assert response.status_code == 200

    send("First synthetic message.")
    branch, paths = _admitted_branch(agent_server, request, identity)
    first = branch.read().events
    assert first
    assert paths == ["snapshot"]
    for index in range(3):
        send(f"Synthetic message {index}.")
    later = branch.read().events
    assert paths == ["snapshot", "status", "search"]
    assert later[: len(first)] == first
    assert len(later) == len(first) + 3
    assert [event.id for event in later] == _snapshot_ids(agent_server, identity)


def _tool_call(call_id: str, name: str, arguments: dict[str, str]) -> MessageToolCall:
    return MessageToolCall(
        id=call_id, name=name, arguments=json.dumps(arguments), origin="completion"
    )


def test_pinned_agent_server_confirmation_cycle_is_read_incrementally(
    agent_server: httpx.Client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The server rebuilds the agent's model from its request, so its completions are scripted
    # where every model of this test process answers.
    scripted = TestLLM.from_messages(
        [
            Message(
                role="assistant",
                content=[TextContent(text="Two steps.")],
                tool_calls=[
                    _tool_call("think-1", "think", {"thought": "First."}),
                    _tool_call("think-2", "think", {"thought": "Second."}),
                ],
            ),
            Message(
                role="assistant",
                content=[TextContent(text="")],
                tool_calls=[_tool_call("finish-1", "finish", {"message": "Done."})],
            ),
        ]
    )

    monkeypatch.setattr(LLM, "acompletion", staticmethod(scripted.acompletion))
    monkeypatch.setattr(LLM, "completion", staticmethod(scripted.completion))
    request = StartConversationRequest(
        agent=Agent(
            llm=TestLLM(model="test-model"),
            tools=[],
            include_default_tools=["FinishTool", "ThinkTool"],
        ),
        workspace=LocalWorkspace(working_dir=tmp_path / "worker-workspace"),
        confirmation_policy=AlwaysConfirm(),
        autotitle=False,
    )
    identity = _create(agent_server, request)
    sent = agent_server.post(
        f"/api/conversations/{identity}/events",
        json={"role": "user", "content": [{"type": "text", "text": "Begin."}], "run": False},
    )
    assert sent.status_code == 200
    branch, paths = _admitted_branch(agent_server, request, identity)
    first = branch.read()
    assert first.events
    assert paths == ["snapshot"]

    def settles(status: ConversationExecutionStatus) -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            reported = agent_server.get(f"/api/conversations/{identity}").json()
            if reported["execution_status"] == status.value:
                return
            time.sleep(0.02)
        pytest.fail(f"agent server did not reach {status.value}")

    assert agent_server.post(f"/api/conversations/{identity}/run").status_code == 200
    settles(ConversationExecutionStatus.WAITING_FOR_CONFIRMATION)
    waiting = branch.read()
    assert waiting.execution_status == ConversationExecutionStatus.WAITING_FOR_CONFIRMATION
    assert waiting.events[: len(first.events)] == first.events
    assert [event.tool_call_id for event in waiting.events if isinstance(event, ActionEvent)] == [
        "think-1",
        "think-2",
    ]
    assert [event.id for event in waiting.events] == _snapshot_ids(agent_server, identity)

    accepted = agent_server.post(
        f"/api/conversations/{identity}/events/respond_to_confirmation", json={"accept": True}
    )
    assert accepted.status_code == 200
    settles(ConversationExecutionStatus.FINISHED)
    finished = branch.read()
    assert finished.execution_status == ConversationExecutionStatus.FINISHED
    assert finished.events[: len(waiting.events)] == waiting.events
    assert sum(isinstance(event, ObservationEvent) for event in finished.events) == 3
    snapshot = agent_server.get(f"/api/conversations/{identity}/events/snapshot").json()
    assert [event.id for event in finished.events] == [event["id"] for event in snapshot["events"]]
    assert finished.stats == ConversationStats.model_validate(snapshot["stats"])
    # Every read after the first is the status, then the history after the leaf.
    assert paths == ["snapshot", "status", "search", "status", "search"]


def test_pinned_agent_server_approved_action_is_not_read_as_pending_during_its_step(
    agent_server: httpx.Client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scripted = TestLLM.from_messages(
        [
            Message(
                role="assistant",
                content=[TextContent(text="Two steps.")],
                tool_calls=[
                    _tool_call("think-1", "think", {"thought": "Slow."}),
                    _tool_call("think-2", "think", {"thought": "Second."}),
                ],
            ),
            Message(
                role="assistant",
                content=[TextContent(text="")],
                tool_calls=[_tool_call("finish-1", "finish", {"message": "Done."})],
            ),
        ]
    )
    monkeypatch.setattr(LLM, "acompletion", staticmethod(scripted.acompletion))
    monkeypatch.setattr(LLM, "completion", staticmethod(scripted.completion))
    executing, release = Event(), Event()
    think = ThinkExecutor.__call__

    def slow_think(
        self: ThinkExecutor,
        action: ThinkAction,
        conversation: object = None,  # noqa: ARG001
    ) -> ThinkObservation:
        executing.set()
        assert release.wait(10)
        return think(self, action)

    monkeypatch.setattr(ThinkExecutor, "__call__", slow_think)
    request = StartConversationRequest(
        agent=Agent(
            llm=TestLLM(model="test-model"),
            tools=[],
            include_default_tools=["FinishTool", "ThinkTool"],
        ),
        workspace=LocalWorkspace(working_dir=tmp_path / "worker-workspace"),
        confirmation_policy=AlwaysConfirm(),
        autotitle=False,
    )
    identity = _create(agent_server, request)
    sent = agent_server.post(
        f"/api/conversations/{identity}/events",
        json={"role": "user", "content": [{"type": "text", "text": "Begin."}], "run": False},
    )
    assert sent.status_code == 200
    branch, _paths = _admitted_branch(agent_server, request, identity)
    branch.read()

    def reported() -> str:
        return str(agent_server.get(f"/api/conversations/{identity}").json()["execution_status"])

    def settles(status: ConversationExecutionStatus) -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if reported() == status.value:
                return
            time.sleep(0.02)
        pytest.fail(f"agent server did not reach {status.value}")

    assert agent_server.post(f"/api/conversations/{identity}/run").status_code == 200
    settles(ConversationExecutionStatus.WAITING_FOR_CONFIRMATION)
    waiting = branch.read()
    assert waiting.execution_status == ConversationExecutionStatus.WAITING_FOR_CONFIRMATION
    assert ConversationState.get_unmatched_actions(waiting.events)
    # Running a conversation that waits for confirmation runs its pending actions, as the
    # gateway does after an approval.
    try:
        assert agent_server.post(f"/api/conversations/{identity}/run").status_code == 200
        assert executing.wait(10)
        # The status the worker reports is still the one it saved before the step.
        assert reported() == ConversationExecutionStatus.WAITING_FOR_CONFIRMATION.value
        during = branch.read()
        assert during.execution_status == ConversationExecutionStatus.RUNNING
        assert ConversationState.get_unmatched_actions(during.events)
    finally:
        release.set()
    settles(ConversationExecutionStatus.FINISHED)
    finished = branch.read()
    assert finished.execution_status == ConversationExecutionStatus.FINISHED
    assert not ConversationState.get_unmatched_actions(finished.events)
