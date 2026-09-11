# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Catalog-scoped OpenHands Task tool for bounded research specialists."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any, TypedDict, override

from openhands.sdk import Agent, ImageContent, LocalConversation, TextContent, Tool
from openhands.sdk.context import AgentContext
from openhands.sdk.conversation.cancellation import CancellationToken
from openhands.sdk.conversation.state import ConversationState
from openhands.sdk.hooks.config import HookConfig
from openhands.sdk.observability.laminar import detached_delegate_context
from openhands.sdk.tool import ToolDefinition, register_tool
from openhands.sdk.tool.builtins.finish import FinishTool
from openhands.tools.task import TaskAction, TaskObservation, TaskTool
from openhands.tools.task.impl import TaskExecutor
from openhands.tools.task.manager import (
    ConfirmationHandler,
    Task,
    TaskManager,
    TaskStatus,
)
from pydantic import Field

from heartwood.core_adapter.research_review import research_review_instructions
from heartwood.gateway._openhands_persistence import ContentMinimizedLocalFileStore
from heartwood.schemas.experiments import ExperimentRecord
from heartwood.schemas.review import ReviewProposals


class _ReviewTaskResult(ExperimentRecord):
    """Typed envelope over the native Task result string, never parsed from model prose."""

    message: str = Field(max_length=16_384)
    proposals: ReviewProposals


class HeartwoodSpecialistObservation(TaskObservation):
    """Native task lineage and message plus optional advisory review proposals."""

    review_proposals: ReviewProposals | None = None

    @property
    @override
    def to_llm_content(self) -> Sequence[TextContent | ImageContent]:
        content = list(super().to_llm_content)
        if self.review_proposals is not None:
            content.append(
                TextContent(
                    text=(
                        "Unverified review proposals. Independent checks and normal "
                        "action approval still apply.\n" + self.review_proposals.model_dump_json()
                    )
                )
            )
        return content


class SpecialistToolRole(TypedDict):
    """Minimal catalog metadata needed to describe one executable specialist."""

    specialist_id: str
    label: str
    description: str


class _InterruptibleSpecialistConversation(LocalConversation):
    """Use native cancellable I/O inside the Task manager's blocking worker contract."""

    _parent_cancel_token: CancellationToken | None = None

    @override
    def run(self) -> None:  # type: ignore[override]  # Upstream tracing types this method as Never.
        asyncio.run(self.arun())

    @override
    async def arun(self) -> None:  # type: ignore[override]  # Upstream tracing types this as Never.
        # Publish the native task before checking cancellation so an interrupt racing
        # with startup targets this task, not the idle child's resumable pause state.
        self._arun_task = asyncio.current_task()
        try:
            if self._parent_cancel_token is not None and self._parent_cancel_token.is_cancelled:
                self.pause()
                return
            await super().arun()
        except asyncio.CancelledError:
            # Native arun's cancellation handler starts after lazy initialization.
            self.pause()
        finally:
            self._arun_task = None


