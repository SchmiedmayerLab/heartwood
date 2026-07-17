# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""OpenHands SDK conversation adapter."""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Mapping, Sequence
from importlib import import_module
from pathlib import Path
from typing import Any, Protocol, cast

from heartwood.core_adapter import (
    BackendEvent,
    BackendEventKind,
    ProposedToolCall,
    ToolExecution,
)
from heartwood.gateway._model_settings import ModelProfile, ModelSettingsError
from heartwood.schemas import ActionConfirmationMode


class OpenHandsSdkError(RuntimeError):
    """Raised when an OpenHands conversation cannot be configured or run."""


class _ConversationState(Protocol):
    execution_status: object


class _Conversation(Protocol):
    state: _ConversationState

    def send_message(self, message: str, sender: str | None = None) -> None:
        """Send a user message."""

    def run(self) -> None:
        """Run until completion or confirmation."""

    def reject_pending_actions(self, reason: str = "User rejected the action") -> None:
        """Reject pending actions."""

    def pause(self) -> None:
        """Pause execution."""

    def close(self) -> None:
        """Close the conversation."""


class _SecurityAnalyzer(Protocol):
    def security_risk(self, action: object) -> object:
        """Return the upstream risk assessment for one action."""


class _AgentContextFactory(Protocol):
    def __call__(self, **options: object) -> object:
        """Build one OpenHands agent context."""


class _Llm(Protocol):
    def model_copy(self, *, update: dict[str, object]) -> _Llm:
        """Copy the model route with condenser-specific options."""

    def reset_metrics(self) -> None:
        """Separate condenser usage from the agent model metrics."""


class _CondenserFactory(Protocol):
    def __call__(self, **options: object) -> object:
        """Build one OpenHands history condenser."""


class _SdkModule(Protocol):
    AgentContext: _AgentContextFactory
    LLMSummarizingCondenser: _CondenserFactory


ConversationFactory = Callable[[Callable[[object], None]], _Conversation]

_AGENT_LLM_NUM_RETRIES = 2
_AGENT_LLM_LOCAL_NUM_RETRIES = 1
_AGENT_LLM_RETRY_MAX_WAIT_SECONDS = 8
_AGENT_LLM_RETRY_MIN_WAIT_SECONDS = 1
_AGENT_LLM_RETRY_MULTIPLIER = 2.0
_AGENT_LLM_LOCAL_TIMEOUT_SECONDS = 600
_AGENT_LLM_TIMEOUT_SECONDS = 180
_AGENT_LLM_DEFAULT_MAX_MESSAGE_CHARS = 30_000
_AGENT_LLM_ESTIMATED_CHARS_PER_TOKEN = 4
_AGENT_CONDENSER_INPUT_FRACTION = 0.75
_AGENT_CONDENSER_MAX_EVENTS = 240
_AGENT_CONDENSER_KEEP_FIRST = 2


