# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""OpenHands SDK conversation adapter."""

from __future__ import annotations

# OpenHands reads these settings while its public types are imported.
import asyncio
import hashlib
import json
import os
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from threading import Lock, Thread, current_thread
from typing import Any, TypedDict, cast

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
os.environ.setdefault("LOG_LEVEL", "ERROR")
os.environ.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")

from openhands.sdk import LLM, AgentContext, Conversation, LLMStreamChunk, Tool
from openhands.sdk.conversation import (
    BaseConversation,
    ConversationExecutionStatus,
    ConversationState,
)
from openhands.sdk.event import (
    ActionEvent,
    AgentErrorEvent,
    Event,
    MessageEvent,
    ObservationEvent,
    PauseEvent,
    UserRejectObservation,
)
from openhands.sdk.event.conversation_error import ConversationErrorEvent
from openhands.sdk.llm import Metrics, content_to_str
from openhands.sdk.security import (
    AlwaysConfirm,
    ConfirmRisky,
    EnsembleSecurityAnalyzer,
    LLMSecurityAnalyzer,
    PatternSecurityAnalyzer,
    PolicyRailSecurityAnalyzer,
    SecurityAnalyzerBase,
    SecurityRisk,
)
from openhands.sdk.settings import (
    LLMSummarizingCondenserSettings,
    OpenHandsAgentSettings,
    VerificationSettings,
)
from openhands.sdk.skills import Skill
from openhands.sdk.subagent import (
    agent_definition_to_factory,
    load_agents_from_dir,
    register_agent_if_absent,
)
from openhands.sdk.tool.schema import Observation
from openhands.tools import TaskToolSet, TaskTrackerTool, TerminalTool
from openhands.tools.task import TaskAction, TaskObservation
from openhands.tools.task_tracker import TaskTrackerObservation

from heartwood.core_adapter import (
    BackendAgentMessageEvent,
    BackendConfirmationRequestEvent,
    BackendErrorCode,
    BackendErrorEvent,
    BackendEvent,
    BackendEventSink,
    BackendLifecycle,
    BackendLifecycleEvent,
    BackendSubagent,
    BackendSubagentEvent,
    BackendSubagentStatus,
    BackendTask,
    BackendTaskPlanEvent,
    BackendTaskStatus,
    BackendToolCallEvent,
    BackendToolExecutionEvent,
    BackendUsage,
    BackendUsageEvent,
    PendingActionGroup,
    ProposedToolCall,
    TokenDeltaSink,
    ToolExecution,
    pending_action_group,
)
from heartwood.gateway._model_settings import ModelProfile, ModelSettingsError
from heartwood.schemas import ActionConfirmationMode, JsonValue


class OpenHandsSdkError(RuntimeError):
    """Raised when an OpenHands conversation cannot be configured or run."""


ConversationFactory = Callable[
    [Callable[[Event], None], Callable[[LLMStreamChunk], None]],
    BaseConversation,
]
OpenHandsModules = tuple[Any, Any, Any, Any]

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
_AGENT_MAX_ITERATIONS_PER_RUN = 100
_AGENT_PROGRESS_POLL_SECONDS = 0.1
_AGENT_WORKER_TRANSITION_TIMEOUT_SECONDS = 10
_AGENT_WORKER_SHUTDOWN_TIMEOUT_SECONDS = 30


class _ConversationRuntimeOptions(TypedDict):
    max_iteration_per_run: int
    stuck_detection: bool