class _CatalogTaskManager(TaskManager):
    """Reuse OpenHands task orchestration behind a strict Heartwood allowlist."""

    def __init__(
        self,
        *,
        allowed_specialist_ids: frozenset[str],
        confirmation_handler: ConfirmationHandler | None = None,
        structured_reviews: bool = False,
    ) -> None:
        super().__init__(confirmation_handler=confirmation_handler)
        self._allowed_specialist_ids = allowed_specialist_ids
        self._structured_reviews = structured_reviews

    @override
    def _generate_ids(self) -> tuple[str, uuid.UUID]:
        """Keep task lineage and cumulative usage distinct across manager restarts."""
        _, conversation_id = super()._generate_ids()
        return f"task_{conversation_id.hex}", conversation_id

    @override
    def start_task(
        self,
        prompt: str,
        subagent_type: str = "default",
        resume: str | None = None,
        description: str | None = None,
        conversation: LocalConversation | None = None,
    ) -> Task:
        if subagent_type not in self._allowed_specialist_ids:
            raise ValueError("The requested specialist is not available in this deployment.")
        if resume is not None:
            raise ValueError(
                "Specialist task resume is unavailable because OpenHands does not restore "
                "task lineage across process restarts. Start a new specialist review instead."
            )
        return super().start_task(
            prompt=prompt,
            subagent_type=subagent_type,
            resume=None,
            description=description,
            conversation=conversation,
        )

    @override
    def _get_conversation(
        self,
        description: str | None,
        max_iteration_per_run: int,
        task_id: str,
        subagent_type: str,
        conversation_id: uuid.UUID,
        worker_agent: Agent,
        hook_config: HookConfig | None = None,
        max_budget_per_run: float | None = None,
    ) -> LocalConversation:
        parent = self.parent_conversation
        if self._structured_reviews:
            context = worker_agent.agent_context or AgentContext()
            worker_agent = worker_agent.model_copy(
                update={
                    "agent_context": context.model_copy(
                        update={
                            "system_message_suffix": "\n\n".join(
                                filter(
                                    None,
                                    (
                                        context.system_message_suffix,
                                        research_review_instructions(),
                                    ),
                                )
                            ),
                        }
                    ),
                    "include_default_tools": [
                        name for name in worker_agent.include_default_tools if name != "FinishTool"
                    ],
                    "tools": [
                        *[tool for tool in worker_agent.tools if tool.name != "FinishTool"],
                        Tool(name="FinishTool", params={"response_schema": ReviewProposals}),
                    ],
                }
            )
        parent_persistence_dir = parent.state.persistence_dir
        if parent_persistence_dir is None:
            raise RuntimeError("Specialist persistence is unavailable.")
        persistence_dir = Path(parent_persistence_dir) / "subagents"
        persistence_dir.mkdir(parents=True, exist_ok=True)
        file_store = ContentMinimizedLocalFileStore(
            LocalConversation.get_persistence_dir(persistence_dir, conversation_id),
            cache_limit_size=max_iteration_per_run,
        )
        with detached_delegate_context() as link:
            conversation = _InterruptibleSpecialistConversation(
                agent=worker_agent,
                workspace=parent.state.workspace.working_dir,
                persistence_dir=persistence_dir,
                conversation_id=conversation_id,
                max_iteration_per_run=max_iteration_per_run,
                max_budget_per_run=max_budget_per_run,
                hook_config=hook_config,
                delete_on_close=True,
                prompt_cache_key=str(parent.state.id),
                file_store=file_store,
                profile_store_dir=Path(file_store.root) / "profiles",
                visualizer=None,
                observability_metadata=self._delegate_observability_metadata(
                    task_id=task_id,
                    subagent_type=subagent_type,
                    link=link,
                ),
                observability_tags=["delegate"],
            )
            conversation._parent_cancel_token = parent.cancel_token
            return conversation

    @override
    def _run_task(self, task: Task, prompt: str) -> Task:
        token = self.parent_conversation.cancel_token
        if token is not None and token.is_cancelled:
            task.set_error("The parent cancelled the specialist before execution.")
            self._evict_task(task)
            return task
        return super()._run_task(task, prompt)

    def interrupt_active_children(self) -> None:
        """Interrupt native running tasks without maintaining another active-task registry."""
        with self._tasks_lock:
            children = tuple(
                task.conversation
                for task in self._tasks.values()
                if task.status == TaskStatus.RUNNING and task.conversation is not None
            )
        errors: list[Exception] = []
        for child in children:
            try:
                child.interrupt()
            except Exception as error:
                errors.append(error)
        if errors:
            raise ExceptionGroup("Specialist interruption failed", errors)

    @override
    def _evict_task(self, task: Task) -> None:
        # Native eviction closes the child and may remove its files. Capture the
        # public structured response first; the parent observation persists it.
        if self._structured_reviews and task.status == TaskStatus.COMPLETED:
            try:
                if task.conversation is None:
                    raise ValueError("Review conversation is unavailable")
                parser = FinishTool.create()[0].set_response_schema(ReviewProposals)
                proposals = parser.parse_last_response(list(task.conversation.state.events))
                if not isinstance(proposals, ReviewProposals):
                    raise ValueError("Structured review proposals are unavailable")
                task.set_result(
                    _ReviewTaskResult(
                        message=task.result or "", proposals=proposals
                    ).model_dump_json()
                )
            except (ValueError, OSError):
                task.set_error("The specialist did not return a valid structured review.")
        super()._evict_task(task)