class OpenHandsSdkBackend:
    """Run a real OpenHands conversation behind the Heartwood event facade."""

    def __init__(
        self,
        *,
        profile: ModelProfile,
        workspace: Path,
        skills_dir: Path,
        persistence_dir: Path,
        conversation_key: str,
        additional_skills_dirs: Sequence[Path] = (),
        credential_environment_names: Sequence[str] = (),
        action_confirmation_mode: ActionConfirmationMode = "always-confirm",
        env: Mapping[str, str] | None = None,
        conversation_factory: ConversationFactory | None = None,
    ) -> None:
        profile.validate()
        if action_confirmation_mode not in {"always-confirm", "confirm-risky"}:
            msg = f"unsupported action confirmation mode: {action_confirmation_mode}"
            raise OpenHandsSdkError(msg)
        self.profile = profile
        self._action_confirmation_mode = action_confirmation_mode
        self.workspace = workspace.resolve()
        self.skills_dir = skills_dir.resolve()
        self.additional_skills_dirs = tuple(path.resolve() for path in additional_skills_dirs)
        self.persistence_dir = persistence_dir.resolve()
        self.conversation_key = conversation_key
        self._credential_environment_names = tuple(sorted(set(credential_environment_names)))
        self.env = env
        self._captured: list[object] = []
        self._pending: dict[str, ProposedToolCall] = {}
        self._security_analyzer: _SecurityAnalyzer | None = None
        self._conversation_factory = conversation_factory or self._default_conversation_factory
        self._conversation: _Conversation | None = None

    @property
    def backend_id(self) -> str:
        """Return the backend id."""
        return "openhands-sdk"

    @property
    def configuration_error(self) -> str | None:
        """Return a safe preflight error before recording a route decision."""
        try:
            self.profile.resolve_api_key(self.env)
        except ModelSettingsError:
            return "active model profile credential reference is unavailable"
        return None

    @property
    def model_endpoint(self) -> str:
        """Return the declared normalized endpoint evaluated by Heartwood policy."""
        return self.profile.policy_endpoint

    @property
    def model_profile_id(self) -> str:
        """Return the selected non-secret profile identifier."""
        return self.profile.profile_id

    @property
    def capability_tier(self) -> str:
        """Return the configured model capability tier."""
        return self.profile.capability_tier

    @property
    def credential_reference(self) -> str | None:
        """Return the selected non-secret credential reference."""
        return self.profile.credential_reference

    @property
    def action_confirmation_mode(self) -> str:
        """Return the selected OpenHands action-confirmation mode."""
        return self._action_confirmation_mode

    @property
    def continuation_requires_model_authorization(self) -> bool:
        """Return true because OpenHands may call the model after continuing."""
        return True

    def submit_turn(self, *, session_id: str, prompt: str) -> tuple[BackendEvent, ...]:
        """Submit a user task to OpenHands and run to the next stop."""
        if self._pending:
            return (
                BackendEvent(
                    kind=BackendEventKind.ERROR,
                    message="resolve the pending action before submitting another task",
                ),
            )
        self._captured.clear()
        try:
            conversation = self._get_conversation()
            conversation.send_message(prompt, sender="heartwood-user")
            conversation.run()
        except Exception as error:
            return (_backend_error(error),)
        return self._translate_capture(session_id=session_id)

    def restore_pending(self, tool_calls: tuple[ProposedToolCall, ...]) -> None:
        """Restore pending action identity from Heartwood's event log."""
        self._pending = {tool_call.tool_call_id: tool_call for tool_call in tool_calls}

    def resolve_confirmation(
        self,
        *,
        session_id: str,
        tool_call_id: str,
        approved: bool,
    ) -> tuple[BackendEvent, ...]:
        """Resolve the OpenHands pending action set and continue the conversation."""
        pending = self._pending.get(tool_call_id)
        if pending is None:
            return (
                BackendEvent(
                    kind=BackendEventKind.ERROR,
                    message=f"no matching pending action: {tool_call_id}",
                ),
            )
        pending_actions = tuple(self._pending.values())
        self._captured.clear()
        resolved = tuple(
            BackendEvent(
                kind=BackendEventKind.CONFIRMATION_RESOLVED,
                tool_call=action,
                approved=approved,
            )
            for action in pending_actions
        )
        try:
            conversation = self._get_conversation()
        except Exception as error:
            return (_backend_error(error),)
        if not approved:
            try:
                conversation.reject_pending_actions("User rejected the pending action set")
            except Exception as error:
                return (_backend_error(error),)
            self._pending.clear()
            return resolved
        try:
            self._pending.clear()
            conversation.run()
        except Exception as error:
            return (*resolved, _backend_error(error))
        return (*resolved, *self._translate_capture(session_id=session_id))

    def pause(self) -> None:
        """Pause the OpenHands conversation."""
        if self._conversation is not None:
            self._conversation.pause()

    def resume(self, *, session_id: str) -> tuple[BackendEvent, ...]:
        """Resume OpenHands until the next stop."""
        self._captured.clear()
        try:
            self._get_conversation().run()
        except Exception as error:
            return (_backend_error(error),)
        return self._translate_capture(session_id=session_id)

    def close(self) -> None:
        """Release OpenHands conversation resources."""
        if self._conversation is not None:
            self._conversation.close()
            self._conversation = None

    def _get_conversation(self) -> _Conversation:
        conversation = self._conversation
        if conversation is None:
            conversation = self._conversation_factory(self._captured.append)
            self._conversation = conversation
        return conversation

    def _default_conversation_factory(  # pragma: no cover - container integration
        self, callback: Callable[[object], None]
    ) -> _Conversation:
        _configure_upstream_defaults(self.env)
        try:
            sdk = import_module("openhands.sdk")
            skill_module = import_module("openhands.sdk.skills")
            tools_module = import_module("openhands.tools")
            project_file_editor_module = import_module("heartwood.gateway._project_file_editor")
        except ImportError as error:  # pragma: no cover - image includes the pinned extra
            msg = "OpenHands SDK dependencies are not installed"
            raise OpenHandsSdkError(msg) from error

        self.workspace.mkdir(parents=True, exist_ok=True)
        self.persistence_dir.mkdir(parents=True, exist_ok=True)
        skills: list[object] = []
        for skills_dir in (self.skills_dir, *self.additional_skills_dirs):
            if not skills_dir.is_dir():
                continue
            repository, knowledge, agent_skills = skill_module.load_skills_from_dir(skills_dir)
            skills.extend((*repository.values(), *knowledge.values(), *agent_skills.values()))
        api_key = self.profile.resolve_api_key(self.env)
        llm_options: dict[str, Any] = {
            "model": self.profile.model,
            "api_key": "local-model" if self.profile.credential_kind == "none" else api_key,
            "base_url": self.profile.base_url,
            "api_version": self.profile.api_version,
            "aws_region_name": self.profile.aws_region_name,
            "aws_profile_name": self.profile.aws_profile_name,
            "max_input_tokens": self.profile.max_input_tokens,
            "max_output_tokens": self.profile.max_output_tokens,
            "max_message_chars": _llm_max_message_chars(self.profile),
            "log_completions": False,
            **_llm_resilience_options(self.profile),
        }
        if self.profile.is_local:
            llm_options.update(input_cost_per_token=0.0, output_cost_per_token=0.0)
        llm = sdk.LLM(**{key: value for key, value in llm_options.items() if value is not None})
        context = _agent_context(sdk, skills)
        agent = sdk.Agent(
            llm=llm,
            condenser=_context_condenser(sdk, llm, self.profile),
            tools=[
                sdk.Tool(
                    name=tools_module.TerminalTool.name,
                    params=_terminal_tool_params(
                        self.profile,
                        self._credential_environment_names,
                    ),
                ),
                sdk.Tool(
                    name=project_file_editor_module.PROJECT_FILE_EDITOR_SPEC,
                    params={"project_root": str(self.workspace)},
                ),
            ],
            agent_context=context,
            tool_concurrency_limit=1,
        )
        conversation_id = uuid.uuid5(uuid.NAMESPACE_URL, self.conversation_key)
        conversation = sdk.Conversation(
            agent=agent,
            workspace=self.workspace,
            persistence_dir=self.persistence_dir,
            conversation_id=conversation_id,
            callbacks=[callback],
            visualizer=None,
            delete_on_close=False,
        )
        analyzer, confirmation_policy = _security_configuration(self._action_confirmation_mode)
        self._security_analyzer = analyzer
        conversation.set_security_analyzer(analyzer)
        conversation.set_confirmation_policy(confirmation_policy)
        return cast(_Conversation, conversation)

    def _translate_capture(self, *, session_id: str) -> tuple[BackendEvent, ...]:
        translated: list[BackendEvent] = []
        proposed: dict[str, ProposedToolCall] = {}
        observed: set[str] = set()
        for event in self._captured:
            event_name = type(event).__name__
            if event_name == "MessageEvent" and getattr(event, "source", None) == "agent":
                message = _message_text(event)
                if message:
                    translated.append(
                        BackendEvent(kind=BackendEventKind.AGENT_MESSAGE, message=message)
                    )
            elif event_name == "ActionEvent":
                tool_call = _tool_call(
                    event,
                    session_id=session_id,
                    analyzed_risk=_analyzed_risk(self._security_analyzer, event),
                )
                proposed[tool_call.tool_call_id] = tool_call
                translated.append(
                    BackendEvent(kind=BackendEventKind.TOOL_CALL_PROPOSED, tool_call=tool_call)
                )
            elif event_name == "ObservationEvent":
                tool_call_id = str(getattr(event, "tool_call_id", ""))
                if tool_call_id:
                    observed.add(tool_call_id)
                translated.append(_tool_observation(event))
            elif event_name == "UserRejectObservation":
                tool_call_id = str(getattr(event, "tool_call_id", ""))
                if tool_call_id:
                    observed.add(tool_call_id)
            elif event_name in {"AgentErrorEvent", "ConversationErrorEvent"}:
                detail = str(getattr(event, "detail", "OpenHands conversation error"))
                translated.append(BackendEvent(kind=BackendEventKind.ERROR, message=detail))
        pending = {
            tool_call_id: tool_call
            for tool_call_id, tool_call in proposed.items()
            if tool_call_id not in observed
        }
        status = getattr(self._get_conversation().state.execution_status, "value", "")
        if status == "waiting_for_confirmation":
            self._pending = pending
            translated.extend(
                BackendEvent(
                    kind=BackendEventKind.CONFIRMATION_REQUESTED,
                    tool_call=tool_call,
                )
                for tool_call in pending.values()
            )
        return tuple(translated)


