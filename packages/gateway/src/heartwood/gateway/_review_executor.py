# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Scope native concurrency to gateway-authorized, tool-free advisory Task batches."""

import asyncio
from collections.abc import Callable, Sequence
from typing import Any, override

from openhands.sdk import Agent
from openhands.sdk.agent.parallel_executor import ParallelToolExecutor
from openhands.sdk.conversation.cancellation import CancellationToken
from openhands.sdk.conversation.resource_lock_manager import ResourceLockManager
from openhands.sdk.event import ActionEvent, Event
from openhands.sdk.tool import ToolDefinition
from openhands.tools.task import TaskAction, TaskTool

from heartwood.gateway._specialist_task import supports_parallel_review
from heartwood.schemas.parallel_reviews import ReviewExecutionPlan, parse_review_execution_plan

type ReviewBatchAuthorizer = Callable[[Sequence[ActionEvent]], ReviewExecutionPlan | None]


def bind_review_executor(agent: Agent, authorize: ReviewBatchAuthorizer) -> None:
    """Adapt the pinned SDK executor slot without changing its agent loop or global limit.

    The authorizer must recheck current qualification and journal the exact approved
    action identities before returning a plan. None retains sequential execution;
    an exception denies dispatch. It is a gateway dependency, never model input.
    """
    if agent.tool_concurrency_limit != 1:
        raise ValueError("Advisory concurrency requires a sequential parent agent")
    agent._parallel_executor = _ReviewBatchExecutor(authorize)


def scoped_review_execution_enabled(agent: object) -> bool:
    """Observe the installed native adapter, not a requested worker count or eligibility flag."""
    return isinstance(agent, Agent) and isinstance(agent._parallel_executor, _ReviewBatchExecutor)


class _ReviewBatchExecutor(ParallelToolExecutor):
    def __init__(self, authorize: ReviewBatchAuthorizer) -> None:
        self._review_locks = ResourceLockManager()
        super().__init__(max_workers=1, lock_manager=self._review_locks)
        self._authorize_review = authorize

    def _parallel_executor(
        self,
        actions: Sequence[ActionEvent],
        tools: dict[str, ToolDefinition[Any, Any]] | None,
        cancel_token: CancellationToken | None,
    ) -> ParallelToolExecutor | None:
        if len(actions) < 2 or (cancel_token is not None and cancel_token.is_cancelled):
            return None
        if not tools or any(
            not isinstance(action.action, TaskAction)
            or action.tool_name not in tools
            or not isinstance(tools[action.tool_name], TaskTool)
            or not supports_parallel_review(tools[action.tool_name])
            or action.action.resume is not None
            for action in actions
        ):
            return None
        plan = self._authorize_review(actions)
        if plan is None:
            return None
        plan = parse_review_execution_plan(plan.model_dump())
        roles = [
            action.action.subagent_type
            for action in actions
            if isinstance(action.action, TaskAction)
        ]
        if (
            len(roles) != len(plan.scope.reviewer_ids)
            or set(roles) != set(plan.scope.reviewer_ids)
            or len({action.id for action in actions}) != len(actions)
            or len({action.tool_call_id for action in actions}) != len(actions)
        ):
            raise ValueError("Approved Task batch differs from the authorized advisory review")
        return ParallelToolExecutor(max_workers=plan.scope.workers, lock_manager=self._review_locks)

    @override
    def execute_batch(
        self,
        action_events: Sequence[ActionEvent],
        tool_runner: Callable[[ActionEvent], list[Event]],
        tools: dict[str, ToolDefinition[Any, Any]] | None = None,
        cancel_token: CancellationToken | None = None,
        span_owner: object | None = None,
    ) -> list[list[Event]]:
        executor = self._parallel_executor(action_events, tools, cancel_token)
        execute = executor.execute_batch if executor is not None else super().execute_batch
        return execute(action_events, tool_runner, tools, cancel_token, span_owner)

    @override
    async def aexecute_batch(
        self,
        action_events: Sequence[ActionEvent],
        tool_runner: Callable[[ActionEvent], list[Event]],
        tools: dict[str, ToolDefinition[Any, Any]] | None = None,
        cancel_token: CancellationToken | None = None,
        span_owner: object | None = None,
    ) -> list[list[Event]]:
        # Admission may read shared storage; keep cancellation responsive while it waits.
        executor = await asyncio.to_thread(
            self._parallel_executor, action_events, tools, cancel_token
        )
        execute = executor.aexecute_batch if executor is not None else super().aexecute_batch
        return await execute(action_events, tool_runner, tools, cancel_token, span_owner)