class _CatalogTaskExecutor(TaskExecutor):
    """Add child interruption to OpenHands' blocking Task executor."""

    def __init__(self, manager: _CatalogTaskManager) -> None:
        super().__init__(manager=manager)
        self._catalog_manager = manager

    @override
    def interrupt(self) -> None:
        self._catalog_manager.interrupt_active_children()

    @override
    def __call__(
        self, action: TaskAction, conversation: LocalConversation | None = None
    ) -> TaskObservation:
        observation = super().__call__(action, conversation)
        proposals = None
        text = observation.text
        failed = observation.is_error
        if self._catalog_manager._structured_reviews and not failed:
            try:
                result = _ReviewTaskResult.model_validate_json(text)
                text, proposals = result.message, result.proposals
            except ValueError:
                text, failed = "The specialist did not return a valid structured review.", True
        return HeartwoodSpecialistObservation.from_text(
            text=text,
            task_id=observation.task_id,
            subagent=observation.subagent,
            status="error" if failed else observation.status,
            is_error=failed,
            review_proposals=proposals,
        )


def supports_parallel_review(tool: ToolDefinition[Any, Any]) -> bool:
    """Only the catalog's structured advisory executor may use scoped concurrency."""
    return (
        isinstance(tool.executor, _CatalogTaskExecutor)
        and tool.executor._catalog_manager._structured_reviews
    )


class HeartwoodSpecialistToolSet(ToolDefinition[TaskAction, TaskObservation]):
    """Create one OpenHands Task tool restricted to catalog specialists."""

    @classmethod
    def create(  # type: ignore[override]
        cls,
        conv_state: ConversationState,  # noqa: ARG003
        specialists: list[SpecialistToolRole],
        confirmation_handler: ConfirmationHandler | None = None,
        structured_reviews: bool = False,
    ) -> Sequence[ToolDefinition[TaskAction, TaskObservation]]:
        normalized = _validated_roles(specialists)
        manager = _CatalogTaskManager(
            allowed_specialist_ids=frozenset(role["specialist_id"] for role in normalized),
            confirmation_handler=confirmation_handler,
            structured_reviews=structured_reviews,
        )
        executor = _CatalogTaskExecutor(manager)
        return [
            tool.model_copy(update={"observation_type": HeartwoodSpecialistObservation})
            for tool in TaskTool.create(
                executor=executor, description=_task_description(normalized)
            )
        ]


def _validated_roles(roles: list[SpecialistToolRole]) -> tuple[SpecialistToolRole, ...]:
    if not roles:
        raise ValueError("At least one available specialist is required.")
    normalized: list[SpecialistToolRole] = []
    identifiers: set[str] = set()
    for role in roles:
        specialist_id = role["specialist_id"].strip()
        label = role["label"].strip()
        description = role["description"].strip()
        if not specialist_id or not label or not description:
            raise ValueError("Specialist tool metadata must be complete.")
        if specialist_id in identifiers:
            raise ValueError("Specialist tool identifiers must be unique.")
        identifiers.add(specialist_id)
        normalized.append(
            {
                "specialist_id": specialist_id,
                "label": label,
                "description": description,
            }
        )
    return tuple(normalized)


def _task_description(roles: tuple[SpecialistToolRole, ...]) -> str:
    available = "\n".join(
        f"- `{role['specialist_id']}` ({role['label']}): {role['description']}" for role in roles
    )
    return f"""Request one bounded, tool-free review from an approved research specialist.

Available specialist types:
{available}

Use this tool only when a focused second pass improves the research task. Include the exact
question, supplied evidence, assumptions, and expected review output in `prompt`. Specialists
cannot inspect files, run tools, access the network, or modify the project. Do not use `resume`;
start a new sequential review when follow-up analysis is needed.
"""


register_tool(HeartwoodSpecialistToolSet.name, HeartwoodSpecialistToolSet)


__all__ = ["HeartwoodSpecialistToolSet", "SpecialistToolRole"]