def _message_text(event: object) -> str:
    message = getattr(event, "llm_message", None)
    content = getattr(message, "content", ())
    if not isinstance(content, Sequence):
        return ""
    return "\n".join(
        text for item in content if isinstance((text := getattr(item, "text", None)), str) and text
    )


def _tool_call(
    event: object,
    *,
    session_id: str,
    analyzed_risk: str | None = None,
) -> ProposedToolCall:
    tool_call_id = str(getattr(event, "tool_call_id", "")) or f"{session_id}-openhands-action"
    tool_name = str(getattr(event, "tool_name", "unknown-tool"))
    raw_risk = getattr(event, "security_risk", "unknown")
    risk_value = analyzed_risk or str(getattr(raw_risk, "value", raw_risk)).lower()
    risk = risk_value if risk_value in {"low", "medium", "high"} else "unknown"
    summary = getattr(event, "summary", None)
    return ProposedToolCall(
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        risk=cast(Any, risk),
        summary=str(summary) if summary else f"run {tool_name}",
    )


def _analyzed_risk(analyzer: _SecurityAnalyzer | None, event: object) -> str | None:
    if analyzer is None:
        return None
    try:
        raw_risk = analyzer.security_risk(event)
    except Exception:
        return "high"
    return str(getattr(raw_risk, "value", raw_risk)).lower()


