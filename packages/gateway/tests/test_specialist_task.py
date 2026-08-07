# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Tests for the catalog-scoped OpenHands specialist Task adapter."""

from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from typing import cast
from unittest.mock import Mock
from uuid import uuid4

import pytest
from openhands.sdk import Agent, LocalConversation
from openhands.tools.task import TaskAction

import heartwood.gateway._specialist_task as specialist_task_module
from heartwood.gateway._openhands_persistence import ContentMinimizedLocalFileStore
from heartwood.gateway._specialist_task import (
    _CatalogTaskExecutor,
    _CatalogTaskManager,
)


def test_catalog_task_manager_rejects_unknown_agents_before_registry_lookup() -> None:
    manager = _CatalogTaskManager(allowed_specialist_ids=frozenset({"research-planner"}))

    with pytest.raises(
        ValueError,
        match="requested specialist is not available",
    ):
        manager.start_task(
            prompt="Run an unapproved task.",
            subagent_type="foreign-agent",
        )


def test_catalog_task_manager_rejects_non_durable_resume() -> None:
    manager = _CatalogTaskManager(allowed_specialist_ids=frozenset({"research-planner"}))

    with pytest.raises(ValueError, match="does not restore task lineage"):
        manager.start_task(
            prompt="Review the revision.",
            subagent_type="research-planner",
            resume="task_00000001",
        )


def test_task_executor_propagates_interrupt_to_the_active_child() -> None:
    manager = _CatalogTaskManager(allowed_specialist_ids=frozenset({"research-planner"}))
    child = Mock(spec=LocalConversation)
    manager._active_child = cast(LocalConversation, child)
    executor = _CatalogTaskExecutor(manager)

    executor.interrupt()

    child.interrupt.assert_called_once_with()


def test_catalog_task_manager_preserves_delegate_observability_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _CatalogTaskManager(allowed_specialist_ids=frozenset({"research-planner"}))
    parent = Mock(spec=LocalConversation)
    parent.state.persistence_dir = tmp_path / "openhands"
    parent.state.workspace.working_dir = tmp_path / "project"
    parent.state.id = uuid4()
    manager._parent_conversation = cast(LocalConversation, parent)

    link = {
        "delegate.parent_trace_id": "trace-123",
        "delegate.parent_span_id": "span-456",
        "tool_call_id": "call-789",
    }
    monkeypatch.setattr(
        specialist_task_module,
        "detached_delegate_context",
        lambda: nullcontext(link),
    )
    conversation_type = Mock()
    conversation_type.get_persistence_dir.return_value = str(tmp_path / "subagent")
    child = Mock(spec=LocalConversation)
    conversation_type.return_value = child
    monkeypatch.setattr(specialist_task_module, "LocalConversation", conversation_type)

    created = manager._get_conversation(
        description="Review the analysis plan",
        max_iteration_per_run=12,
        task_id="task-001",
        subagent_type="research-planner",
        conversation_id=uuid4(),
        worker_agent=cast(Agent, Mock()),
    )

    assert created is child
    options = conversation_type.call_args.kwargs
    assert options["observability_metadata"] == {
        "is_delegate": True,
        "task_id": "task-001",
        "subagent_type": "research-planner",
        "parent_session_id": str(parent.state.id),
        **link,
    }
    assert options["observability_tags"] == ["delegate"]
    assert isinstance(options["file_store"], ContentMinimizedLocalFileStore)


def test_task_action_resume_remains_typed_for_fail_closed_validation() -> None:
    action = TaskAction(
        description="Review revision",
        prompt="Review the supplied revision.",
        subagent_type="research-planner",
        resume="task_00000001",
    )

    assert action.resume == "task_00000001"
