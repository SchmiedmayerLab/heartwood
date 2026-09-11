# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Real native scheduling stays sequential outside a scoped advisory batch."""

import asyncio
import json
from collections.abc import Sequence
from threading import Barrier, Lock
from threading import Event as ThreadEvent
from typing import Any

import pytest
from openhands.sdk import Agent
from openhands.sdk.conversation.cancellation import CancellationToken
from openhands.sdk.event import ActionEvent, Event
from openhands.sdk.llm import MessageToolCall
from openhands.sdk.testing import TestLLM
from openhands.sdk.tool import ToolDefinition
from openhands.tools.task import TaskAction, TaskTool
from openhands.tools.terminal import TerminalAction

from heartwood.gateway._review_executor import _ReviewBatchExecutor, bind_review_executor
from heartwood.gateway._specialist_task import _CatalogTaskExecutor, _CatalogTaskManager
from heartwood.schemas.parallel_reviews import ParallelReviewPlan


def _actions() -> list[ActionEvent]:
    return [
        ActionEvent(
            id=f"action-{index}",
            thought=[],
            action=TaskAction(prompt="Review supplied synthetic evidence.", subagent_type=role),
            tool_name="task",
            tool_call_id=f"call-{index}",
            tool_call=MessageToolCall(
                id=f"call-{index}",
                name="task",
                arguments=json.dumps(
                    {"prompt": "Review supplied synthetic evidence.", "subagent_type": role}
                ),
                origin="completion",
            ),
            llm_response_id="response-1",
        )
        for index, role in enumerate(("data-quality-reviewer", "statistical-reviewer"))
    ]


def _tools(*, structured: bool = True) -> dict[str, ToolDefinition[Any, Any]]:
    executor = _CatalogTaskExecutor(
        _CatalogTaskManager(
            allowed_specialist_ids=frozenset({"data-quality-reviewer", "statistical-reviewer"}),
            structured_reviews=structured,
        )
    )
    return {
        tool.name: tool
        for tool in TaskTool.create(executor=executor, description="Review synthetic evidence")
    }


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("qualified", [False, True])
def test_only_qualified_review_batches_overlap_and_keep_result_order(
    parallel_review_plan: ParallelReviewPlan, asynchronous: bool, qualified: bool
) -> None:
    active = 0
    peak = 0
    lock = Lock()
    barrier = Barrier(2 if qualified else 1)
    authorizations: list[tuple[str, ...]] = []

    def authorize(actions: Sequence[ActionEvent]) -> ParallelReviewPlan | None:
        authorizations.append(tuple(action.id for action in actions))
        return parallel_review_plan if qualified else None

    def run(action: ActionEvent) -> list[Event]:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            barrier.wait(timeout=5)
            return [action]
        finally:
            with lock:
                active -= 1

    executor = _ReviewBatchExecutor(authorize)
    actions = _actions()
    result = (
        asyncio.run(executor.aexecute_batch(actions, run, _tools()))
        if asynchronous
        else executor.execute_batch(actions, run, _tools())
    )
    assert result == [[action] for action in actions]
    assert authorizations == [("action-0", "action-1")]
    assert peak == (2 if qualified else 1)
    assert active == 0


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(
    "kind", ["mixed", "resume", "ordinary-task", "unknown-tool", "single", "empty"]
)
def test_other_tools_never_consume_parallel_authorization(asynchronous: bool, kind: str) -> None:
    actions = _actions()
    tools = _tools(structured=kind != "ordinary-task")
    if kind == "mixed":
        actions[1] = actions[1].model_copy(
            update={"action": TerminalAction(command="true"), "tool_name": "terminal"}
        )
    elif kind == "resume":
        assert isinstance(actions[0].action, TaskAction)
        actions[0] = actions[0].model_copy(
            update={"action": actions[0].action.model_copy(update={"resume": "task-previous"})}
        )
    elif kind == "unknown-tool":
        tools = {}
    elif kind == "single":
        actions = actions[:1]
    elif kind == "empty":
        actions = []
    observed: list[str] = []

    def authorize(actions: Sequence[ActionEvent]) -> ParallelReviewPlan | None:  # noqa: ARG001
        pytest.fail("Non-advisory work requested parallel authorization")

    def run(action: ActionEvent) -> list[Event]:
        observed.append(action.id)
        return []

    executor = _ReviewBatchExecutor(authorize)
    if asynchronous:
        asyncio.run(executor.aexecute_batch(actions, run, tools))
    else:
        executor.execute_batch(actions, run, tools)
    assert observed == [action.id for action in actions]


