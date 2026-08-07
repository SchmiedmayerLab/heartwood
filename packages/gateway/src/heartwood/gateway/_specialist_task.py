# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Catalog-scoped OpenHands Task tool for bounded research specialists."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from pathlib import Path
from threading import RLock
from typing import TypedDict, override

from openhands.sdk import Agent, LocalConversation
from openhands.sdk.conversation.state import ConversationState
from openhands.sdk.hooks.config import HookConfig
from openhands.sdk.observability.laminar import detached_delegate_context
from openhands.sdk.tool import ToolDefinition, register_tool
from openhands.tools.task import TaskAction, TaskObservation, TaskTool
from openhands.tools.task.impl import TaskExecutor
from openhands.tools.task.manager import (
    ConfirmationHandler,
    Task,
    TaskManager,
)

from heartwood.gateway._openhands_persistence import ContentMinimizedLocalFileStore


class SpecialistToolRole(TypedDict):
    """Minimal catalog metadata needed to describe one executable specialist."""

    specialist_id: str
    label: str
    description: str


class _CatalogTaskManager(TaskManager):
    """Reuse OpenHands task orchestration behind a strict Heartwood allowlist."""

    def __init__(
        self,
        *,
        allowed_specialist_ids: frozenset[str],
        confirmation_handler: ConfirmationHandler | None = None,
    ) -> None:
        super().__init__(confirmation_handler=confirmation_handler)
        self._allowed_specialist_ids = allowed_specialist_ids
        self._active_child: LocalConversation | None = None
        self._active_child_lock = RLock()

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
            return LocalConversation(
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
                visualizer=None,
                observability_metadata=self._delegate_observability_metadata(
                    task_id=task_id,
                    subagent_type=subagent_type,
                    link=link,
                ),
                observability_tags=["delegate"],
            )

    @override
    def _run_task(self, task: Task, prompt: str) -> Task:
        child = task.conversation
        with self._active_child_lock:
            self._active_child = child
        try:
            return super()._run_task(task, prompt)
        finally:
            with self._active_child_lock:
                if self._active_child is child:
                    self._active_child = None

    def interrupt_active_child(self) -> None:
        """Propagate parent interruption to the currently running child."""
        with self._active_child_lock:
            child = self._active_child
        if child is not None:
            child.interrupt()


class _CatalogTaskExecutor(TaskExecutor):
    """Add child interruption to OpenHands' blocking Task executor."""

    def __init__(self, manager: _CatalogTaskManager) -> None:
        super().__init__(manager=manager)
        self._catalog_manager = manager

    @override
    def interrupt(self) -> None:
        self._catalog_manager.interrupt_active_child()


class HeartwoodSpecialistToolSet(ToolDefinition[TaskAction, TaskObservation]):
    """Create one OpenHands Task tool restricted to catalog specialists."""

    @classmethod
    def create(  # type: ignore[override]
        cls,
        conv_state: ConversationState,  # noqa: ARG003
        specialists: list[SpecialistToolRole],
        confirmation_handler: ConfirmationHandler | None = None,
    ) -> Sequence[ToolDefinition[TaskAction, TaskObservation]]:
        normalized = _validated_roles(specialists)
        manager = _CatalogTaskManager(
            allowed_specialist_ids=frozenset(role["specialist_id"] for role in normalized),
            confirmation_handler=confirmation_handler,
        )
        executor = _CatalogTaskExecutor(manager)
        return TaskTool.create(
            executor=executor,
            description=_task_description(normalized),
        )


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