def _tool_observation(event: object) -> BackendEvent:
    observation = getattr(event, "observation", event)
    exit_code = getattr(observation, "exit_code", None)
    if not isinstance(exit_code, int):
        metadata = getattr(observation, "metadata", None)
        exit_code = getattr(metadata, "exit_code", None)
    is_error = bool(getattr(observation, "is_error", False))
    resolved_exit_code = exit_code if isinstance(exit_code, int) else (1 if is_error else 0)
    failed = is_error or resolved_exit_code != 0
    tool_name = str(getattr(event, "tool_name", "unknown-tool"))
    return BackendEvent(
        kind=BackendEventKind.TOOL_EXECUTION,
        tool_execution=ToolExecution(
            tool_name=tool_name,
            exit_code=resolved_exit_code,
            summary=f"{tool_name} {'failed' if failed else 'completed'}",
        ),
    )


def _llm_resilience_options(profile: ModelProfile) -> dict[str, int | float]:
    """Bound interactive model retries while allowing transient recovery."""
    return {
        "num_retries": (
            _AGENT_LLM_LOCAL_NUM_RETRIES if profile.is_local else _AGENT_LLM_NUM_RETRIES
        ),
        "retry_max_wait": _AGENT_LLM_RETRY_MAX_WAIT_SECONDS,
        "retry_min_wait": _AGENT_LLM_RETRY_MIN_WAIT_SECONDS,
        "retry_multiplier": _AGENT_LLM_RETRY_MULTIPLIER,
        "timeout": (
            _AGENT_LLM_LOCAL_TIMEOUT_SECONDS if profile.is_local else _AGENT_LLM_TIMEOUT_SECONDS
        ),
    }