@pytest.mark.parametrize("damage", ["role", "duplicate-role", "duplicate-id", "duplicate-call"])
def test_authorized_scope_cannot_be_substituted(
    parallel_review_plan: ParallelReviewPlan, damage: str
) -> None:
    actions = _actions()
    first = actions[0]
    assert isinstance(first.action, TaskAction)
    if damage in {"role", "duplicate-role"}:
        actions[0] = first.model_copy(
            update={
                "action": first.action.model_copy(
                    update={
                        "subagent_type": "unapproved-reviewer"
                        if damage == "role"
                        else "statistical-reviewer"
                    }
                )
            }
        )
    else:
        field = "id" if damage == "duplicate-id" else "tool_call_id"
        actions[0] = first.model_copy(update={field: getattr(actions[1], field)})
    calls: list[str] = []

    def run(action: ActionEvent) -> list[Event]:
        calls.append(action.id)
        return []

    with pytest.raises(ValueError, match="differs from the authorized"):
        _ReviewBatchExecutor(lambda _: parallel_review_plan).execute_batch(actions, run, _tools())
    assert calls == []


def test_revocation_denies_dispatch_before_any_tool_runs() -> None:
    def authorize(actions: Sequence[ActionEvent]) -> ParallelReviewPlan | None:  # noqa: ARG001
        raise ValueError("Review authorization expired")

    def run(action: ActionEvent) -> list[Event]:  # noqa: ARG001
        pytest.fail("Expired review executed a tool")

    with pytest.raises(ValueError, match="expired"):
        _ReviewBatchExecutor(authorize).execute_batch(_actions(), run, _tools())


def test_cancelled_batch_neither_authorizes_nor_executes() -> None:
    token = CancellationToken()
    token.cancel()

    def authorize(actions: Sequence[ActionEvent]) -> ParallelReviewPlan | None:  # noqa: ARG001
        pytest.fail("Cancelled review requested authorization")

    def run(action: ActionEvent) -> list[Event]:  # noqa: ARG001
        pytest.fail("Cancelled review executed a tool")

    results = _ReviewBatchExecutor(authorize).execute_batch(_actions(), run, _tools(), token)
    assert len(results) == 2
    assert all(results)


def test_scoped_executor_cannot_enable_global_tool_concurrency() -> None:
    agent = Agent(llm=TestLLM.from_messages([]), tools=[], tool_concurrency_limit=2)
    with pytest.raises(ValueError, match="sequential parent"):
        bind_review_executor(agent, lambda _: None)


def test_cancellation_during_slow_authorization_never_dispatches(
    parallel_review_plan: ParallelReviewPlan,
) -> None:
    started = ThreadEvent()
    release = ThreadEvent()
    finished = ThreadEvent()

    def authorize(actions: Sequence[ActionEvent]) -> ParallelReviewPlan:  # noqa: ARG001
        started.set()
        try:
            assert release.wait(5)
            return parallel_review_plan
        finally:
            finished.set()

    def run(action: ActionEvent) -> list[Event]:  # noqa: ARG001
        pytest.fail("Cancelled admission dispatched a tool")

    async def cancel() -> None:
        task = asyncio.create_task(
            _ReviewBatchExecutor(authorize).aexecute_batch(_actions(), run, _tools())
        )
        try:
            assert await asyncio.to_thread(started.wait, 2)
            assert task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()
        assert await asyncio.to_thread(finished.wait, 2)

    asyncio.run(cancel())
