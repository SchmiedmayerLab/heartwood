# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Translate native Task batches into the existing workflow admission journal."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from openhands.sdk import LocalConversation
from openhands.sdk.event import ActionEvent
from openhands.tools.task import TaskAction

from heartwood.core_adapter import SessionService
from heartwood.schemas.parallel_reviews import ReviewDispatchAction, ReviewExecutionPlan
from heartwood.schemas.review import review_digest

if TYPE_CHECKING:
    from heartwood.gateway._openhands_sdk import OpenHandsSdkBackend


def bind_workflow_review_execution(backend: OpenHandsSdkBackend, service: SessionService) -> None:
    """Keep native event objects at the adapter and session mutations at their owner."""

    def authorize(actions: Sequence[ActionEvent]) -> ReviewExecutionPlan | None:
        conversation = backend._conversation
        token = conversation.cancel_token if isinstance(conversation, LocalConversation) else None
        if any(not isinstance(event.action, TaskAction) for event in actions):
            raise ValueError("Parallel review requires only native Task actions")
        return service.admit_parallel_review(
            tuple(
                ReviewDispatchAction(
                    event_id=event.id,
                    tool_call_id=event.tool_call_id,
                    reviewer_id=event.action.subagent_type,
                    action_fingerprint=review_digest(event.action.model_dump(mode="json")),
                )
                for event in actions
                if isinstance(event.action, TaskAction)
            ),
            cancelled=lambda: token is None or token.is_cancelled or backend._conversation_closing,
        )

    backend.bind_review_authorizer(authorize)
