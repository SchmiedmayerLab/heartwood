# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Tests for the catalog-scoped OpenHands specialist Task adapter."""

from __future__ import annotations

from typing import cast
from unittest.mock import Mock

import pytest
from openhands.sdk import LocalConversation
from openhands.tools.task import TaskAction

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


def test_task_action_resume_remains_typed_for_fail_closed_validation() -> None:
    action = TaskAction(
        description="Review revision",
        prompt="Review the supplied revision.",
        subagent_type="research-planner",
        resume="task_00000001",
    )

    assert action.resume == "task_00000001"
