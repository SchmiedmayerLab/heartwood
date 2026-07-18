# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Small execution facade shared by deterministic and OpenHands backends."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol

from heartwood.schemas import JsonValue


class BackendEventKind(StrEnum):
    """Kinds of event emitted by an execution backend."""

    AGENT_MESSAGE = "agent_message"
    TOOL_CALL_PROPOSED = "tool_call_proposed"
    CONFIRMATION_REQUESTED = "confirmation_requested"
    CONFIRMATION_RESOLVED = "confirmation_resolved"
    TOOL_EXECUTION = "tool_execution"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ToolExecution:
    """Content-minimized summary of a tool observation."""

    tool_name: str
    exit_code: int
    summary: str


@dataclass(frozen=True, slots=True)
class ProposedToolCall:
    """A tool action proposed before execution."""

    tool_call_id: str
    tool_name: str
    risk: Literal["low", "medium", "high", "unknown"]
    summary: str
    arguments: dict[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class BackendEvent:
    """One SDK-neutral event emitted by an agent backend."""

    kind: BackendEventKind
    message: str | None = None
    tool_call: ProposedToolCall | None = None
    approved: bool | None = None
    tool_execution: ToolExecution | None = None


class AgentBackend(Protocol):
    """Stable facade over OpenHands and deterministic test conversations."""

    @property
    def backend_id(self) -> str:
        """Return the backend id."""

    @property
    def configuration_error(self) -> str | None:
        """Return a content-minimized reason the backend cannot start a turn."""

    @property
    def model_endpoint(self) -> str:
        """Return the declared normalized endpoint evaluated by Heartwood policy."""

    @property
    def model_profile_id(self) -> str:
        """Return the stable non-secret model profile identifier."""

    @property
    def capability_tier(self) -> str:
        """Return the configured model capability tier."""

    @property
    def credential_reference(self) -> str | None:
        """Return the non-secret credential reference evaluated by policy."""

    @property
    def action_confirmation_mode(self) -> str:
        """Return the selected OpenHands action-confirmation mode."""

    @property
    def continuation_requires_model_authorization(self) -> bool:
        """Return whether approval or resume can continue model execution."""

    def submit_turn(self, *, session_id: str, prompt: str) -> tuple[BackendEvent, ...]:
        """Submit a user task and run until completion or confirmation."""

    def restore_pending(self, tool_calls: tuple[ProposedToolCall, ...]) -> None:
        """Restore pending confirmation state from the Heartwood event log."""

    def resolve_confirmation(
        self,
        *,
        session_id: str,
        tool_call_id: str,
        approved: bool,
    ) -> tuple[BackendEvent, ...]:
        """Resolve the pending action; a rejection must not continue the model."""

    def pause(self) -> None:
        """Pause the conversation."""

    def resume(self, *, session_id: str) -> tuple[BackendEvent, ...]:
        """Resume a paused conversation."""

    def close(self) -> None:
        """Release backend resources."""


class DeterministicAgentBackend:
    """Deterministic conversation used by unit tests and replay fixtures."""

    def __init__(self, *, action_confirmation_mode: str = "always-confirm") -> None:
        if action_confirmation_mode not in {"always-confirm", "confirm-risky"}:
            msg = f"unsupported action confirmation mode: {action_confirmation_mode}"
            raise ValueError(msg)
        self._action_confirmation_mode = action_confirmation_mode
        self._pending: ProposedToolCall | None = None

    @property
    def backend_id(self) -> str:
        """Return the backend id."""
        return "deterministic-local"

    @property
    def configuration_error(self) -> str | None:
        """Return no configuration error for the deterministic fixture."""
        return None

    @property
    def model_endpoint(self) -> str:
        """Return the synthetic endpoint covered by the generic policy."""
        return "https://model.local.invalid/v1/chat/completions"

    @property
    def model_profile_id(self) -> str:
        """Return the deterministic fixture profile identifier."""
        return "deterministic-local"

    @property
    def capability_tier(self) -> str:
        """Return the deterministic capability tier."""
        return "supervised"

    @property
    def credential_reference(self) -> str | None:
        """Return no credential for the deterministic backend."""
        return None

    @property
    def action_confirmation_mode(self) -> str:
        """Return the selected deterministic confirmation mode."""
        return self._action_confirmation_mode

    @property
    def continuation_requires_model_authorization(self) -> bool:
        """Return false because the deterministic backend makes no model calls."""
        return False

    def submit_turn(self, *, session_id: str, prompt: str) -> tuple[BackendEvent, ...]:
        """Emit a message and one pending synthetic action."""
        if self._pending is not None:
            return (
                BackendEvent(
                    kind=BackendEventKind.ERROR,
                    message="resolve the pending action before submitting another task",
                ),
            )
        self._pending = ProposedToolCall(
            tool_call_id=f"{session_id}-toolcall-0",
            tool_name="heartwood.synthetic.noop",
            risk="low",
            summary="run the synthetic aggregate no-op",
        )
        events = (
            BackendEvent(
                kind=BackendEventKind.AGENT_MESSAGE,
                message=(
                    "Planned a synthetic aggregate analysis over the detected dataset "
                    f"(session_id={session_id}, prompt_length={len(prompt)})."
                ),
            ),
            BackendEvent(kind=BackendEventKind.TOOL_CALL_PROPOSED, tool_call=self._pending),
        )
        if self.action_confirmation_mode == "confirm-risky":
            self._pending = None
            return (
                *events,
                BackendEvent(
                    kind=BackendEventKind.TOOL_EXECUTION,
                    tool_execution=ToolExecution(
                        tool_name="heartwood.synthetic.noop",
                        exit_code=0,
                        summary=f"automatically executed low-risk action; session_id={session_id}",
                    ),
                ),
            )
        return (
            *events,
            BackendEvent(kind=BackendEventKind.CONFIRMATION_REQUESTED, tool_call=self._pending),
        )

    def restore_pending(self, tool_calls: tuple[ProposedToolCall, ...]) -> None:
        """Restore pending deterministic confirmation state."""
        self._pending = tool_calls[0] if len(tool_calls) == 1 else None

    def resolve_confirmation(
        self,
        *,
        session_id: str,
        tool_call_id: str,
        approved: bool,
    ) -> tuple[BackendEvent, ...]:
        """Resolve and clear the pending synthetic action."""
        pending = self._pending
        if pending is None or pending.tool_call_id != tool_call_id:
            return (
                BackendEvent(
                    kind=BackendEventKind.ERROR,
                    message=f"no matching pending action: {tool_call_id}",
                ),
            )
        self._pending = None
        resolved = BackendEvent(
            kind=BackendEventKind.CONFIRMATION_RESOLVED,
            tool_call=pending,
            approved=approved,
        )
        if not approved:
            return (resolved,)
        return (
            resolved,
            BackendEvent(
                kind=BackendEventKind.TOOL_EXECUTION,
                tool_execution=ToolExecution(
                    tool_name=pending.tool_name,
                    exit_code=0,
                    summary=f"approved deterministic action; session_id={session_id}",
                ),
            ),
        )

    def pause(self) -> None:
        """Pause the deterministic backend."""

    def resume(self, *, session_id: str) -> tuple[BackendEvent, ...]:  # noqa: ARG002
        """Resume the deterministic backend without producing events."""
        return ()

    def close(self) -> None:
        """Release deterministic backend resources."""


class LocalWorkspaceAgentBackend(DeterministicAgentBackend):
    """Deterministic test backend that writes one bounded local artifact."""

    def __init__(self, artifact_dir: Path) -> None:
        super().__init__()
        self.artifact_dir = artifact_dir.resolve()

    @property
    def backend_id(self) -> str:
        """Return the backend id."""
        return "local-workspace"

    def submit_turn(self, *, session_id: str, prompt: str) -> tuple[BackendEvent, ...]:
        """Emit one pending bounded workspace action."""
        events = super().submit_turn(session_id=session_id, prompt=prompt)
        if self._pending is None:
            return events
        self._pending = ProposedToolCall(
            tool_call_id=self._pending.tool_call_id,
            tool_name="heartwood.local.write_summary",
            risk="low",
            summary="write a synthetic workspace summary artifact",
        )
        return (
            events[0],
            BackendEvent(kind=BackendEventKind.TOOL_CALL_PROPOSED, tool_call=self._pending),
            BackendEvent(kind=BackendEventKind.CONFIRMATION_REQUESTED, tool_call=self._pending),
        )

    def resolve_confirmation(
        self,
        *,
        session_id: str,
        tool_call_id: str,
        approved: bool,
    ) -> tuple[BackendEvent, ...]:
        """Write the bounded artifact after an allow-once decision."""
        pending = self._pending
        if pending is None or pending.tool_call_id != tool_call_id:
            return super().resolve_confirmation(
                session_id=session_id,
                tool_call_id=tool_call_id,
                approved=approved,
            )
        self._pending = None
        resolved = BackendEvent(
            kind=BackendEventKind.CONFIRMATION_RESOLVED,
            tool_call=pending,
            approved=approved,
        )
        if not approved:
            return (resolved,)
        path = self._write_summary(session_id)
        return (
            resolved,
            BackendEvent(
                kind=BackendEventKind.TOOL_EXECUTION,
                tool_execution=ToolExecution(
                    tool_name=pending.tool_name,
                    exit_code=0,
                    summary=(f"wrote synthetic workspace artifact: {path.parent.name}/{path.name}"),
                ),
            ),
        )

    def _write_summary(self, session_id: str) -> Path:
        path = (self.artifact_dir / "synthetic-workspace-summary.md").resolve()
        if path.parent != self.artifact_dir:
            msg = f"artifact path escapes backend directory: {path}"
            raise ValueError(msg)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "\n".join(
                (
                    "# Synthetic Workspace Summary",
                    "",
                    f"- Session: `{session_id}`",
                    "- Dataset: synthetic OMOP fixture",
                    "- Tool action: local workspace artifact write",
                    "- Persisted prompt content: none",
                    "",
                )
            ),
            encoding="utf-8",
        )
        return path
