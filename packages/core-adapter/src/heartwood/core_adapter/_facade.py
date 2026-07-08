# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Minimal execution facade for future agent backend integration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ToolExecution:
    """Structured summary of a tool execution."""

    tool_name: str
    exit_code: int
    summary: str


@dataclass(frozen=True, slots=True)
class AgentTurn:
    """Result of one deterministic backend turn."""

    backend_id: str
    tool_execution: ToolExecution | None


class AgentBackend(Protocol):
    """Stable facade for an execution backend."""

    @property
    def backend_id(self) -> str:
        """Return the backend id."""

    def run(self, *, session_id: str, prompt_length: int, approved: bool) -> AgentTurn:
        """Run one turn through the backend."""


class DeterministicAgentBackend:
    """Offline backend used by tests and synthetic replay."""

    @property
    def backend_id(self) -> str:
        """Return the backend id."""
        return "deterministic-local"

    def run(self, *, session_id: str, prompt_length: int, approved: bool) -> AgentTurn:
        """Return a deterministic tool summary without executing external tools."""
        summary = "approved deterministic no-op" if approved else "approval required"
        return AgentTurn(
            backend_id=self.backend_id,
            tool_execution=ToolExecution(
                tool_name="heartwood.synthetic.noop",
                exit_code=0 if approved else 1,
                summary=f"{summary}; prompt_length={prompt_length}; session_id={session_id}",
            ),
        )
