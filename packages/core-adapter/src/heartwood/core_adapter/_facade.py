# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Small execution facade shared by deterministic and OpenHands backends."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol, cast

from heartwood.core_adapter._state import _write_private_json_atomic
from heartwood.schemas import JsonValue


class BackendEventKind(StrEnum):
    """Kinds of event emitted by an execution backend."""

    AGENT_MESSAGE = "agent_message"
    TOOL_CALL_PROPOSED = "tool_call_proposed"
    CONFIRMATION_REQUESTED = "confirmation_requested"
    CONFIRMATION_RESOLVED = "confirmation_resolved"
    TOOL_EXECUTION = "tool_execution"
    LIFECYCLE = "lifecycle"
    TASK_PLAN = "task_plan"
    USAGE = "usage"
    SUBAGENT = "subagent"
    ERROR = "error"


class BackendErrorCode(StrEnum):
    """Stable content-safe errors emitted by an execution backend."""

    RUNTIME_UNAVAILABLE = "HW-AGENT-001"
    ACTION_FAILED = "HW-AGENT-002"
    CONVERSATION_STOPPED = "HW-AGENT-003"
    WORKER_STOPPED = "HW-AGENT-004"
    INVALID_STATE = "HW-AGENT-005"
    ACTION_OUTCOME_UNKNOWN = "HW-AGENT-006"
    AGENT_OUTCOME_UNKNOWN = "HW-AGENT-007"
    UNKNOWN = "HW-AGENT-999"


_FATAL_BACKEND_ERROR_CODES = frozenset(
    {
        BackendErrorCode.ACTION_OUTCOME_UNKNOWN.value,
        BackendErrorCode.AGENT_OUTCOME_UNKNOWN.value,
    }
)


def backend_error_is_fatal(code: object) -> bool:
    """Return whether a backend error makes further session work unsafe."""
    return code in _FATAL_BACKEND_ERROR_CODES


def backend_error_message(code: BackendErrorCode) -> str:
    """Return the public message associated with a stable backend error."""
    return {
        BackendErrorCode.RUNTIME_UNAVAILABLE: "Agent runtime is unavailable",
        BackendErrorCode.ACTION_FAILED: "An agent action failed",
        BackendErrorCode.CONVERSATION_STOPPED: "The agent conversation stopped",
        BackendErrorCode.WORKER_STOPPED: "The agent worker stopped",
        BackendErrorCode.INVALID_STATE: (
            "The agent cannot perform that operation in its current state"
        ),
        BackendErrorCode.ACTION_OUTCOME_UNKNOWN: (
            "A previously approved action has an unknown outcome; verify the project "
            "and continue in a new session"
        ),
        BackendErrorCode.AGENT_OUTCOME_UNKNOWN: (
            "A previously started agent turn has an unknown outcome; inspect the session "
            "and continue in a new session"
        ),
        BackendErrorCode.UNKNOWN: "The agent runtime reported an error",
    }[code]


class BackendLifecycle(StrEnum):
    """Normalized OpenHands conversation lifecycle."""

    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    WAITING_FOR_CONFIRMATION = "waiting-for-confirmation"
    FINISHED = "finished"
    ERROR = "error"


class BackendTaskStatus(StrEnum):
    """Normalized OpenHands task status."""

    TODO = "todo"
    IN_PROGRESS = "in-progress"
    DONE = "done"


class BackendSubagentStatus(StrEnum):
    """Normalized sequential subagent lifecycle."""

    PROPOSED = "proposed"
    RUNNING = "running"
    COMPLETED = "completed"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ToolExecution:
    """Bounded private result of a tool observation."""

    tool_call_id: str
    action_id: str | None
    tool_name: str
    exit_code: int
    summary: str
    result: str | None = None
    result_truncated: bool = False


@dataclass(frozen=True, slots=True)
class ProposedToolCall:
    """A tool action proposed before execution."""

    tool_call_id: str
    tool_name: str
    risk: Literal["low", "medium", "high", "unknown"]
    summary: str
    arguments: dict[str, JsonValue] = field(default_factory=dict)
    action_id: str | None = None
    kind: Literal["terminal", "file-editor", "task", "other"] = "other"
    affected_paths: tuple[str, ...] = ()
    project_path: str | None = None


@dataclass(frozen=True, slots=True)
class PendingActionGroup:
    """One atomic OpenHands confirmation decision."""

    group_id: str
    actions: tuple[ProposedToolCall, ...]


def pending_action_group(
    actions: tuple[ProposedToolCall, ...],
) -> PendingActionGroup | None:
    """Return the stable atomic group represented by ordered pending actions."""
    if not actions:
        return None
    canonical = "\n".join(action.tool_call_id for action in actions)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return PendingActionGroup(
        group_id=f"action-set-{digest}",
        actions=actions,
    )


