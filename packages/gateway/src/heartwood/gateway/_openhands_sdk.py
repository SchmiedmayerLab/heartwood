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
from contextlib import suppress
from pathlib import Path
from threading import Event as ThreadEvent
from threading import Lock, RLock, Thread, current_thread
from typing import Any, Literal, TypedDict, cast

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
    ObservationBaseEvent,
    ObservationEvent,
    PauseEvent,
    UserRejectObservation,
)
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
    AgentDefinition,
    agent_definition_to_factory,
    load_agents_from_dir,
    register_agent_if_absent,
)
from openhands.sdk.tool.schema import Observation
from openhands.tools import TaskToolSet, TaskTrackerTool, TerminalTool
from openhands.tools.file_editor import FileEditorAction
from openhands.tools.task import TaskAction, TaskObservation
from openhands.tools.task_tracker import TaskTrackerObservation
from openhands.tools.terminal import TerminalAction

from heartwood.core_adapter import (
    BackendAgentMessageEvent,
    BackendConfirmationRequestEvent,
    BackendConfirmationResolutionEvent,
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
from heartwood.gateway._openhands_models import (
    OpenHandsModelError,
    request_endpoint_for_model,
)
from heartwood.gateway._subscriptions import (
    OpenHandsOpenAISubscription,
    SubscriptionError,
    create_openai_subscription_llm,
)
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
_AGENT_PROGRESS_POLL_SECONDS = 0.25
_AGENT_WORKER_TRANSITION_TIMEOUT_SECONDS = 10
_AGENT_WORKER_SHUTDOWN_TIMEOUT_SECONDS = 30
_MAX_TOOL_RESULT_CHARS = 16_000
_MAX_TOOL_RESULT_LINES = 240
_OPENHANDS_FINISH_TOOL_NAME = "finish"
_SPECIALIST_REGISTRATION_LOCK = Lock()
_REGISTERED_HEARTWOOD_SPECIALISTS: dict[str, AgentDefinition] = {}


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
        self._conversation_lock = RLock()
        self._conversation_closing = False
        self._closed = False
        self._event_sink: BackendEventSink = lambda _events: None
        self._token_sink: TokenDeltaSink = lambda _delta: None
        self._run_thread: Thread | None = None
        self._worker_threads: set[Thread] = set()
        self._run_lock = Lock()
        self._execution_active = False
        self._run_failed = False
        self._run_cancelled = ThreadEvent()
        self._agent_loop: asyncio.AbstractEventLoop | None = None
        self._agent_task: asyncio.Task[Any] | None = None
        self._agent_started = False
        self._view_repair_lock = Lock()
        self._view_repair_tool_call_ids: set[str] = set()
        self._view_repair_boundary_reached = False
        self._view_repair_paused_internally = False

    @property
    def backend_id(self) -> str:
        """Return the backend id."""
        return "openhands-sdk"

    @property
    def configuration_error(self) -> str | None:
        """Return a safe preflight error before recording a route decision."""
        try:
            request_endpoint = request_endpoint_for_model(
                self.profile.model,
                self.profile.policy_endpoint,
            )
        except OpenHandsModelError:
            return "OpenHands could not inspect the active model request path"
        if request_endpoint != self.profile.policy_endpoint:
            return "The active model profile request path does not match OpenHands"
        if self.profile.auth_type == "subscription":
            try:
                if not OpenHandsOpenAISubscription().credential_available():
                    return "ChatGPT sign-in is required for the active model profile"
            except SubscriptionError:
                return "OpenHands could not inspect the active ChatGPT sign-in"
            return None
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
        return self._reconcile_conversation(
            conversation,
            session_id=session_id,
            known_source_event_ids=known_source_event_ids,
        )

    def _reconcile_conversation(
        self,
        conversation: BaseConversation,
        *,
        session_id: str,
        known_source_event_ids: frozenset[str],
    ) -> tuple[BackendEvent, ...]:
        """Translate one already-owned conversation without reopening it."""
        branch = tuple(_conversation_state(conversation).active_branch())
        actions_by_id, action_groups = self._action_resolution_context(branch)
        translated: list[BackendEvent] = []
        seen_source_event_ids = set(known_source_event_ids)
        for event in branch:
            for backend_event in self._translate_event(
                event,
                session_id=session_id,
                actions_by_id=actions_by_id,
                action_groups=action_groups,
            ):
                if backend_event.source_event_id in seen_source_event_ids:
                    continue
                translated.append(backend_event)
                if backend_event.source_event_id is not None:
                    seen_source_event_ids.add(backend_event.source_event_id)
        for state_event in self._state_events(conversation):
            if state_event.source_event_id in seen_source_event_ids:
                continue
            translated.append(state_event)
            if state_event.source_event_id is not None:
                seen_source_event_ids.add(state_event.source_event_id)
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
        state = _conversation_state(conversation)
        if state.execution_status != ConversationExecutionStatus.WAITING_FOR_CONFIRMATION:
            return None
        return self._unmatched_action_group(conversation)

    def submit_turn(self, *, session_id: str, prompt: str) -> tuple[BackendEvent, ...]:
        """Submit a user task and start or steer the active OpenHands run."""
        try:
            conversation = self._get_conversation()
        except Exception as error:
            return (_backend_error(error),)
        if (outcome_error := self._interrupted_outcome_error(conversation)) is not None:
            return (
                BackendErrorEvent(
                    error_code=outcome_error,
                ),
            )
        if self.pending_action_group(session_id=session_id) is not None:
            return (
                BackendErrorEvent(
                    error_code=BackendErrorCode.INVALID_STATE,
                ),
            )
        if self._run_active() and not self._execution_in_flight():
            return (
                BackendErrorEvent(
                    error_code=BackendErrorCode.INVALID_STATE,
                ),
            )
        try:
            conversation.send_message(prompt, sender="heartwood-user")
        except Exception as error:
            return (_backend_error(error),)
        if self._run_active():
            return ()
        if not self._start_run(session_id=session_id, conversation=conversation):
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
            return (_backend_error(error),)
        if not approved:
            try:
                conversation.reject_pending_actions("User rejected the pending action set")
                _conversation_state(conversation).rebuild_view()
            except Exception as error:
                return (_backend_error(error),)
            return (
                *self._confirmation_resolution_events(
                    pending_group,
                    approved=False,
                ),
                *self._state_events(),
            )
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
        self._prepare_pending_action_view_repair(conversation)
        if not self._start_run(session_id=session_id, conversation=conversation):
            self._clear_pending_action_view_repair()
            return (
                BackendErrorEvent(
                    error_code=BackendErrorCode.INVALID_STATE,
                ),
            )
        return (
            *self._confirmation_resolution_events(
                pending_group,
                approved=True,
            ),
            *subagents,
            BackendLifecycleEvent(
                lifecycle=BackendLifecycle.RUNNING,
                source_event_id=f"heartwood-run:{uuid.uuid4()}",
            ),
        )

    def pause(self, *, session_id: str) -> tuple[BackendEvent, ...]:
        """Interrupt active OpenHands I/O and acknowledge a stable boundary."""
        conversation = self._conversation
        can_interrupt = self._request_run_cancellation()
        if conversation is None or not can_interrupt:
            return (
                BackendErrorEvent(
                    error_code=BackendErrorCode.INVALID_STATE,
                ),
            )
        conversation.interrupt()
        if not self._wait_for_run_boundary(_AGENT_WORKER_TRANSITION_TIMEOUT_SECONDS):
            return (
                BackendErrorEvent(
                    error_code=BackendErrorCode.WORKER_STOPPED,
                ),
            )
        return self.reconcile(
            session_id=session_id,
            known_source_event_ids=frozenset(),
        )

    def resume(self, *, session_id: str) -> tuple[BackendEvent, ...]:
        """Resume OpenHands in the background."""
        try:
            conversation = self._get_conversation()
        except Exception as error:
            return (_backend_error(error),)
        if (outcome_error := self._interrupted_outcome_error(conversation)) is not None:
            return (
                BackendErrorEvent(
                    error_code=outcome_error,
                ),
            )
        if self._run_active():
            return (
                BackendErrorEvent(
                    error_code=BackendErrorCode.INVALID_STATE,
                ),
            )
        if _execution_status(conversation) not in {
            BackendLifecycle.PAUSED.value,
            BackendLifecycle.IDLE.value,
        }:
            return (
                BackendErrorEvent(
                    error_code=BackendErrorCode.INVALID_STATE,
                ),
            )
        if not self._start_run(session_id=session_id, conversation=conversation):
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
        with self._conversation_lock:
            if self._closed:
                return
            if self._conversation_closing:
                raise OpenHandsSdkError("OpenHands conversation close is already in progress")
            self._conversation_closing = True
            conversation = self._conversation
        if conversation is None:
            with self._conversation_lock:
                self._closed = True
                self._conversation_closing = False
            return
        try:
            if self._request_run_cancellation():
                conversation.interrupt()
            if not self._wait_for_workers_exit(_AGENT_WORKER_SHUTDOWN_TIMEOUT_SECONDS):
                raise OpenHandsSdkError(
                    "OpenHands worker did not stop; session ownership remains active"
                )
            with self._conversation_lock:
                self._closed = True
            conversation.close()
        except Exception:
            with self._conversation_lock:
                self._closed = False
                self._conversation_closing = False
            raise
        with self._conversation_lock:
            if self._conversation is conversation:
                self._conversation = None
            self._conversation_closing = False

    def _get_conversation(self) -> BaseConversation:
        with self._conversation_lock:
            if self._closed:
                raise OpenHandsSdkError("OpenHands conversation is closed")
            if self._conversation_closing:
                raise OpenHandsSdkError("OpenHands conversation is closing")
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
        options = _llm_options(
            self.profile,
            api_key=self.profile.resolve_api_key(self.env),
            extra_body=self._llm_extra_body,
            native_tool_calling=self._native_tool_calling,
        )
        if self.profile.auth_type == "subscription":
            for key in (
                "api_key",
                "base_url",
                "max_output_tokens",
                "model",
                "stream",
                "temperature",
            ):
                options.pop(key, None)
            llm = cast(
                LLM,
                create_openai_subscription_llm(
                    model=self.profile.model,
                    options=options,
                ),
            )
        else:
            llm = LLM(**options)
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
            with _SPECIALIST_REGISTRATION_LOCK:
                registered_definition = _REGISTERED_HEARTWOOD_SPECIALISTS.get(definition.name)
                if registered_definition is not None:
                    if registered_definition != definition:
                        raise OpenHandsSdkError(
                            f"specialized agent name has conflicting definitions: {definition.name}"
                        )
                    continue
                registered = register_agent_if_absent(
                    name=definition.name,
                    factory_func=agent_definition_to_factory(
                        definition,
                        work_dir=self.workspace,
                    ),
                    description=definition,
                )
                if not registered:
                    raise OpenHandsSdkError(
                        f"specialized agent name is already registered outside Heartwood: "
                        f"{definition.name}"
                    )
                _REGISTERED_HEARTWOOD_SPECIALISTS[definition.name] = definition

    def _handle_sdk_event(self, event: Event) -> None:
        """Leave durable translation to the persisted OpenHands state.

        OpenHands invokes this callback before its own persistence callback.
        Durable Heartwood translation therefore occurs only from conversation
        state after the run reaches a stable boundary.
        """
        if not isinstance(event, ObservationBaseEvent):
            return
        should_pause = False
        with self._view_repair_lock:
            if event.tool_call_id not in self._view_repair_tool_call_ids:
                return
            self._view_repair_tool_call_ids.remove(event.tool_call_id)
            if not self._view_repair_tool_call_ids:
                self._view_repair_boundary_reached = True
                should_pause = True
        if not should_pause:
            return
        conversation = self._conversation
        if conversation is None:
            return
        state = _conversation_state(conversation)
        if state.execution_status == ConversationExecutionStatus.RUNNING:
            with self._view_repair_lock:
                self._view_repair_paused_internally = True
            try:
                conversation.pause()
            except Exception:
                with self._view_repair_lock:
                    self._view_repair_paused_internally = False
                raise

    def _handle_token(self, chunk: LLMStreamChunk) -> None:
        if not chunk.choices:
            return
        content = chunk.choices[0].delta.content
        if isinstance(content, str) and content:
            self._token_sink(content)

    def _run_active(self) -> bool:
        thread = self._run_thread
        return thread is not None and thread.is_alive()

    def _execution_in_flight(self) -> bool:
        with self._run_lock:
            return self._execution_active

    def _start_run(
        self,
        *,
        session_id: str,
        conversation: BaseConversation,
    ) -> bool:
        with self._run_lock:
            if self._run_active():
                return False
            self._run_failed = False
            self._run_cancelled.clear()
            self._execution_active = True
            thread = Thread(
                target=self._run,
                kwargs={
                    "session_id": session_id,
                    "conversation": conversation,
                },
                name=f"heartwood-openhands-{session_id}",
                daemon=True,
            )
            self._run_thread = thread
            self._worker_threads.add(thread)
            thread.start()
            return True

    def _run(
        self,
        *,
        session_id: str,
        conversation: BaseConversation,
    ) -> None:
        failure: tuple[BackendEvent, ...] = ()
        published_source_event_ids: frozenset[str] = frozenset()
        try:
            published_source_event_ids = asyncio.run(
                self._run_until_stable(
                    session_id=session_id,
                    conversation=conversation,
                )
            )
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
                self._execution_active = False
            try:
                final_events = (
                    *failure,
                    *self._reconcile_conversation(
                        conversation,
                        session_id=session_id,
                        known_source_event_ids=published_source_event_ids,
                    ),
                )
            except Exception as error:
                final_events = (
                    *failure,
                    _backend_error(
                        error,
                        source_event_id=self._error_source(f"finalize:{uuid.uuid4()}"),
                    ),
                )
            with self._run_lock:
                if self._run_thread is worker:
                    self._run_thread = None
            try:
                self._event_sink(final_events)
            finally:
                with self._run_lock:
                    self._worker_threads.discard(worker)

    async def _run_until_stable(
        self,
        *,
        session_id: str,
        conversation: BaseConversation,
    ) -> frozenset[str]:
        """Run OpenHands while publishing newly persisted non-token progress."""
        published_source_event_ids: set[str] = set()
        while True:
            run = self._admit_conversation_run(conversation)
            if run is None:
                self._clear_pending_action_view_repair()
                return frozenset(published_source_event_ids)
            try:
                while not run.done():
                    done, _pending = await asyncio.wait(
                        {run},
                        timeout=_AGENT_PROGRESS_POLL_SECONDS,
                    )
                    if run not in done:
                        events = self._reconcile_conversation(
                            conversation,
                            session_id=session_id,
                            known_source_event_ids=frozenset(published_source_event_ids),
                        )
                        if events:
                            await asyncio.to_thread(self._event_sink, events)
                            published_source_event_ids.update(
                                event.source_event_id
                                for event in events
                                if event.source_event_id is not None
                            )
                run.result()
            except asyncio.CancelledError:
                if not self._run_cancelled.is_set():
                    raise
                self._clear_pending_action_view_repair()
                return frozenset(published_source_event_ids)
            except Exception:
                self._clear_pending_action_view_repair()
                raise
            finally:
                self._clear_agent_task(run)
            if self._run_cancelled.is_set():
                self._clear_pending_action_view_repair()
                return frozenset(published_source_event_ids)
            if not self._complete_pending_action_view_repair(conversation):
                return frozenset(published_source_event_ids)

    def _admit_conversation_run(
        self,
        conversation: BaseConversation,
    ) -> asyncio.Task[Any] | None:
        loop = asyncio.get_running_loop()
        with self._run_lock:
            if self._run_cancelled.is_set():
                return None
            run = loop.create_task(self._run_admitted_conversation(conversation))
            self._agent_loop = loop
            self._agent_task = run
            self._agent_started = False
            return run

    async def _run_admitted_conversation(self, conversation: BaseConversation) -> None:
        # Yield once so a pause or close that raced with task admission is observed
        # before OpenHands can start another model or tool step.
        await asyncio.sleep(0)
        with self._run_lock:
            if self._run_cancelled.is_set():
                return
            self._agent_started = True
        await conversation.arun()

    def _request_run_cancellation(self) -> bool:
        with self._run_lock:
            run_thread = self._run_thread
            can_interrupt = (
                self._execution_active and run_thread is not None and run_thread.is_alive()
            )
            if not can_interrupt:
                return False
            self._run_cancelled.set()
            loop = self._agent_loop
            task = self._agent_task
            should_cancel_task = not self._agent_started
        if should_cancel_task and loop is not None and task is not None and not task.done():
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(task.cancel)
        return True

    def _clear_agent_task(self, run: asyncio.Task[Any]) -> None:
        with self._run_lock:
            if self._agent_task is run:
                self._agent_task = None
                self._agent_loop = None
                self._agent_started = False

    def _prepare_pending_action_view_repair(self, conversation: BaseConversation) -> None:
        """Detect the OpenHands cold-view omission for unmatched actions."""
        state = _conversation_state(conversation)
        unmatched_actions = ConversationState.get_unmatched_actions(state.active_branch())
        view_action_ids = {
            event.id for event in state.view.events if isinstance(event, ActionEvent)
        }
        missing_actions = [
            action for action in unmatched_actions if action.id not in view_action_ids
        ]
        with self._view_repair_lock:
            self._view_repair_tool_call_ids = (
                {str(action.tool_call_id) for action in unmatched_actions}
                if missing_actions
                else set()
            )
            self._view_repair_boundary_reached = False
            self._view_repair_paused_internally = False

    def _complete_pending_action_view_repair(self, conversation: BaseConversation) -> bool:
        """Rebuild a cold view after all approved tool results are persisted."""
        with self._view_repair_lock:
            if not self._view_repair_boundary_reached:
                return False
            should_continue = self._view_repair_paused_internally
        state = _conversation_state(conversation)
        state.rebuild_view()
        should_continue = (
            should_continue
            and not self._run_cancelled.is_set()
            and state.execution_status == ConversationExecutionStatus.PAUSED
        )
        self._clear_pending_action_view_repair()
        return should_continue

    def _clear_pending_action_view_repair(self) -> None:
        with self._view_repair_lock:
            self._view_repair_tool_call_ids.clear()
            self._view_repair_boundary_reached = False
            self._view_repair_paused_internally = False

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

    def _unmatched_action_group(
        self,
        conversation: BaseConversation,
    ) -> PendingActionGroup | None:
        return pending_action_group(
            tuple(
                _tool_call(
                    event,
                    analyzed_risk=_analyzed_risk(self._security_analyzer, event),
                    workspace=self.workspace,
                )
                for event in ConversationState.get_unmatched_actions(
                    _conversation_state(conversation).active_branch()
                )
            )
        )

    def _interrupted_outcome_error(
        self,
        conversation: BaseConversation,
    ) -> BackendErrorCode | None:
        state = _conversation_state(conversation)
        if self._execution_in_flight():
            return None
        unmatched_actions = ConversationState.get_unmatched_actions(state.active_branch())
        with self._run_lock:
            run_cancelled = self._run_cancelled.is_set()
        intentional_pause = (
            run_cancelled and state.execution_status == ConversationExecutionStatus.PAUSED
        )
        if (
            unmatched_actions
            and state.execution_status != ConversationExecutionStatus.WAITING_FOR_CONFIRMATION
            and not intentional_pause
        ):
            return BackendErrorCode.ACTION_OUTCOME_UNKNOWN
        if state.execution_status == ConversationExecutionStatus.RUNNING:
            return BackendErrorCode.AGENT_OUTCOME_UNKNOWN
        return None

    def _action_resolution_context(
        self,
        branch: Sequence[Event],
    ) -> tuple[dict[str, ActionEvent], dict[str, PendingActionGroup]]:
        actions_by_id: dict[str, ActionEvent] = {}
        pending_actions: list[ActionEvent] = []
        groups: dict[str, PendingActionGroup] = {}
        for event in branch:
            if (
                isinstance(event, ActionEvent)
                and event.action is not None
                and event.tool_name != _OPENHANDS_FINISH_TOOL_NAME
            ):
                actions_by_id[event.id] = event
                pending_actions.append(event)
                continue
            if not isinstance(event, ObservationEvent | UserRejectObservation):
                continue
            action = actions_by_id.get(event.action_id)
            if action is None:
                continue
            if action.id not in groups:
                group = pending_action_group(
                    tuple(
                        _tool_call(
                            pending,
                            analyzed_risk=_analyzed_risk(self._security_analyzer, pending),
                            workspace=self.workspace,
                        )
                        for pending in pending_actions
                    )
                )
                if group is not None:
                    groups.update(dict.fromkeys((pending.id for pending in pending_actions), group))
            pending_actions = [pending for pending in pending_actions if pending.id != action.id]
        for action in pending_actions:
            if action.id in groups:
                continue
            group = pending_action_group(
                (
                    _tool_call(
                        action,
                        analyzed_risk=_analyzed_risk(self._security_analyzer, action),
                        workspace=self.workspace,
                    ),
                )
            )
            if group is not None:
                groups[action.id] = group
        return actions_by_id, groups

    def _confirmation_resolution_events(
        self,
        group: PendingActionGroup,
        *,
        approved: bool,
    ) -> tuple[BackendEvent, ...]:
        return tuple(
            BackendConfirmationResolutionEvent(
                tool_call=action,
                action_group_id=group.group_id,
                approved=approved,
                source_event_id=_confirmation_resolution_source(action.tool_call_id),
            )
            for action in group.actions
        )

    def _translate_event(
        self,
        event: Event,
        *,
        session_id: str,
        actions_by_id: Mapping[str, ActionEvent] | None = None,
        action_groups: Mapping[str, PendingActionGroup] | None = None,
    ) -> tuple[BackendEvent, ...]:
        actions_by_id = {} if actions_by_id is None else actions_by_id
        action_groups = {} if action_groups is None else action_groups
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
            if event.tool_name == _OPENHANDS_FINISH_TOOL_NAME:
                message = _finish_message(event)
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
            tool_call = _tool_call(
                event,
                analyzed_risk=_analyzed_risk(self._security_analyzer, event),
                workspace=self.workspace,
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
            if event.tool_name == _OPENHANDS_FINISH_TOOL_NAME:
                return ()
            action = actions_by_id.get(event.action_id)
            action_group = None if action is None else action_groups.get(action.id)
            observation_events: list[BackendEvent] = []
            if (
                action is not None
                and action_group is not None
                and self._action_required_confirmation(action)
            ):
                observation_events.extend(
                    self._confirmation_resolution_events(
                        action_group,
                        approved=True,
                    )
                )
            observation_events.append(
                _tool_observation(
                    event,
                    source_event_id=f"{source}:observation",
                )
            )
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
            action = actions_by_id.get(event.action_id)
            action_group = None if action is None else action_groups.get(action.id)
            if action is None or action_group is None:
                return ()
            return self._confirmation_resolution_events(
                action_group,
                approved=False,
            )
        if isinstance(event, AgentErrorEvent):
            action = next(
                (
                    candidate
                    for candidate in actions_by_id.values()
                    if candidate.tool_call_id == event.tool_call_id
                ),
                None,
            )
            tool_name = event.tool_name or "unknown-tool"
            result, result_truncated = _bounded_tool_result(event.error)
            return (
                BackendToolExecutionEvent(
                    tool_execution=ToolExecution(
                        tool_call_id=event.tool_call_id,
                        action_id=None if action is None else action.id,
                        tool_name=tool_name,
                        exit_code=1,
                        summary=f"{tool_name} failed",
                        result=result,
                        result_truncated=result_truncated,
                    ),
                    source_event_id=f"{source}:execution",
                ),
                BackendErrorEvent(
                    error_code=BackendErrorCode.ACTION_FAILED,
                    source_event_id=f"{source}:error",
                ),
            )
        if isinstance(event, PauseEvent):
            with self._view_repair_lock:
                if self._view_repair_paused_internally:
                    return ()
            return (
                BackendLifecycleEvent(
                    lifecycle=BackendLifecycle.PAUSED,
                    source_event_id=f"{source}:lifecycle",
                ),
            )
        return ()

    def _action_required_confirmation(self, action: ActionEvent) -> bool:
        if self.action_confirmation_mode == "always-confirm":
            return True
        return _analyzed_risk(self._security_analyzer, action) != SecurityRisk.LOW.value.lower()

    def _state_events(
        self,
        conversation: BaseConversation | None = None,
    ) -> tuple[BackendEvent, ...]:
        if conversation is None:
            conversation = self._get_conversation()
        state = _conversation_state(conversation)
        branch = state.active_branch()
        anchor = branch[-1].id if branch else str(conversation.id)
        unmatched_actions = ConversationState.get_unmatched_actions(branch)
        unmatched_group = self._unmatched_action_group(conversation)
        lifecycle = _backend_lifecycle(state.execution_status)
        with self._run_lock:
            run_failed = self._run_failed
            execution_active = self._execution_active
            run_cancelled = self._run_cancelled.is_set()
        if (
            execution_active
            and not run_cancelled
            and state.execution_status
            in {
                ConversationExecutionStatus.IDLE,
                ConversationExecutionStatus.PAUSED,
            }
        ):
            lifecycle = BackendLifecycle.RUNNING
        interrupted_outcome = (
            state.execution_status == ConversationExecutionStatus.RUNNING and not execution_active
        )
        intentional_pause = (
            run_cancelled and state.execution_status == ConversationExecutionStatus.PAUSED
        )
        inactive_with_unmatched_actions = (
            unmatched_group is not None
            and not execution_active
            and state.execution_status != ConversationExecutionStatus.WAITING_FOR_CONFIRMATION
            and not intentional_pause
        )
        if run_failed or interrupted_outcome or inactive_with_unmatched_actions:
            lifecycle = BackendLifecycle.ERROR
        events: list[BackendEvent] = [
            BackendLifecycleEvent(
                lifecycle=lifecycle,
                source_event_id=(f"openhands-state:{anchor}:{lifecycle.value}:lifecycle"),
            )
        ]
        if interrupted_outcome or inactive_with_unmatched_actions:
            outcome_error = (
                BackendErrorCode.ACTION_OUTCOME_UNKNOWN
                if unmatched_group is not None
                else BackendErrorCode.AGENT_OUTCOME_UNKNOWN
            )
            events.append(
                BackendErrorEvent(
                    error_code=outcome_error,
                    source_event_id=(
                        f"openhands-state:{anchor}:{outcome_error.value.lower()}:outcome-unknown"
                    ),
                )
            )
        if (
            state.execution_status
            in {
                ConversationExecutionStatus.ERROR,
                ConversationExecutionStatus.STUCK,
                ConversationExecutionStatus.DELETING,
            }
            and not run_failed
        ):
            events.append(
                BackendErrorEvent(
                    error_code=BackendErrorCode.CONVERSATION_STOPPED,
                    source_event_id=(f"openhands-state:{anchor}:conversation-stopped"),
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
                        workspace=self.workspace,
                    ),
                    action_group_id=unmatched_group.group_id,
                    source_event_id=f"openhands:{action.id}:confirmation",
                )
                for action in unmatched_actions
            )
        if not execution_active:
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


def _finish_message(event: ActionEvent) -> str:
    if event.action is None:
        return ""
    message = event.action.model_dump(mode="json").get("message")
    return message if isinstance(message, str) else ""


def _tool_call(
    event: ActionEvent,
    *,
    analyzed_risk: str | None = None,
    workspace: Path | None = None,
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
        action_id=event.id,
        kind=_tool_kind(event),
        affected_paths=_affected_paths(event, workspace=workspace),
        project_path=_project_path(event, workspace=workspace),
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


def _tool_kind(
    event: ActionEvent,
) -> Literal["terminal", "file-editor", "task", "other"]:
    action = event.action
    if isinstance(action, TerminalAction):
        return "terminal"
    if isinstance(action, FileEditorAction):
        return "file-editor"
    if isinstance(action, TaskAction):
        return "task"
    return "other"


def _affected_paths(event: ActionEvent, *, workspace: Path | None) -> tuple[str, ...]:
    """Return only paths proven to be modified by a typed file-editor action."""
    action = event.action
    project_path = _project_path(event, workspace=workspace)
    if not isinstance(action, FileEditorAction) or action.command == "view":
        return ()
    if project_path is None:
        return ()
    return (project_path,)


def _project_path(event: ActionEvent, *, workspace: Path | None) -> str | None:
    """Return a project-relative path referenced by a typed file-editor action."""
    action = event.action
    if workspace is None or not isinstance(action, FileEditorAction):
        return None
    root = workspace.expanduser().resolve()
    path = Path(action.path).expanduser()
    resolved = (path if path.is_absolute() else root / path).resolve()
    try:
        relative = resolved.relative_to(root)
    except ValueError:
        return None
    if not relative.parts or ".heartwood" in relative.parts or ".git" in relative.parts:
        return None
    return relative.as_posix()


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
    result, result_truncated = _bounded_tool_result(observation.text)
    return BackendToolExecutionEvent(
        tool_execution=ToolExecution(
            tool_call_id=event.tool_call_id,
            action_id=event.action_id,
            tool_name=tool_name,
            exit_code=resolved_exit_code,
            summary=f"{tool_name} {'failed' if failed else 'completed'}",
            result=result,
            result_truncated=result_truncated,
        ),
        source_event_id=source_event_id,
    )


def _bounded_tool_result(value: str) -> tuple[str | None, bool]:
    if not value:
        return None, False
    lines = value.splitlines(keepends=True)
    if len(lines) <= _MAX_TOOL_RESULT_LINES and len(value) <= _MAX_TOOL_RESULT_CHARS:
        return value, False

    marker = "\n... Heartwood output truncated ...\n"
    payload_characters = _MAX_TOOL_RESULT_CHARS - len(marker)
    payload_lines = _MAX_TOOL_RESULT_LINES - len(marker.splitlines())
    head_lines = payload_lines // 2
    tail_lines = payload_lines - head_lines
    head = "".join(lines[:head_lines])
    tail = "".join(lines[-tail_lines:])
    head_characters = payload_characters // 2
    tail_characters = payload_characters - head_characters
    return f"{head[:head_characters]}{marker}{tail[-tail_characters:]}", True


def _confirmation_resolution_source(tool_call_id: str) -> str:
    return f"openhands-tool-call:{tool_call_id}:confirmation-resolution"


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
    }.get(status, BackendLifecycle.ERROR)


def _task_status(status: str) -> BackendTaskStatus:
    return {
        "todo": BackendTaskStatus.TODO,
        "in_progress": BackendTaskStatus.IN_PROGRESS,
        "done": BackendTaskStatus.DONE,
    }.get(status, BackendTaskStatus.TODO)


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
