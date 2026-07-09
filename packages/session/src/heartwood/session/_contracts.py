# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Shared command and event contract for all Heartwood interfaces."""

from __future__ import annotations

from enum import StrEnum
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

__all__ = ["CommandKind", "EventKind", "JsonValue", "SessionCommand", "SessionEvent"]


class CommandKind(StrEnum):
    """Commands accepted by a Heartwood session."""

    DETECT = "detect"
    APPROVE = "approve"
    DENY = "deny"
    CHAT = "chat"
    RUN = "run"
    PAUSE = "pause"
    RESUME = "resume"
    REPLAY = "replay"
    AUDIT_EXPORT = "audit.export"


class EventKind(StrEnum):
    """Events emitted by a Heartwood session.

    The stream mirrors the agent-server event model so every surface renders the
    same turns. ``MODEL_CALL_DECISION_RECORDED`` is the egress decision for one
    model call; ``POLICY_DECISION_RECORDED`` is reserved for non-egress policy
    decisions.
    """

    COMMAND_RECEIVED = "command.received"
    DETECTION_PROPOSED = "detection.proposed"
    APPROVAL_RECORDED = "approval.recorded"
    POLICY_DECISION_RECORDED = "policy.decision.recorded"
    MODEL_CALL_DECISION_RECORDED = "model_call.decision.recorded"
    AGENT_MESSAGE_EMITTED = "agent_message.emitted"
    TOOL_CALL_PROPOSED = "tool_call.proposed"
    CONFIRMATION_REQUESTED = "confirmation.requested"
    CONFIRMATION_RESOLVED = "confirmation.resolved"
    TOOL_EXECUTION_RECORDED = "tool.execution.recorded"
    SESSION_PAUSED = "session.paused"
    SESSION_RESUMED = "session.resumed"
    AUDIT_EXPORT_RECORDED = "audit.export.recorded"
    ERROR_RECORDED = "error.recorded"


class _SessionRecord(BaseModel):
    """Base model for immutable session contract records."""

    model_config: ClassVar[ConfigDict] = ConfigDict(
        extra="forbid",
        frozen=True,
        use_enum_values=True,
    )


class SessionCommand(_SessionRecord):
    """Versioned command envelope shared by CLI, notebook API, and UI adapters."""

    schema_version: Literal["heartwood.session-command.v1"] = "heartwood.session-command.v1"
    command_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    kind: CommandKind
    actor_id: str = Field(default="human", min_length=1)
    created_at: str = Field(min_length=1)
    payload: dict[str, JsonValue] = Field(default_factory=dict)


class SessionEvent(_SessionRecord):
    """Versioned event envelope emitted from the shared session stream."""

    schema_version: Literal["heartwood.session-event.v1"] = "heartwood.session-event.v1"
    event_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    sequence: int = Field(ge=0)
    kind: EventKind
    occurred_at: str = Field(min_length=1)
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    previous_event_hash: str | None = None
