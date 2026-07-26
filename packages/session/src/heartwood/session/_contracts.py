# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Shared command and event contract for all Heartwood interfaces."""

from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from typing import ClassVar, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue

__all__ = [
    "CommandKind",
    "EventKind",
    "JsonValue",
    "SessionCommand",
    "SessionEvent",
    "compute_session_event_hash",
    "new_command_id",
    "validate_session_id",
]

_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def validate_session_id(value: str) -> str:
    """Validate a session identifier before it reaches persistence or transport state."""
    if _SESSION_ID_PATTERN.fullmatch(value) is None:
        msg = (
            "session id must start with a letter or number and contain at most 128 "
            "letters, numbers, dots, hyphens, or underscores"
        )
        raise ValueError(msg)
    return value


class CommandKind(StrEnum):
    """Commands accepted by a Heartwood session."""

    APPROVE = "approve"
    DENY = "deny"
    CHAT = "chat"
    PAUSE = "pause"
    RESUME = "resume"
    REPLAY = "replay"
    AUDIT_EXPORT = "audit.export"


def new_command_id(session_id: str, kind: CommandKind | str) -> str:
    """Return an opaque command identifier suitable for retries and persistence."""
    validate_session_id(session_id)
    kind_value = kind.value if isinstance(kind, CommandKind) else kind
    return f"{session_id}-{kind_value}-{uuid4().hex}"


class EventKind(StrEnum):
    """Events emitted by a Heartwood session.

    The stream translates OpenHands conversation events so every surface renders
    the same turns. ``MODEL_CALL_DECISION_RECORDED`` records route authorization
    before task submission or a continuation that may call the model;
    ``POLICY_DECISION_RECORDED`` is reserved for other policy decisions.
    """

    COMMAND_RECEIVED = "command.received"
    APPROVAL_RECORDED = "approval.recorded"
    POLICY_DECISION_RECORDED = "policy.decision.recorded"
    MODEL_CALL_DECISION_RECORDED = "model_call.decision.recorded"
    USER_MESSAGE_RECORDED = "user_message.recorded"
    AGENT_MESSAGE_EMITTED = "agent_message.emitted"
    TOOL_CALL_PROPOSED = "tool_call.proposed"
    CONFIRMATION_REQUESTED = "confirmation.requested"
    CONFIRMATION_RESOLVED = "confirmation.resolved"
    TOOL_EXECUTION_RECORDED = "tool.execution.recorded"
    AGENT_LIFECYCLE_UPDATED = "agent.lifecycle.updated"
    TASK_PLAN_UPDATED = "task.plan.updated"
    MODEL_USAGE_UPDATED = "model.usage.updated"
    SUBAGENT_UPDATED = "subagent.updated"
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
    session_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=_SESSION_ID_PATTERN.pattern,
    )
    kind: CommandKind
    actor_id: str = Field(default="human", min_length=1)
    created_at: str = Field(min_length=1)
    payload: dict[str, JsonValue] = Field(default_factory=dict)


class SessionEvent(_SessionRecord):
    """Versioned event envelope emitted from the shared session stream."""

    schema_version: Literal["heartwood.session-event.v1"] = "heartwood.session-event.v1"
    event_id: str = Field(min_length=1)
    session_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=_SESSION_ID_PATTERN.pattern,
    )
    sequence: int = Field(ge=0)
    kind: EventKind
    occurred_at: str = Field(min_length=1)
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    previous_event_hash: str | None = None


def compute_session_event_hash(event: SessionEvent) -> str:
    """Return the deterministic SHA-256 hash of a complete session event."""
    canonical = json.dumps(
        event.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"
