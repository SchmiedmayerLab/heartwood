# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Content-minimized persistence for OpenHands conversation events."""

from __future__ import annotations

from openhands.sdk import LocalFileStore
from openhands.sdk.event import Event
from openhands.sdk.event.conversation_error import ConversationErrorEvent
from openhands.sdk.event.error_classification import FailureKind

_SAFE_ERROR_DETAILS = {
    FailureKind.AUTH: "Model provider authentication failed.",
    FailureKind.QUOTA: "The model provider reported an exhausted quota or budget.",
    FailureKind.RATE_LIMIT: "The model provider temporarily limited requests.",
    FailureKind.CONFIG: "The model connection is not configured correctly.",
    FailureKind.TRANSIENT: "The model provider is temporarily unavailable.",
    FailureKind.AGENT_ACTION: "The agent could not complete the requested action.",
    FailureKind.INTERNAL: "The agent runtime stopped unexpectedly.",
    FailureKind.UNKNOWN: "The agent conversation stopped unexpectedly.",
}


class ContentMinimizedLocalFileStore(LocalFileStore):
    """Persist OpenHands state while minimizing provider failure content."""

    def write(self, path: str, contents: str | bytes) -> None:
        if isinstance(contents, str) and path.startswith("events/event-"):
            contents = _minimize_event(contents)
        super().write(path, contents)


def _minimize_event(contents: str) -> str:
    if "ConversationErrorEvent" not in contents:
        return contents
    event = Event.model_validate_json(contents)
    if not isinstance(event, ConversationErrorEvent):
        return contents
    classification = event.classification
    detail = _SAFE_ERROR_DETAILS[
        FailureKind.UNKNOWN if classification is None else classification.kind
    ]
    return event.model_copy(update={"detail": detail}).model_dump_json(exclude_none=True)


__all__ = ["ContentMinimizedLocalFileStore"]