def prepare_openhands_sdk(env: Mapping[str, str] | None = None) -> OpenHandsModules:
    """Load the pinned OpenHands runtime with Heartwood's upstream defaults."""
    _configure_upstream_defaults(env)
    from openhands import sdk, tools
    from openhands.sdk import skills

    from heartwood.gateway import _project_file_editor

    return sdk, skills, tools, _project_file_editor


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
        agents_dir: Path | None = None,
        credential_environment_names: Sequence[str] = (),
        action_confirmation_mode: ActionConfirmationMode = "always-confirm",
        env: Mapping[str, str] | None = None,
        llm_extra_body: Mapping[str, object] | None = None,
        native_tool_calling: bool | None = None,
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
        self.agents_dir = None if agents_dir is None else agents_dir.resolve()
        self.persistence_dir = persistence_dir.resolve()
        self.conversation_key = conversation_key
        self._credential_environment_names = tuple(sorted(set(credential_environment_names)))
        self.env = env
        self._llm_extra_body = dict(llm_extra_body or {})
        self._native_tool_calling = native_tool_calling
        self._security_analyzer: SecurityAnalyzerBase | None = None
        self._conversation_factory = conversation_factory or self._default_conversation_factory
        self._conversation: BaseConversation | None = None
        self._event_sink: BackendEventSink = lambda _events: None
        self._token_sink: TokenDeltaSink = lambda _delta: None
        self._run_thread: Thread | None = None
        self._worker_threads: set[Thread] = set()
        self._run_lock = Lock()
        self._run_failed = False

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

    def bind_runtime(
        self,
        *,
        event_sink: BackendEventSink,
        token_sink: TokenDeltaSink,
    ) -> None:
        """Bind the gateway-owned durable and transient runtime sinks."""
        self._event_sink = event_sink
        self._token_sink = token_sink

    def reconcile(
        self,
        *,
        session_id: str,
        known_source_event_ids: frozenset[str],
    ) -> tuple[BackendEvent, ...]:
        """Translate persisted OpenHands state not yet committed by Heartwood."""
        try:
            conversation = self._get_conversation()
        except Exception as error:
            backend_error = _backend_error(
                error,
                source_event_id=self._error_source("conversation-unavailable"),
            )
            return (
                () if backend_error.source_event_id in known_source_event_ids else (backend_error,)
            )
        translated = [
            backend_event
            for event in _conversation_state(conversation).active_branch()
            for backend_event in self._translate_event(event, session_id=session_id)
            if backend_event.source_event_id not in known_source_event_ids
        ]
        translated.extend(
            event
            for event in self._state_events()
            if event.source_event_id not in known_source_event_ids
        )
        return tuple(translated)

    def pending_action_group(
        self,
        *,
        session_id: str,  # noqa: ARG002
    ) -> PendingActionGroup | None:
        """Return the atomic unmatched executable action group from OpenHands state."""
        try:
            conversation = self._get_conversation()
        except Exception:
            return None
        return pending_action_group(
            tuple(
                _tool_call(
                    event,
                    analyzed_risk=_analyzed_risk(self._security_analyzer, event),
                )
                for event in ConversationState.get_unmatched_actions(
                    _conversation_state(conversation).active_branch()
                )
            )
        )

    def submit_turn(self, *, session_id: str, prompt: str) -> tuple[BackendEvent, ...]:
        """Submit a user task and start or steer the active OpenHands run."""
        if self.pending_action_group(session_id=session_id) is not None:
            return (
                BackendErrorEvent(
                    error_code=BackendErrorCode.INVALID_STATE,
                ),
            )
        try:
            conversation = self._get_conversation()
            conversation.send_message(prompt, sender="heartwood-user")
        except Exception as error:
            return (
                _backend_error(error),
            )
        if self._run_active():
            return ()
        if not self._start_run(session_id=session_id):
            return (
                BackendErrorEvent(
                    error_code=BackendErrorCode.INVALID_STATE,
                ),
            )
        return (
            BackendLifecycleEvent(
                lifecycle=BackendLifecycle.RUNNING,
                source_event_id=f"heartwood-run:{uuid.uuid4()}",
            ),
        )

    def resolve_confirmation(
        self,
        *,
        session_id: str,
        action_group_id: str,
        approved: bool,
    ) -> tuple[BackendEvent, ...]:
        """Resolve the complete OpenHands pending action set."""
        pending_group = self.pending_action_group(session_id=session_id)
        if pending_group is None or pending_group.group_id != action_group_id:
            return (
                BackendErrorEvent(
                    error_code=BackendErrorCode.INVALID_STATE,
                ),
            )
        try:
            conversation = self._get_conversation()
        except Exception as error:
            return (
                _backend_error(error),
            )
        if not approved:
            try:
                conversation.reject_pending_actions("User rejected the pending action set")
            except Exception as error:
                return (
                    _backend_error(error),
                )
            return self._state_events()
        if not self._wait_for_run_boundary(_AGENT_WORKER_TRANSITION_TIMEOUT_SECONDS):
            return (
                BackendErrorEvent(
                    error_code=BackendErrorCode.WORKER_STOPPED,
                ),
            )
        subagents = self._subagent_events_for_unmatched_actions(
            session_id=session_id,
            status=BackendSubagentStatus.RUNNING,
        )
        if not self._start_run(session_id=session_id):
            return (
                BackendErrorEvent(
                    error_code=BackendErrorCode.INVALID_STATE,
                ),
            )
        return (
            *subagents,
            BackendLifecycleEvent(
                lifecycle=BackendLifecycle.RUNNING,
                source_event_id=f"heartwood-run:{uuid.uuid4()}",
            ),
        )

    def pause(self) -> None:
        """Interrupt active OpenHands I/O and leave the conversation resumable."""
        conversation = self._conversation
        if conversation is not None and self._run_active():
            conversation.interrupt()

    def resume(self, *, session_id: str) -> tuple[BackendEvent, ...]:
        """Resume OpenHands in the background."""
        conversation = self._get_conversation()
        if self._run_active():
            execution_status = _execution_status(conversation)
            if execution_status == BackendLifecycle.RUNNING.value:
                conversation.interrupt()
            if not self._wait_for_run_boundary(_AGENT_WORKER_TRANSITION_TIMEOUT_SECONDS):
                return (
                    BackendErrorEvent(
                        error_code=BackendErrorCode.WORKER_STOPPED,
                    ),
                )
        if (
            _conversation_state(conversation).execution_status
            == ConversationExecutionStatus.RUNNING
        ):
            conversation.interrupt()
        if _execution_status(conversation) not in {
            BackendLifecycle.PAUSED.value,
            BackendLifecycle.IDLE.value,
        }:
            return (
                BackendErrorEvent(
                    error_code=BackendErrorCode.INVALID_STATE,
                ),
            )
        if not self._start_run(session_id=session_id):
            return (
                BackendErrorEvent(
                    error_code=BackendErrorCode.INVALID_STATE,
                ),
            )
        return (
            BackendLifecycleEvent(
                lifecycle=BackendLifecycle.RUNNING,
                source_event_id=f"heartwood-run:{uuid.uuid4()}",
            ),
        )

    def close(self) -> None:
        """Release OpenHands conversation resources."""
        if self._conversation is not None:
            if self._run_active():
                self._conversation.interrupt()
            if not self._wait_for_workers_exit(_AGENT_WORKER_SHUTDOWN_TIMEOUT_SECONDS):
                raise OpenHandsSdkError(
                    "OpenHands worker did not stop; session ownership remains active"
                )
            self._conversation.close()
            self._conversation = None

    def _get_conversation(self) -> BaseConversation:
        conversation = self._conversation
        if conversation is None:
            conversation = self._conversation_factory(
                self._handle_sdk_event,
                self._handle_token,
            )
            self._conversation = conversation
        return conversation

    def _default_conversation_factory(  # pragma: no cover - container integration
        self,
        callback: Callable[[Event], None],
        token_callback: Callable[[LLMStreamChunk], None],
    ) -> BaseConversation:
        _configure_upstream_defaults(self.env)
        from openhands.sdk.skills import load_skills_from_dir

        from heartwood.gateway._project_file_editor import PROJECT_FILE_EDITOR_SPEC

        self.workspace.mkdir(parents=True, exist_ok=True)
        self.persistence_dir.mkdir(parents=True, exist_ok=True)
        skills: list[Skill] = []
        for skills_dir in (self.skills_dir, *self.additional_skills_dirs):
            if not skills_dir.is_dir():
                continue
            repository, knowledge, agent_skills = load_skills_from_dir(skills_dir)
            skills.extend((*repository.values(), *knowledge.values(), *agent_skills.values()))
        self._register_specialized_agents()
        api_key = self.profile.resolve_api_key(self.env)
        llm = LLM(
            **_llm_options(
                self.profile,
                api_key=api_key,
                extra_body=self._llm_extra_body,
                native_tool_calling=self._native_tool_calling,
            )
        )
        settings = _agent_settings(
            llm=llm,
            tools=[
                Tool(
                    name=TerminalTool.name,
                    params=_terminal_tool_params(
                        self.profile,
                        self._credential_environment_names,
                    ),
                ),
                Tool(
                    name=PROJECT_FILE_EDITOR_SPEC,
                    params={"project_root": str(self.workspace)},
                ),
                Tool(name=TaskTrackerTool.name),
                Tool(name=TaskToolSet.name),
            ],
            context=_agent_context(skills),
            condenser=_context_condenser_settings(self.profile),
        )
        agent = settings.create_agent()
        conversation_id = uuid.uuid5(uuid.NAMESPACE_URL, self.conversation_key)
        conversation = Conversation(
            agent=agent,
            workspace=self.workspace,
            persistence_dir=self.persistence_dir,
            conversation_id=conversation_id,
            callbacks=[callback],
            token_callbacks=[token_callback],
            **_conversation_runtime_options(),
            visualizer=None,
            delete_on_close=False,
        )
        analyzer, confirmation_policy = _security_configuration(self._action_confirmation_mode)
        self._security_analyzer = analyzer
        conversation.set_security_analyzer(analyzer)
        conversation.set_confirmation_policy(confirmation_policy)
        return conversation

    def _register_specialized_agents(self) -> None:
        if self.agents_dir is None:
            return
        for definition in load_agents_from_dir(self.agents_dir):
            if definition.tools:
                raise OpenHandsSdkError(
                    f"specialized agent {definition.name} must be tool-free until "
                    "Heartwood supports audited child-action confirmation"
                )
            register_agent_if_absent(
                name=definition.name,
                factory_func=agent_definition_to_factory(
                    definition,
                    work_dir=self.workspace,
                ),
                description=definition,
            )

    def _handle_sdk_event(self, event: Event) -> None:  # noqa: ARG002
        """Leave durable translation to the persisted OpenHands state.

        OpenHands invokes this callback before its own persistence callback.
        Durable Heartwood translation therefore occurs only from conversation
        state after the run reaches a stable boundary.
        """
        return None

    def _handle_token(self, chunk: LLMStreamChunk) -> None:
        if not chunk.choices:
            return
        content = chunk.choices[0].delta.content
        if isinstance(content, str) and content:
            self._token_sink(content)

    def _run_active(self) -> bool:
        thread = self._run_thread
        return thread is not None and thread.is_alive()

    def _start_run(self, *, session_id: str) -> bool:
        with self._run_lock:
            if self._run_active():
                return False
            self._run_failed = False
            thread = Thread(
                target=self._run,
                kwargs={"session_id": session_id},
                name=f"heartwood-openhands-{session_id}",
                daemon=True,
            )
            self._run_thread = thread
            self._worker_threads.add(thread)
            thread.start()
            return True

    def _run(self, *, session_id: str) -> None:
        failure: tuple[BackendEvent, ...] = ()
        try:
            asyncio.run(self._run_until_stable(session_id=session_id))
        except Exception as error:
            with self._run_lock:
                self._run_failed = True
            failure = (
                _backend_error(
                    error,
                    source_event_id=self._error_source(f"worker:{uuid.uuid4()}"),
                ),
            )
        finally:
            worker = current_thread()
            with self._run_lock:
                if self._run_thread is worker:
                    self._run_thread = None
            try:
                if failure:
                    self._event_sink(failure)
                self._event_sink(
                    self.reconcile(
                        session_id=session_id,
                        known_source_event_ids=frozenset(),
                    )
                )
            finally:
                with self._run_lock:
                    self._worker_threads.discard(worker)

    async def _run_until_stable(self, *, session_id: str) -> None:
        """Run OpenHands while publishing newly persisted non-token progress."""
        run = asyncio.create_task(self._get_conversation().arun())
        while not run.done():
            done, _pending = await asyncio.wait(
                {run},
                timeout=_AGENT_PROGRESS_POLL_SECONDS,
            )
            if run not in done:
                self._event_sink(
                    self.reconcile(
                        session_id=session_id,
                        known_source_event_ids=frozenset(),
                    )
                )
        await run

    def _wait_for_run_boundary(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while self._run_active() and time.monotonic() < deadline:
            time.sleep(0.01)
        return not self._run_active()

    def _wait_for_workers_exit(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            with self._run_lock:
                workers = tuple(
                    worker
                    for worker in self._worker_threads
                    if worker is not current_thread() and worker.is_alive()
                )
            if not workers:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            workers[0].join(timeout=remaining)

    def _error_source(self, scope: str) -> str:
        conversation_digest = hashlib.sha256(self.conversation_key.encode("utf-8")).hexdigest()[:16]
        return f"heartwood-error:{conversation_digest}:{scope}"

    def _subagent_events_for_unmatched_actions(
        self,
        *,
        session_id: str,
        status: BackendSubagentStatus,
    ) -> tuple[BackendEvent, ...]:
        conversation = self._get_conversation()
        return tuple(
            BackendSubagentEvent(
                subagent=BackendSubagent(
                    invocation_id=event.tool_call_id,
                    task_id=None,
                    agent_name=event.action.subagent_type,
                    status=status,
                    parent_session_id=session_id,
                    parent_action_id=event.id,
                ),
                source_event_id=f"openhands:{event.id}:subagent:{status.value}",
            )
            for event in ConversationState.get_unmatched_actions(
                _conversation_state(conversation).active_branch()
            )
            if isinstance(event.action, TaskAction)
        )

    def _translate_event(
        self,
        event: Event,
        *,
        session_id: str,
    ) -> tuple[BackendEvent, ...]:
        source = f"openhands:{event.id}"
        if isinstance(event, MessageEvent):
            if event.source != "agent":
                return ()
            message = _message_text(event)
            return (
                (
                    BackendAgentMessageEvent(
                        message=message,
                        source_event_id=f"{source}:message",
                    ),
                )
                if message
                else ()
            )
        if isinstance(event, ActionEvent):
            if event.action is None:
                return ()
            tool_call = _tool_call(
                event,
                analyzed_risk=_analyzed_risk(self._security_analyzer, event),
            )
            translated: list[BackendEvent] = [
                BackendToolCallEvent(
                    tool_call=tool_call,
                    source_event_id=f"{source}:proposal",
                )
            ]
            if isinstance(event.action, TaskAction):
                translated.append(
                    BackendSubagentEvent(
                        subagent=BackendSubagent(
                            invocation_id=event.tool_call_id,
                            task_id=None,
                            agent_name=event.action.subagent_type,
                            status=BackendSubagentStatus.PROPOSED,
                            parent_session_id=session_id,
                            parent_action_id=event.id,
                        ),
                        source_event_id=f"{source}:subagent",
                    )
                )
            return tuple(translated)
        if isinstance(event, ObservationEvent):
            observation_events: list[BackendEvent] = [
                _tool_observation(
                    event,
                    source_event_id=f"{source}:observation",
                )
            ]
            if isinstance(event.observation, TaskTrackerObservation):
                observation_events.append(
                    BackendTaskPlanEvent(
                        tasks=tuple(
                            BackendTask(
                                title=task.title,
                                status=_task_status(task.status),
                            )
                            for task in event.observation.task_list
                        ),
                        source_event_id=f"{source}:task-plan",
                    )
                )
            if isinstance(event.observation, TaskObservation):
                observation_events.append(
                    BackendSubagentEvent(
                        subagent=BackendSubagent(
                            invocation_id=event.tool_call_id,
                            task_id=event.observation.task_id,
                            agent_name=event.observation.subagent,
                            status=(
                                BackendSubagentStatus.ERROR
                                if event.observation.is_error
                                else BackendSubagentStatus.COMPLETED
                            ),
                            parent_session_id=session_id,
                            parent_action_id=event.action_id,
                        ),
                        source_event_id=f"{source}:subagent",
                    )
                )
            return tuple(observation_events)
        if isinstance(event, UserRejectObservation):
            return ()
        if isinstance(event, AgentErrorEvent):
            return (
                BackendErrorEvent(
                    error_code=BackendErrorCode.ACTION_FAILED,
                    source_event_id=f"{source}:error",
                ),
            )
        if isinstance(event, ConversationErrorEvent):
            return (
                BackendErrorEvent(
                    error_code=BackendErrorCode.CONVERSATION_STOPPED,
                    source_event_id=f"{source}:error",
                ),
            )
        if isinstance(event, PauseEvent):
            return (
                BackendLifecycleEvent(
                    lifecycle=BackendLifecycle.PAUSED,
                    source_event_id=f"{source}:lifecycle",
                ),
            )
        return ()

    def _state_events(self) -> tuple[BackendEvent, ...]:
        conversation = self._get_conversation()
        state = _conversation_state(conversation)
        branch = state.active_branch()
        anchor = branch[-1].id if branch else str(conversation.id)
        unmatched_actions = ConversationState.get_unmatched_actions(branch)
        unmatched_group = pending_action_group(
            tuple(
                _tool_call(
                    action,
                    analyzed_risk=_analyzed_risk(self._security_analyzer, action),
                )
                for action in unmatched_actions
            )
        )
        lifecycle = _backend_lifecycle(state.execution_status)
        with self._run_lock:
            run_failed = self._run_failed
            run_active = self._run_active()
        action_outcome_unknown = (
            state.execution_status == ConversationExecutionStatus.RUNNING
            and not run_active
            and unmatched_group is not None
        )
        if run_failed or action_outcome_unknown:
            lifecycle = BackendLifecycle.ERROR
        elif (
            state.execution_status == ConversationExecutionStatus.RUNNING and not run_active
        ):
            lifecycle = BackendLifecycle.PAUSED
        events: list[BackendEvent] = [
            BackendLifecycleEvent(
                lifecycle=lifecycle,
                source_event_id=(f"openhands-state:{anchor}:{lifecycle.value}:lifecycle"),
            )
        ]
        if action_outcome_unknown and unmatched_group is not None:
            events.append(
                BackendErrorEvent(
                    error_code=BackendErrorCode.ACTION_OUTCOME_UNKNOWN,
                    source_event_id=(
                        f"openhands-state:{anchor}:"
                        f"{unmatched_group.group_id}:action-outcome-unknown"
                    ),
                )
            )
        if state.execution_status == ConversationExecutionStatus.WAITING_FOR_CONFIRMATION:
            if unmatched_group is None:  # pragma: no cover - SDK state contract
                raise OpenHandsSdkError(
                    "OpenHands is waiting for confirmation without unmatched actions"
                )
            events.extend(
                BackendConfirmationRequestEvent(
                    tool_call=_tool_call(
                        action,
                        analyzed_risk=_analyzed_risk(self._security_analyzer, action),
                    ),
                    action_group_id=unmatched_group.group_id,
                    source_event_id=f"openhands:{action.id}:confirmation",
                )
                for action in unmatched_actions
            )
        for usage in _usage(state):
            events.append(
                BackendUsageEvent(
                    usage=usage,
                    source_event_id=_usage_source_event_id(anchor, usage),
                )
            )
        return tuple(events)


def _message_text(event: MessageEvent) -> str:
    return "\n".join(part for part in content_to_str(event.llm_message.content) if part)


def _tool_call(
    event: ActionEvent,
    *,
    analyzed_risk: str | None = None,
) -> ProposedToolCall:
    tool_name = event.tool_name or "unknown-tool"
    risk_value = analyzed_risk or event.security_risk.value.lower()
    risk = risk_value if risk_value in {"low", "medium", "high"} else "unknown"
    return ProposedToolCall(
        tool_call_id=event.tool_call_id,
        tool_name=tool_name,
        risk=cast(Any, risk),
        summary=event.summary or f"run {tool_name}",
        arguments=_tool_arguments(event),
    )


def _tool_arguments(event: ActionEvent) -> dict[str, JsonValue]:
    """Return the exact structured action arguments supplied by OpenHands."""
    raw: object = event.tool_call.arguments
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {"raw": str(raw)}
    if isinstance(raw, Mapping):
        return _json_mapping(raw)
    if event.action is not None:
        return _json_mapping(event.action.model_dump(mode="json"))
    return {}


def _json_mapping(value: object) -> dict[str, JsonValue]:
    normalized = json.loads(json.dumps(value, default=str))
    return cast(dict[str, JsonValue], normalized) if isinstance(normalized, dict) else {}


def _conversation_state(conversation: BaseConversation) -> ConversationState:
    return cast(ConversationState, conversation.state)


def _execution_status(conversation: BaseConversation) -> str:
    return _conversation_state(conversation).execution_status.value


def _analyzed_risk(
    analyzer: SecurityAnalyzerBase | None,
    event: ActionEvent,
) -> str | None:
    if analyzer is None:
        return None
    try:
        raw_risk = analyzer.security_risk(event)
    except Exception:
        return "high"
    return raw_risk.value.lower()


def _tool_observation(
    event: ObservationEvent,
    *,
    source_event_id: str,
) -> BackendEvent:
    observation = event.observation
    exit_code = _observation_exit_code(observation)
    is_error = observation.is_error
    resolved_exit_code = exit_code if isinstance(exit_code, int) else (1 if is_error else 0)
    failed = is_error or resolved_exit_code != 0
    tool_name = event.tool_name or "unknown-tool"
    return BackendToolExecutionEvent(
        tool_execution=ToolExecution(
            tool_name=tool_name,
            exit_code=resolved_exit_code,
            summary=f"{tool_name} {'failed' if failed else 'completed'}",
        ),
        source_event_id=source_event_id,
    )


def _observation_exit_code(observation: Observation) -> int | None:
    payload = observation.model_dump(mode="json")
    direct = payload.get("exit_code")
    if isinstance(direct, int) and not isinstance(direct, bool):
        return direct
    metadata = payload.get("metadata")
    if isinstance(metadata, Mapping):
        nested = metadata.get("exit_code")
        if isinstance(nested, int) and not isinstance(nested, bool):
            return nested
    return None


def _backend_lifecycle(status: ConversationExecutionStatus) -> BackendLifecycle:
    return {
        ConversationExecutionStatus.IDLE: BackendLifecycle.IDLE,
        ConversationExecutionStatus.RUNNING: BackendLifecycle.RUNNING,
        ConversationExecutionStatus.PAUSED: BackendLifecycle.PAUSED,
        ConversationExecutionStatus.WAITING_FOR_CONFIRMATION: (
            BackendLifecycle.WAITING_FOR_CONFIRMATION
        ),
        ConversationExecutionStatus.FINISHED: BackendLifecycle.FINISHED,
        ConversationExecutionStatus.ERROR: BackendLifecycle.ERROR,
        ConversationExecutionStatus.STUCK: BackendLifecycle.ERROR,
        ConversationExecutionStatus.DELETING: BackendLifecycle.ERROR,
    }[status]


def _task_status(status: str) -> BackendTaskStatus:
    return {
        "todo": BackendTaskStatus.TODO,
        "in_progress": BackendTaskStatus.IN_PROGRESS,
        "done": BackendTaskStatus.DONE,
    }[status]


def _usage(state: ConversationState) -> tuple[BackendUsage, ...]:
    by_purpose = tuple(
        _usage_snapshot(usage_id, metrics)
        for usage_id, metrics in sorted(state.stats.usage_to_metrics.items())
    )
    if not by_purpose:
        return ()
    combined = _usage_snapshot("total", state.stats.get_combined_metrics())
    return (combined, *by_purpose)


def _usage_snapshot(usage_id: str, metrics: Metrics) -> BackendUsage:
    snapshot = metrics.get_snapshot()
    tokens = snapshot.accumulated_token_usage
    return BackendUsage(
        usage_id=usage_id,
        model_name=snapshot.model_name,
        call_count=len(metrics.token_usages),
        prompt_tokens=tokens.prompt_tokens if tokens is not None else 0,
        completion_tokens=tokens.completion_tokens if tokens is not None else 0,
        cache_read_tokens=tokens.cache_read_tokens if tokens is not None else 0,
        cache_write_tokens=tokens.cache_write_tokens if tokens is not None else 0,
        reasoning_tokens=tokens.reasoning_tokens if tokens is not None else 0,
        context_window=(tokens.context_window or None) if tokens is not None else None,
        accumulated_cost=snapshot.accumulated_cost,
    )


def _usage_source_event_id(anchor: str, usage: BackendUsage) -> str:
    payload = {
        "accumulated_cost": usage.accumulated_cost,
        "cache_read_tokens": usage.cache_read_tokens,
        "cache_write_tokens": usage.cache_write_tokens,
        "call_count": usage.call_count,
        "completion_tokens": usage.completion_tokens,
        "context_window": usage.context_window,
        "model_name": usage.model_name,
        "prompt_tokens": usage.prompt_tokens,
        "reasoning_tokens": usage.reasoning_tokens,
        "usage_id": usage.usage_id,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f"openhands-state:{anchor}:usage:{usage.usage_id}:{digest}"


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


def _llm_options(
    profile: ModelProfile,
    *,
    api_key: str | None,
    extra_body: Mapping[str, object],
    native_tool_calling: bool | None = None,
) -> dict[str, Any]:
    """Build the complete OpenHands LLM configuration for one model profile."""
    options: dict[str, Any] = {
        "model": profile.model,
        "usage_id": "agent",
        "api_key": "local-model" if profile.credential_kind == "none" else api_key,
        "base_url": profile.base_url,
        "api_version": profile.api_version,
        "aws_region_name": profile.aws_region_name,
        "aws_profile_name": profile.aws_profile_name,
        "max_input_tokens": profile.max_input_tokens,
        "max_output_tokens": profile.max_output_tokens,
        "max_message_chars": _llm_max_message_chars(profile),
        "log_completions": False,
        "stream": True,
        "litellm_extra_body": dict(extra_body) or None,
        "native_tool_calling": native_tool_calling,
        **_llm_resilience_options(profile),
    }
    if profile.is_local:
        options.update(input_cost_per_token=0.0, output_cost_per_token=0.0)
    return {key: value for key, value in options.items() if value is not None}


def _llm_max_message_chars(profile: ModelProfile) -> int:
    """Keep individual local events useful at the configured input capacity."""
    if profile.max_input_tokens is None:
        return _AGENT_LLM_DEFAULT_MAX_MESSAGE_CHARS
    return max(
        _AGENT_LLM_DEFAULT_MAX_MESSAGE_CHARS,
        profile.max_input_tokens * _AGENT_LLM_ESTIMATED_CHARS_PER_TOKEN,
    )


def _context_condenser_settings(
    profile: ModelProfile,
) -> LLMSummarizingCondenserSettings:
    """Build OpenHands' supported rolling-summary settings."""
    max_tokens = (
        max(1, int(profile.max_input_tokens * _AGENT_CONDENSER_INPUT_FRACTION))
        if profile.max_input_tokens is not None
        else None
    )
    return LLMSummarizingCondenserSettings(
        max_tokens=max_tokens,
        max_size=_AGENT_CONDENSER_MAX_EVENTS,
        keep_first=_AGENT_CONDENSER_KEEP_FIRST,
    )


def _agent_settings(
    *,
    llm: LLM,
    tools: list[Tool],
    context: AgentContext,
    condenser: LLMSummarizingCondenserSettings,
) -> OpenHandsAgentSettings:
    """Return Heartwood's explicit OpenHands agent runtime contract."""
    return OpenHandsAgentSettings(
        llm=llm,
        tools=tools,
        agent_context=context,
        condenser=condenser,
        enable_sub_agents=True,
        enable_switch_llm_tool=False,
        tool_concurrency_limit=1,
        mcp_config={},
        verification=VerificationSettings(
            critic_enabled=False,
            enable_iterative_refinement=False,
        ),
    )


def _conversation_runtime_options() -> _ConversationRuntimeOptions:
    """Return bounded conversation options that must not drift with SDK defaults."""
    return {
        "max_iteration_per_run": _AGENT_MAX_ITERATIONS_PER_RUN,
        "stuck_detection": True,
    }


def _backend_error(
    _error: Exception,
    *,
    source_event_id: str | None = None,
) -> BackendEvent:
    return BackendErrorEvent(
        error_code=BackendErrorCode.WORKER_STOPPED,
        source_event_id=source_event_id,
    )


def _agent_context(
    skills: list[Skill],
) -> AgentContext:
    """Build the context from explicitly verified Skills only."""
    return AgentContext(
        skills=skills,
        load_user_skills=False,
        load_public_skills=False,
        load_project_skills=False,
        system_message_suffix=(
            "Operate only inside the configured project directory. Do not inspect or modify "
            "reserved .heartwood state. Skills are context resources, not tools named after "
            "their identifiers. Activate a Skill only through the OpenHands invoke_skill tool "
            "with its exact Skill identifier. An explicitly loaded Skill may read or execute "
            "only the files under the Skill location returned by invoke_skill; never modify "
            "that location or inspect neighboring .heartwood content. Resolve a Skill-relative "
            "file such as scripts/run.py from the returned Skill location, never from the "
            "project directory. Follow Heartwood data-use, egress, and "
            "aggregate-export controls. Treat a tool exit code of zero as success even when "
            "the tool returns no text. Never install a dependency solely because a successful "
            "tool action returned no text."
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
) -> tuple[SecurityAnalyzerBase, AlwaysConfirm | ConfirmRisky]:
    """Build the pinned OpenHands defense-in-depth analyzer and confirmation policy."""
    analyzer = EnsembleSecurityAnalyzer(
        analyzers=[
            PolicyRailSecurityAnalyzer(),
            PatternSecurityAnalyzer(),
            LLMSecurityAnalyzer(),
        ],
        propagate_unknown=True,
    )
    policy: AlwaysConfirm | ConfirmRisky
    if mode == "always-confirm":
        policy = AlwaysConfirm()
    else:
        policy = ConfirmRisky(
            threshold=SecurityRisk.MEDIUM,
            confirm_unknown=True,
        )
    return analyzer, policy


def _configure_upstream_defaults(env: Mapping[str, str] | None) -> None:
    configured = {} if env is None else env
    for name, default in (
        ("LITELLM_LOCAL_MODEL_COST_MAP", "True"),
        ("LOG_LEVEL", "ERROR"),
        ("OPENHANDS_SUPPRESS_BANNER", "1"),
    ):
        os.environ.setdefault(name, configured.get(name, default))