def _llm_max_message_chars(profile: ModelProfile) -> int:
    """Keep individual local events useful at the configured input capacity."""
    if profile.max_input_tokens is None:
        return _AGENT_LLM_DEFAULT_MAX_MESSAGE_CHARS
    return max(
        _AGENT_LLM_DEFAULT_MAX_MESSAGE_CHARS,
        profile.max_input_tokens * _AGENT_LLM_ESTIMATED_CHARS_PER_TOKEN,
    )


def _context_condenser(sdk: _SdkModule, llm: _Llm, profile: ModelProfile) -> object:
    """Build OpenHands' native rolling summary within the active input budget."""
    max_tokens = (
        max(1, int(profile.max_input_tokens * _AGENT_CONDENSER_INPUT_FRACTION))
        if profile.max_input_tokens is not None
        else None
    )
    condenser_llm = llm.model_copy(update={"usage_id": "heartwood-condenser", "stream": False})
    condenser_llm.reset_metrics()
    return sdk.LLMSummarizingCondenser(
        llm=condenser_llm,
        max_tokens=max_tokens,
        max_size=_AGENT_CONDENSER_MAX_EVENTS,
        keep_first=_AGENT_CONDENSER_KEEP_FIRST,
    )


def _backend_error(error: Exception) -> BackendEvent:
    return BackendEvent(
        kind=BackendEventKind.ERROR,
        message=f"OpenHands conversation failed: {type(error).__name__}",
    )


def _agent_context(
    sdk: _SdkModule,
    skills: list[object],
) -> object:
    """Build the context from explicitly verified Skills only."""
    return sdk.AgentContext(
        skills=skills,
        load_user_skills=False,
        load_public_skills=False,
        load_project_skills=False,
        system_message_suffix=(
            "Operate only inside the configured project directory. Do not inspect or modify "
            "reserved .heartwood state. An explicitly loaded Skill may read or execute only "
            "the files under the Skill location returned by invoke_skill; never modify that "
            "location or inspect neighboring .heartwood content. Resolve a Skill-relative file "
            "such as scripts/run.py from the returned Skill location, never from the project "
            "directory. Follow Heartwood data-use, egress, and aggregate-export controls."
        ),
    )


def _terminal_tool_params(
    profile: ModelProfile,
    credential_environment_names: Sequence[str] = (),
) -> dict[str, object]:
    """Mask configured environment-referenced provider keys from agent subprocesses."""
    names = set(credential_environment_names)
    if profile.credential_kind == "environment" and profile.api_key_env is not None:
        names.add(profile.api_key_env)
    if not names:
        return {}
    return {"env": dict.fromkeys(sorted(names), "")}


def _security_configuration(
    mode: ActionConfirmationMode,
) -> tuple[_SecurityAnalyzer, object]:
    """Build the pinned OpenHands defense-in-depth analyzer and confirmation policy."""
    security = import_module("openhands.sdk.security")
    analyzer = security.EnsembleSecurityAnalyzer(
        analyzers=[
            security.PolicyRailSecurityAnalyzer(),
            security.PatternSecurityAnalyzer(),
            security.LLMSecurityAnalyzer(),
        ],
        propagate_unknown=True,
    )
    if mode == "always-confirm":
        policy = security.AlwaysConfirm()
    else:
        policy = security.ConfirmRisky(
            threshold=security.SecurityRisk.MEDIUM,
            confirm_unknown=True,
        )
    return analyzer, policy


def _configure_upstream_defaults(env: Mapping[str, str] | None) -> None:
    configured = {} if env is None else env
    for name, default in (
        ("LITELLM_LOCAL_MODEL_COST_MAP", "True"),
        ("LOG_LEVEL", "WARNING"),
        ("OPENHANDS_SUPPRESS_BANNER", "1"),
    ):
        os.environ.setdefault(name, configured.get(name, default))