@dataclass(frozen=True, slots=True)
class BackendTask:
    """One task projected from the OpenHands Task Tracker."""

    title: str
    status: BackendTaskStatus


@dataclass(frozen=True, slots=True)
class BackendUsage:
    """Content-minimized cumulative model usage."""

    usage_id: str
    model_name: str
    call_count: int
    prompt_tokens: int
    completion_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    context_window: int | None = None
    accumulated_cost: float = 0.0


@dataclass(frozen=True, slots=True)
class BackendSubagent:
    """One sequential specialized-agent task."""

    invocation_id: str
    task_id: str | None
    agent_name: str
    status: BackendSubagentStatus
    parent_session_id: str
    parent_action_id: str


@dataclass(frozen=True, slots=True, kw_only=True)
class _BackendEvent:
    """Common source identity for one SDK-neutral backend event."""

    source_event_id: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class BackendAgentMessageEvent(_BackendEvent):
    message: str
    kind: Literal[BackendEventKind.AGENT_MESSAGE] = field(
        default=BackendEventKind.AGENT_MESSAGE,
        init=False,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class BackendToolCallEvent(_BackendEvent):
    tool_call: ProposedToolCall
    kind: Literal[BackendEventKind.TOOL_CALL_PROPOSED] = field(
        default=BackendEventKind.TOOL_CALL_PROPOSED,
        init=False,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class BackendConfirmationRequestEvent(_BackendEvent):
    tool_call: ProposedToolCall
    action_group_id: str
    kind: Literal[BackendEventKind.CONFIRMATION_REQUESTED] = field(
        default=BackendEventKind.CONFIRMATION_REQUESTED,
        init=False,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class BackendConfirmationResolutionEvent(_BackendEvent):
    tool_call: ProposedToolCall
    action_group_id: str
    approved: bool
    kind: Literal[BackendEventKind.CONFIRMATION_RESOLVED] = field(
        default=BackendEventKind.CONFIRMATION_RESOLVED,
        init=False,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class BackendToolExecutionEvent(_BackendEvent):
    tool_execution: ToolExecution
    kind: Literal[BackendEventKind.TOOL_EXECUTION] = field(
        default=BackendEventKind.TOOL_EXECUTION,
        init=False,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class BackendLifecycleEvent(_BackendEvent):
    lifecycle: BackendLifecycle
    kind: Literal[BackendEventKind.LIFECYCLE] = field(
        default=BackendEventKind.LIFECYCLE,
        init=False,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class BackendTaskPlanEvent(_BackendEvent):
    tasks: tuple[BackendTask, ...]
    kind: Literal[BackendEventKind.TASK_PLAN] = field(
        default=BackendEventKind.TASK_PLAN,
        init=False,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class BackendUsageEvent(_BackendEvent):
    usage: BackendUsage
    kind: Literal[BackendEventKind.USAGE] = field(
        default=BackendEventKind.USAGE,
        init=False,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class BackendSubagentEvent(_BackendEvent):
    subagent: BackendSubagent
    kind: Literal[BackendEventKind.SUBAGENT] = field(
        default=BackendEventKind.SUBAGENT,
        init=False,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class BackendErrorEvent(_BackendEvent):
    error_code: BackendErrorCode
    kind: Literal[BackendEventKind.ERROR] = field(
        default=BackendEventKind.ERROR,
        init=False,
    )


type BackendEvent = (
    BackendAgentMessageEvent
    | BackendToolCallEvent
    | BackendConfirmationRequestEvent
    | BackendConfirmationResolutionEvent
    | BackendToolExecutionEvent
    | BackendLifecycleEvent
    | BackendTaskPlanEvent
    | BackendUsageEvent
    | BackendSubagentEvent
    | BackendErrorEvent
)


BackendEventSink = Callable[[tuple[BackendEvent, ...]], None]
TokenDeltaSink = Callable[[str], None]


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

    def bind_runtime(
        self,
        *,
        event_sink: BackendEventSink,
        token_sink: TokenDeltaSink,
    ) -> None:
        """Bind durable and transient gateway-owned runtime sinks."""

    def reconcile(
        self,
        *,
        session_id: str,
        known_source_event_ids: frozenset[str],
    ) -> tuple[BackendEvent, ...]:
        """Project SDK state not yet present in the Heartwood event stream."""

    def pending_action_group(self, *, session_id: str) -> PendingActionGroup | None:
        """Return the atomic unmatched action group directly from backend state."""

    def submit_turn(self, *, session_id: str, prompt: str) -> tuple[BackendEvent, ...]:
        """Submit a user task and start or steer execution."""

    def resolve_confirmation(
        self,
        *,
        session_id: str,
        action_group_id: str,
        approved: bool,
    ) -> tuple[BackendEvent, ...]:
        """Apply a gateway-recorded decision to the complete pending action group."""

    def pause(self, *, session_id: str) -> tuple[BackendEvent, ...]:
        """Pause the conversation after reaching a stable execution boundary."""

    def resume(self, *, session_id: str) -> tuple[BackendEvent, ...]:
        """Resume a paused conversation."""

    def close(self) -> None:
        """Release backend resources."""


class DeterministicAgentBackend:
    """Deterministic conversation used by unit tests and replay fixtures."""

    def __init__(
        self,
        *,
        action_confirmation_mode: str = "always-confirm",
        persistence_path: Path | None = None,
    ) -> None:
        if action_confirmation_mode not in {"always-confirm", "confirm-risky"}:
            msg = f"unsupported action confirmation mode: {action_confirmation_mode}"
            raise ValueError(msg)
        self._action_confirmation_mode = action_confirmation_mode
        self._persistence_path = None if persistence_path is None else persistence_path.resolve()
        self._pending = self._load_pending()
        self._event_sink: BackendEventSink = lambda _events: None
        self._token_sink: TokenDeltaSink = lambda _delta: None

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

    def bind_runtime(
        self,
        *,
        event_sink: BackendEventSink,
        token_sink: TokenDeltaSink,
    ) -> None:
        """Bind runtime sinks for contract parity with OpenHands."""
        self._event_sink = event_sink
        self._token_sink = token_sink

    def reconcile(
        self,
        *,
        session_id: str,  # noqa: ARG002
        known_source_event_ids: frozenset[str],  # noqa: ARG002
    ) -> tuple[BackendEvent, ...]:
        """Return no additional events for the in-memory deterministic backend."""
        return ()

    def pending_action_group(
        self,
        *,
        session_id: str,  # noqa: ARG002
    ) -> PendingActionGroup | None:
        """Return the current deterministic pending action group."""
        actions = () if self._pending is None else (self._pending,)
        return pending_action_group(actions)

    def submit_turn(self, *, session_id: str, prompt: str) -> tuple[BackendEvent, ...]:
        """Emit a message and one pending synthetic action."""
        if self._pending is not None:
            return (
                BackendErrorEvent(
                    error_code=BackendErrorCode.INVALID_STATE,
                ),
            )
        self._pending = ProposedToolCall(
            tool_call_id=f"{session_id}-toolcall-0",
            tool_name="heartwood.synthetic.noop",
            risk="low",
            summary="run the synthetic aggregate no-op",
        )
        events = (
            BackendAgentMessageEvent(
                message=(
                    "Planned a synthetic aggregate analysis over the detected dataset "
                    f"(session_id={session_id}, prompt_length={len(prompt)})."
                ),
            ),
            BackendToolCallEvent(tool_call=self._pending),
        )
        if self.action_confirmation_mode == "confirm-risky":
            pending = self._pending
            self._pending = None
            self._persist_pending()
            return (
                *events,
                BackendToolExecutionEvent(
                    tool_execution=ToolExecution(
                        tool_call_id=pending.tool_call_id,
                        action_id=None,
                        tool_name="heartwood.synthetic.noop",
                        exit_code=0,
                        summary=f"automatically executed low-risk action; session_id={session_id}",
                    ),
                ),
            )
        self._persist_pending()
        action_group = self.pending_action_group(session_id=session_id)
        if action_group is None:  # pragma: no cover - pending action was just assigned
            raise RuntimeError("deterministic action group was not created")
        return (
            *events,
            BackendConfirmationRequestEvent(
                tool_call=self._pending,
                action_group_id=action_group.group_id,
            ),
        )

    def resolve_confirmation(
        self,
        *,
        session_id: str,
        action_group_id: str,
        approved: bool,
    ) -> tuple[BackendEvent, ...]:
        """Apply a gateway-recorded decision and clear the pending synthetic action."""
        pending = self._pending
        group = self.pending_action_group(session_id=session_id)
        if pending is None or group is None or group.group_id != action_group_id:
            return (
                BackendErrorEvent(
                    error_code=BackendErrorCode.INVALID_STATE,
                ),
            )
        self._pending = None
        self._persist_pending()
        if not approved:
            return (
                BackendConfirmationResolutionEvent(
                    tool_call=pending,
                    action_group_id=group.group_id,
                    approved=False,
                    source_event_id=_deterministic_confirmation_source(pending.tool_call_id),
                ),
            )
        return (
            BackendConfirmationResolutionEvent(
                tool_call=pending,
                action_group_id=group.group_id,
                approved=True,
                source_event_id=_deterministic_confirmation_source(pending.tool_call_id),
            ),
            BackendToolExecutionEvent(
                tool_execution=ToolExecution(
                    tool_call_id=pending.tool_call_id,
                    action_id=None,
                    tool_name=pending.tool_name,
                    exit_code=0,
                    summary=f"approved deterministic action; session_id={session_id}",
                ),
            ),
        )

    def pause(self, *, session_id: str) -> tuple[BackendEvent, ...]:  # noqa: ARG002
        """Pause the deterministic backend."""
        return ()

    def resume(self, *, session_id: str) -> tuple[BackendEvent, ...]:  # noqa: ARG002
        """Resume the deterministic backend without producing events."""
        return ()

    def close(self) -> None:
        """Release deterministic backend resources."""

    def _load_pending(self) -> ProposedToolCall | None:
        path = self._persistence_path
        if path is None or not path.is_file():
            return None
        try:
            raw: object = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as error:
            raise ValueError("deterministic backend state is invalid") from error
        if not isinstance(raw, dict):
            raise ValueError("deterministic backend state is invalid")
        tool_call_id = raw.get("tool_call_id")
        tool_name = raw.get("tool_name")
        if (
            not isinstance(tool_call_id, str)
            or not tool_call_id
            or not isinstance(tool_name, str)
            or not tool_name
        ):
            raise ValueError("deterministic backend state is invalid")
        risk_value = raw.get("risk")
        risk: Literal["low", "medium", "high", "unknown"]
        if risk_value in {"low", "medium", "high"}:
            risk = cast(Literal["low", "medium", "high"], risk_value)
        else:
            risk = "unknown"
        arguments = raw.get("arguments")
        return ProposedToolCall(
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            risk=risk,
            summary=str(raw.get("summary", "pending action")),
            arguments=(
                cast(dict[str, JsonValue], arguments) if isinstance(arguments, dict) else {}
            ),
            action_id=(
                str(raw["action_id"])
                if isinstance(raw.get("action_id"), str) and raw["action_id"]
                else None
            ),
            kind=_persisted_tool_kind(raw.get("kind")),
            affected_paths=_persisted_affected_paths(raw.get("affected_paths")),
            project_path=(
                str(raw["project_path"])
                if isinstance(raw.get("project_path"), str) and raw["project_path"]
                else None
            ),
        )

    def _persist_pending(self) -> None:
        path = self._persistence_path
        if path is None:
            return
        if self._pending is None:
            path.unlink(missing_ok=True)
            return
        _write_private_json_atomic(
            path,
            {
                "tool_call_id": self._pending.tool_call_id,
                "tool_name": self._pending.tool_name,
                "risk": self._pending.risk,
                "summary": self._pending.summary,
                "arguments": self._pending.arguments,
                "action_id": self._pending.action_id,
                "kind": self._pending.kind,
                "affected_paths": list(self._pending.affected_paths),
                "project_path": self._pending.project_path,
            },
        )


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
        self._persist_pending()
        action_group = self.pending_action_group(session_id=session_id)
        if action_group is None:  # pragma: no cover - assignment above guarantees presence
            raise RuntimeError("deterministic action group was not created")
        return (
            events[0],
            BackendToolCallEvent(tool_call=self._pending),
            BackendConfirmationRequestEvent(
                tool_call=self._pending,
                action_group_id=action_group.group_id,
            ),
        )

    def resolve_confirmation(
        self,
        *,
        session_id: str,
        action_group_id: str,
        approved: bool,
    ) -> tuple[BackendEvent, ...]:
        """Apply a gateway-recorded decision and optionally write the artifact."""
        pending = self._pending
        group = self.pending_action_group(session_id=session_id)
        if pending is None or group is None or group.group_id != action_group_id:
            return super().resolve_confirmation(
                session_id=session_id,
                action_group_id=action_group_id,
                approved=approved,
            )
        self._pending = None
        self._persist_pending()
        if not approved:
            return (
                BackendConfirmationResolutionEvent(
                    tool_call=pending,
                    action_group_id=group.group_id,
                    approved=False,
                    source_event_id=_deterministic_confirmation_source(pending.tool_call_id),
                ),
            )
        path = self._write_summary(session_id)
        return (
            BackendConfirmationResolutionEvent(
                tool_call=pending,
                action_group_id=group.group_id,
                approved=True,
                source_event_id=_deterministic_confirmation_source(pending.tool_call_id),
            ),
            BackendToolExecutionEvent(
                tool_execution=ToolExecution(
                    tool_call_id=pending.tool_call_id,
                    action_id=None,
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


def _deterministic_confirmation_source(tool_call_id: str) -> str:
    return f"deterministic-tool-call:{tool_call_id}:confirmation-resolution"


def _persisted_tool_kind(
    value: object,
) -> Literal["terminal", "file-editor", "task", "other"]:
    if value in {"terminal", "file-editor", "task"}:
        return value
    return "other"


def _persisted_affected_paths(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item)
