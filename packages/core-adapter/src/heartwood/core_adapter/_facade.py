# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Event-streaming execution facade for the agent backend.

The facade mirrors the OpenHands agent-server model: a turn produces a stream of
typed events (assistant message, proposed tool call, confirmation gate, tool
execution) rather than a single result. The first backend is deterministic and
offline; the real ``openhands-agent-server`` binding implements the same
protocol behind this facade once dependencies, policy gates, and replay behavior
are pinned.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Protocol


class BackendEventKind(StrEnum):
    """Kinds of event a backend emits while running a turn."""

    AGENT_MESSAGE = "agent_message"
    TOOL_CALL_PROPOSED = "tool_call_proposed"
    CONFIRMATION = "confirmation"
    TOOL_EXECUTION = "tool_execution"


@dataclass(frozen=True, slots=True)
class ToolExecution:
    """Structured summary of a tool execution."""

    tool_name: str
    exit_code: int
    summary: str


@dataclass(frozen=True, slots=True)
class ProposedToolCall:
    """A tool call proposed by the backend before execution."""

    tool_call_id: str
    tool_name: str
    risk: Literal["low", "medium", "high", "unknown"]
    summary: str


@dataclass(frozen=True, slots=True)
class BackendEvent:
    """One event in a backend turn stream.

    Exactly one of ``message``, ``tool_call``, ``approved``, or
    ``tool_execution`` is populated, according to ``kind``.
    """

    kind: BackendEventKind
    message: str | None = None
    tool_call: ProposedToolCall | None = None
    approved: bool | None = None
    tool_execution: ToolExecution | None = None


class AgentBackend(Protocol):
    """Stable facade for an event-streaming execution backend."""

    @property
    def backend_id(self) -> str:
        """Return the backend id."""

    def chat_turn(self, *, session_id: str, prompt_length: int) -> tuple[BackendEvent, ...]:
        """Return the event stream for a chat-only turn (no tool execution)."""

    def run_turn(
        self, *, session_id: str, prompt_length: int, approved: bool
    ) -> tuple[BackendEvent, ...]:
        """Return the event stream for a turn that proposes and gates a tool call."""


class DeterministicAgentBackend:
    """Offline backend used by tests and synthetic replay.

    It never executes external tools or reaches a model; it emits a fixed,
    content-free event stream so sessions replay deterministically.
    """

    @property
    def backend_id(self) -> str:
        """Return the backend id."""
        return "deterministic-local"

    def _tool_call(self, session_id: str) -> ProposedToolCall:
        return ProposedToolCall(
            tool_call_id=f"{session_id}-toolcall-0",
            tool_name="heartwood.synthetic.noop",
            risk="low",
            summary="run the synthetic aggregate no-op",
        )

    def _agent_message(self, session_id: str, prompt_length: int) -> BackendEvent:
        return BackendEvent(
            kind=BackendEventKind.AGENT_MESSAGE,
            message=(
                "Planned a synthetic aggregate analysis over the detected dataset "
                f"(session_id={session_id}, prompt_length={prompt_length})."
            ),
        )

    def chat_turn(self, *, session_id: str, prompt_length: int) -> tuple[BackendEvent, ...]:
        """Emit a single synthetic assistant message for a chat turn."""
        return (self._agent_message(session_id, prompt_length),)

    def run_turn(
        self, *, session_id: str, prompt_length: int, approved: bool
    ) -> tuple[BackendEvent, ...]:
        """Emit message, proposed tool call, confirmation gate, and execution."""
        tool_call = self._tool_call(session_id)
        summary = "approved deterministic no-op" if approved else "approval required"
        return (
            self._agent_message(session_id, prompt_length),
            BackendEvent(kind=BackendEventKind.TOOL_CALL_PROPOSED, tool_call=tool_call),
            BackendEvent(
                kind=BackendEventKind.CONFIRMATION, tool_call=tool_call, approved=approved
            ),
            BackendEvent(
                kind=BackendEventKind.TOOL_EXECUTION,
                tool_execution=ToolExecution(
                    tool_name=tool_call.tool_name,
                    exit_code=0 if approved else 1,
                    summary=f"{summary}; prompt_length={prompt_length}; session_id={session_id}",
                ),
            ),
        )
