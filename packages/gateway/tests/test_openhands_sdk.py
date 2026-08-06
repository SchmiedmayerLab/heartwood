# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import asyncio
import inspect
import json
import os
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import replace
from functools import cache
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from typing import Literal, cast

import pytest
from litellm.types.utils import Delta, StreamingChoices
from openhands.sdk import LLM, Agent, LLMStreamChunk, LocalConversation, Tool
from openhands.sdk.agent import base as agent_base
from openhands.sdk.conversation import (
    BaseConversation,
    ConversationExecutionStatus,
    ConversationState,
)
from openhands.sdk.conversation.conversation_stats import ConversationStats
from openhands.sdk.event import (
    ActionEvent,
    AgentErrorEvent,
    MessageEvent,
    ObservationEvent,
    PauseEvent,
    UserRejectObservation,
)
from openhands.sdk.event import (
    Event as OpenHandsEvent,
)
from openhands.sdk.event.conversation_error import ConversationErrorEvent
from openhands.sdk.event.error_classification import ErrorClassification, FailureKind
from openhands.sdk.llm import LLMResponse, Message, MessageToolCall, Metrics, TextContent
from openhands.sdk.llm.llm import LLMCallContext
from openhands.sdk.llm.streaming import TokenCallbackType
from openhands.sdk.security import (
    AlwaysConfirm,
    ConfirmRisky,
    EnsembleSecurityAnalyzer,
    SecurityAnalyzerBase,
    SecurityRisk,
)
from openhands.sdk.settings import (
    LLMSummarizingCondenserSettings,
    OpenHandsAgentSettings,
)
from openhands.sdk.skills import load_skills_from_dir
from openhands.sdk.subagent import (
    get_agent_factory,
    get_registered_agent_definitions,
    register_agent_if_absent,
)
from openhands.sdk.testing import TestLLM
from openhands.sdk.tool import ToolDefinition
from openhands.sdk.tool.builtins.finish import FinishAction, FinishObservation, FinishTool
from openhands.sdk.tool.schema import Action, Observation
from openhands.tools import TaskTrackerTool, TerminalTool
from openhands.tools.file_editor import FileEditorAction
from openhands.tools.task import TaskAction, TaskObservation
from openhands.tools.task_tracker import TaskTrackerObservation
from openhands.tools.task_tracker.definition import TaskItem
from openhands.tools.terminal import TerminalAction, TerminalObservation

import heartwood.gateway._openhands_sdk as openhands_sdk_module
from heartwood.core_adapter import (
    BackendAgentMessageEvent,
    BackendConfirmationResolutionEvent,
    BackendErrorCode,
    BackendErrorEvent,
    BackendEvent,
    BackendEventKind,
    BackendLifecycle,
    BackendLifecycleEvent,
    BackendSubagentEvent,
    BackendSubagentStatus,
    BackendTaskPlanEvent,
    BackendTaskStatus,
    BackendToolCallEvent,
    BackendToolExecutionEvent,
    BackendUsageEvent,
    PendingActionGroup,
    SessionOwnershipError,
    SessionService,
)
from heartwood.gateway import (
    ModelProfile,
    OpenHandsSdkBackend,
    ProjectContext,
    SessionGateway,
    SessionProjection,
)
from heartwood.gateway._openhands_failures import classified_error_code
from heartwood.gateway._openhands_persistence import ContentMinimizedLocalFileStore
from heartwood.gateway._openhands_sdk import (
    ConversationFactory,
    OpenHandsSdkError,
    _agent_context,
    _agent_settings,
    _analyzed_risk,
    _backend_lifecycle,
    _configure_upstream_defaults,
    _context_condenser_settings,
    _conversation_runtime_options,
    _llm_max_message_chars,
    _llm_options,
    _llm_resilience_options,
    _observation_exit_code,
    _security_configuration,
    _task_status,
    _terminal_tool_params,
    _tool_arguments,
    _tool_call,
    _tool_observation,
    _usage,
    _usage_source_event_id,
)
from heartwood.gateway._specialist_task import HeartwoodSpecialistToolSet
from heartwood.gateway._specialists import (
    SpecialistCatalog,
    SpecialistCatalogError,
    load_specialist_catalog,
)
from heartwood.session import CommandKind, EventKind, JsonValue, SessionCommand


@pytest.fixture(autouse=True)
def _disable_profile_store_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_base, "has_vision_profile_available", lambda: False)


def test_verified_skills_load_through_openhands_native_loader() -> None:
    repository, knowledge, agent = load_skills_from_dir(_verified_skills_root())

    assert set(repository) | set(knowledge) | set(agent) == {
        "aggregate-export",
        "baseline-model",
        "omop-cohort-summary",
    }


def test_bundled_tool_free_specialists_register_with_openhands(tmp_path: Path) -> None:
    catalog = _specialist_catalog()

    assert [role.specialist_id for role in catalog.available_roles] == [
        "research-planner",
        "data-quality-reviewer",
        "cohort-feature-reviewer",
        "statistical-reviewer",
        "reproducibility-reviewer",
    ]
    assert all(role.definition.tools == [] for role in catalog.available_roles)

    backend = OpenHandsSdkBackend(
        profile=_local_profile(),
        workspace=tmp_path / "workspace",
        skills_dir=tmp_path / "skills",
        persistence_dir=tmp_path / "openhands",
        conversation_key="specialist-registration",
        specialist_catalog=catalog,
        env={},
        conversation_factory=_finished_conversation_factory(
            tmp_path,
            TestLLM.from_messages([]),
        ),
    )
    backend._register_specialized_agents()
    registered = {item.name for item in get_registered_agent_definitions()}
    assert {role.specialist_id for role in catalog.available_roles} <= registered
    assert "analysis-implementer" not in registered
    for role in catalog.available_roles:
        registered_definition = get_agent_factory(role.specialist_id).definition
        assert registered_definition.max_iteration_per_run == role.definition.max_iteration_per_run
        assert registered_definition.max_budget_per_run == role.definition.max_budget_per_run


def test_tool_enabled_specialized_agents_fail_closed(tmp_path: Path) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "unsafe-specialist.md").write_text(
        "\n".join(
            (
                "---",
                "name: unsafe-specialist",
                "description: Attempts unaudited child actions.",
                "model: inherit",
                "tools:",
                "  - terminal",
                "skills: []",
                "max_iteration_per_run: 4",
                "max_budget_per_run: 1.0",
                "permission_mode: always_confirm",
                "heartwood:",
                "  label: Unsafe Specialist",
                "  capability: project-actions",
                "  availability: available",
                "  order: 1",
                "---",
                "",
                "Run a terminal action.",
                "",
            )
        ),
        encoding="utf-8",
    )
    with pytest.raises(SpecialistCatalogError, match="must remain unavailable"):
        load_specialist_catalog(agents_dir, _verified_skills_root())


def test_specialized_agent_name_collision_fails_closed(tmp_path: Path) -> None:
    specialist_name = f"heartwood-collision-{uuid.uuid4().hex}"
    assert register_agent_if_absent(
        name=specialist_name,
        factory_func=lambda llm: OpenHandsAgentSettings(
            llm=llm,
            tools=[Tool(name=TerminalTool.name)],
        ).create_agent(),
        description="Externally registered tool-enabled specialist.",
    )
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / f"{specialist_name}.md").write_text(
        "\n".join(
            (
                "---",
                f"name: {specialist_name}",
                "description: Verified tool-free specialist.",
                "model: inherit",
                "tools: []",
                "skills: []",
                "max_iteration_per_run: 4",
                "max_budget_per_run: 1.0",
                "permission_mode: always_confirm",
                "heartwood:",
                "  label: Collision Specialist",
                "  capability: advisory",
                "  availability: available",
                "  order: 1",
                "---",
                "",
                "Return a bounded plan.",
                "",
            )
        ),
        encoding="utf-8",
    )
    backend = OpenHandsSdkBackend(
        profile=_local_profile(),
        workspace=tmp_path / "workspace",
        skills_dir=tmp_path / "skills",
        persistence_dir=tmp_path / "openhands",
        conversation_key="specialist-collision",
        specialist_catalog=load_specialist_catalog(
            agents_dir,
            _verified_skills_root(),
        ),
        env={},
        conversation_factory=_finished_conversation_factory(
            tmp_path,
            TestLLM.from_messages([]),
        ),
    )

    with pytest.raises(OpenHandsSdkError, match="already registered outside Heartwood"):
        backend._register_specialized_agents()


def test_foreign_registered_agent_cannot_cross_the_specialist_allowlist(
    tmp_path: Path,
) -> None:
    specialist_name = f"foreign-tool-agent-{uuid.uuid4().hex}"
    factory_calls: list[str] = []

    def foreign_factory(llm: LLM) -> Agent:
        factory_calls.append(specialist_name)
        return OpenHandsAgentSettings(
            llm=llm,
            tools=[Tool(name=TerminalTool.name)],
        ).create_agent()

    assert register_agent_if_absent(
        name=specialist_name,
        factory_func=foreign_factory,
        description="Foreign tool-enabled agent.",
    )
    llm = TestLLM.from_messages(
        [
            _task_message(
                "foreign-task-call",
                description="Use foreign agent",
                prompt="Run an unapproved task.",
                specialist_id=specialist_name,
            )
        ]
    )
    backend = OpenHandsSdkBackend(
        profile=_local_profile(),
        workspace=tmp_path / "workspace",
        skills_dir=tmp_path / "skills",
        persistence_dir=tmp_path / "openhands",
        conversation_key="foreign-specialist",
        specialist_catalog=_specialist_catalog(),
        env={},
        conversation_factory=_conversation_factory(
            tmp_path,
            llm,
            tools=[_specialist_tool()],
        ),
    )

    backend._register_specialized_agents()
    backend.submit_turn(session_id="session-1", prompt="Use the foreign specialist")
    deadline = time.monotonic() + 10
    while backend._run_active() and time.monotonic() < deadline:
        time.sleep(0.02)
    events = backend.reconcile(
        session_id="session-1",
        known_source_event_ids=frozenset(),
    )

    assert not backend._run_active()
    assert backend.pending_action_group(session_id="session-1") is None
    assert factory_calls == []
    assert any(
        isinstance(event, BackendConfirmationResolutionEvent) and not event.approved
        for event in events
    )
    backend.close()


def test_openhands_context_loads_only_explicitly_verified_skills() -> None:
    context = _agent_context([])

    assert context.skills == []
    assert context.load_user_skills is False
    assert context.load_public_skills is False
    assert context.load_project_skills is False
    assert context.load_memory is False
    suffix = context.system_message_suffix or ""
    assert "invoke_skill" in suffix
    assert "not tools named after their identifiers" in suffix
    assert "location returned by invoke_skill" in suffix
    assert "Never install a dependency solely" in suffix
    assert "/opt/heartwood" not in suffix
    assert str(Path.cwd()) not in suffix


def test_terminal_tool_masks_all_configured_provider_environment_keys() -> None:
    environment_profile = _remote_profile()
    local_profile = _local_profile()

    assert _terminal_tool_params(environment_profile, ("ANTHROPIC_API_KEY",)) == {
        "env": {"ANTHROPIC_API_KEY": "", "OPENAI_API_KEY": ""}
    }
    assert _terminal_tool_params(local_profile, ("OPENAI_API_KEY",)) == {
        "env": {"OPENAI_API_KEY": ""}
    }
    assert _terminal_tool_params(local_profile) == {}


def test_openhands_defaults_are_quiet_offline_and_allow_deployment_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("LITELLM_LOCAL_MODEL_COST_MAP", "LOG_LEVEL", "OPENHANDS_SUPPRESS_BANNER"):
        monkeypatch.delenv(name, raising=False)

    _configure_upstream_defaults({})

    assert os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] == "True"
    assert os.environ["LOG_LEVEL"] == "ERROR"
    assert os.environ["OPENHANDS_SUPPRESS_BANNER"] == "1"

    monkeypatch.delenv("LOG_LEVEL")
    _configure_upstream_defaults({"LOG_LEVEL": "WARNING"})
    assert os.environ["LOG_LEVEL"] == "WARNING"


def test_openhands_llm_and_condenser_settings_follow_the_profile_budget() -> None:
    profile = _local_profile(max_input_tokens=28_672, max_output_tokens=4_096)
    options = _llm_options(
        profile,
        api_key=None,
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        native_tool_calling=False,
    )
    condenser = _context_condenser_settings(profile)

    assert options["stream"] is True
    assert options["usage_id"] == "agent"
    assert options["litellm_extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
    assert options["max_input_tokens"] == 28_672
    assert options["max_output_tokens"] == 4_096
    assert options["input_cost_per_token"] == 0.0
    assert options["native_tool_calling"] is False
    assert _llm_max_message_chars(profile) == 114_688
    assert _llm_max_message_chars(_local_profile()) == 30_000
    assert condenser == LLMSummarizingCondenserSettings(
        max_tokens=21_504,
        max_size=240,
        keep_first=2,
    )


def test_openhands_agent_and_conversation_options_are_explicit() -> None:
    llm = TestLLM.from_messages([], usage_id="agent")
    settings = _agent_settings(
        llm=llm,
        tools=[],
        context=_agent_context([]),
        condenser=LLMSummarizingCondenserSettings(
            max_tokens=1_024,
            max_size=40,
            keep_first=2,
        ),
    )

    assert settings.enable_sub_agents is False
    assert settings.enable_switch_llm_tool is False
    assert settings.tool_concurrency_limit == 1
    assert settings.mcp_config == {}
    assert settings.verification.critic_enabled is False
    assert settings.verification.enable_iterative_refinement is False
    assert _conversation_runtime_options() == {
        "max_iteration_per_run": 100,
        "stuck_detection": True,
    }


def test_openhands_bounds_local_and_remote_model_retries() -> None:
    assert _llm_resilience_options(_local_profile()) == {
        "num_retries": 1,
        "retry_max_wait": 8,
        "retry_min_wait": 1,
        "retry_multiplier": 2.0,
        "timeout": 600,
    }
    assert _llm_resilience_options(_remote_profile()) == {
        "num_retries": 2,
        "retry_max_wait": 8,
        "retry_min_wait": 1,
        "retry_multiplier": 2.0,
        "timeout": 180,
    }


def test_backend_exposes_typed_route_metadata_and_rejects_invalid_mode(
    tmp_path: Path,
) -> None:
    backend = _backend(
        tmp_path,
        _finished_conversation_factory(tmp_path, TestLLM.from_messages([])),
    )

    assert backend.backend_id == "openhands-sdk"
    assert backend.configuration_error is None
    assert backend.model_endpoint == "http://127.0.0.1:8765/v1/chat/completions"
    assert backend.model_profile_id == "heartwood"
    assert backend.capability_tier == "supervised"
    assert backend.credential_reference is None
    assert backend.action_confirmation_mode == "always-confirm"
    assert backend.continuation_requires_model_authorization
    backend._register_specialized_agents()

    with pytest.raises(OpenHandsSdkError, match="unsupported action confirmation mode"):
        OpenHandsSdkBackend(
            profile=_local_profile(),
            workspace=tmp_path / "workspace",
            skills_dir=tmp_path / "skills",
            persistence_dir=tmp_path / "openhands-invalid",
            conversation_key="invalid-mode",
            action_confirmation_mode=cast(Literal["always-confirm", "confirm-risky"], "invalid"),
            env={},
        )


def test_unavailable_conversation_fails_closed_without_provider_details(
    tmp_path: Path,
) -> None:
    def unavailable_factory(
        _event_callback: Callable[[OpenHandsEvent], None],
        _token_callback: Callable[[LLMStreamChunk], None],
    ) -> BaseConversation:
        raise RuntimeError("private provider failure")

    backend = _backend(tmp_path, unavailable_factory)

    assert backend.pending_action_group(session_id="session-1") is None
    first_error = backend.reconcile(
        session_id="session-1",
        known_source_event_ids=frozenset(),
    )
    assert isinstance(first_error[0], BackendErrorEvent)
    assert first_error[0].error_code == BackendErrorCode.WORKER_STOPPED
    assert first_error[0].source_event_id is not None
    assert (
        backend.reconcile(
            session_id="session-1",
            known_source_event_ids=frozenset({first_error[0].source_event_id}),
        )
        == ()
    )
    failed_submissions = (
        backend.submit_turn(
            session_id="session-1",
            prompt="Do not send",
        )[0],
        backend.submit_turn(
            session_id="session-1",
            prompt="Still do not send",
        )[0],
    )
    failed_errors = [event for event in failed_submissions if isinstance(event, BackendErrorEvent)]
    assert len(failed_errors) == len(failed_submissions)
    assert [event.error_code for event in failed_errors] == [
        BackendErrorCode.WORKER_STOPPED,
        BackendErrorCode.WORKER_STOPPED,
    ]
    assert all(event.source_event_id is None for event in failed_submissions)
    failed_resume = backend.resume(session_id="session-1")
    assert isinstance(failed_resume[0], BackendErrorEvent)
    assert failed_resume[0].error_code == BackendErrorCode.WORKER_STOPPED
    assert failed_resume[0].source_event_id is None
    missing_group = backend.resolve_confirmation(
        session_id="session-1",
        action_group_id="missing-group",
        approved=True,
    )
    assert isinstance(missing_group[0], BackendErrorEvent)
    assert missing_group[0].error_code == BackendErrorCode.INVALID_STATE


def test_pending_action_blocks_steering_and_reject_failure_is_safe(
    tmp_path: Path,
) -> None:
    action = _terminal_action_event("action-1", "call-1", "printf safe")
    conversation = _RejectFailingConversation(action)
    backend = _backend(
        tmp_path,
        cast(
            ConversationFactory,
            lambda _event_callback, _token_callback: conversation,
        ),
    )
    group = backend.pending_action_group(session_id="session-1")
    assert group is not None

    blocked_steering = backend.submit_turn(
        session_id="session-1",
        prompt="Steer before deciding",
    )
    assert isinstance(blocked_steering[0], BackendErrorEvent)
    assert blocked_steering[0].error_code == BackendErrorCode.INVALID_STATE
    rejection_error = backend.resolve_confirmation(
        session_id="session-1",
        action_group_id=group.group_id,
        approved=False,
    )[0]
    assert isinstance(rejection_error, BackendErrorEvent)
    assert rejection_error.error_code == BackendErrorCode.WORKER_STOPPED
    assert rejection_error.source_event_id is None


def test_token_and_tool_payload_edge_cases_remain_typed(tmp_path: Path) -> None:
    backend = _backend(
        tmp_path,
        _finished_conversation_factory(tmp_path, TestLLM.from_messages([])),
    )
    deltas: list[str] = []
    backend.bind_runtime(
        event_sink=lambda _events: None,
        token_sink=deltas.append,
    )
    backend._handle_token(
        LLMStreamChunk(
            id="empty",
            created=0,
            object="chat.completion.chunk",
            choices=[],
        )
    )
    backend._handle_token(
        LLMStreamChunk(
            id="none",
            created=0,
            object="chat.completion.chunk",
            choices=[StreamingChoices(index=0, delta=Delta(content=None))],
        )
    )
    backend._handle_token(
        LLMStreamChunk(
            id="text",
            created=0,
            object="chat.completion.chunk",
            choices=[StreamingChoices(index=0, delta=Delta(content="Working"))],
        )
    )
    assert deltas == ["Working"]

    action = _terminal_action_event("action-1", "call-1", "printf safe")
    invalid_json = action.model_copy(
        update={
            "tool_call": action.tool_call.model_copy(
                update={"arguments": "{"},
            )
        }
    )
    scalar_with_action = action.model_copy(
        update={
            "tool_call": action.tool_call.model_copy(
                update={"arguments": '"scalar"'},
            )
        }
    )
    scalar_without_action = scalar_with_action.model_copy(update={"action": None})
    assert _tool_arguments(invalid_json) == {"raw": "{"}
    assert _tool_arguments(scalar_with_action)["command"] == "printf safe"
    assert _tool_arguments(scalar_without_action) == {}
    assert _observation_exit_code(_MetadataObservation(metadata={"exit_code": 7})) == 7
    assert _observation_exit_code(_MetadataObservation(metadata={"exit_code": True})) is None


def test_openhands_security_configuration_uses_upstream_policies() -> None:
    analyzer, always = _security_configuration("always-confirm")
    risk_analyzer, risky = _security_configuration("confirm-risky")

    assert isinstance(analyzer, EnsembleSecurityAnalyzer)
    assert isinstance(risk_analyzer, EnsembleSecurityAnalyzer)
    assert analyzer.propagate_unknown is True
    assert risk_analyzer.propagate_unknown is True
    assert isinstance(always, AlwaysConfirm)
    assert always.should_confirm(SecurityRisk.LOW) is True
    assert isinstance(risky, ConfirmRisky)
    assert risky.threshold == SecurityRisk.MEDIUM
    assert risky.confirm_unknown is True
    assert risky.should_confirm(SecurityRisk.LOW) is False
    assert risky.should_confirm(SecurityRisk.UNKNOWN) is True


def test_typed_event_translation_covers_messages_tools_tasks_and_errors(
    tmp_path: Path,
) -> None:
    backend = _backend(
        tmp_path,
        _finished_conversation_factory(tmp_path, TestLLM.from_messages([])),
    )
    message = MessageEvent(
        id="message-1",
        source="agent",
        llm_message=Message(
            role="assistant",
            content=[TextContent(text="Analysis complete.")],
        ),
    )
    action = _terminal_action_event("action-1", "call-1", "pwd")
    observation = ObservationEvent(
        id="observation-1",
        tool_name="terminal",
        tool_call_id="call-1",
        action_id=action.id,
        observation=TerminalObservation(command="pwd", exit_code=0),
    )
    task_plan = ObservationEvent(
        id="task-plan-1",
        tool_name="task_tracker",
        tool_call_id="task-plan-call",
        action_id="task-plan-action",
        observation=TaskTrackerObservation(
            command="plan",
            task_list=[
                TaskItem(title="Inspect inputs", notes="", status="done"),
                TaskItem(title="Run analysis", notes="", status="in_progress"),
            ],
        ),
    )
    subagent_action = _subagent_action_event()
    subagent_observation = ObservationEvent(
        id="subagent-observation",
        tool_name="task",
        tool_call_id="subagent-call",
        action_id=subagent_action.id,
        observation=TaskObservation(
            task_id="task-1",
            subagent="research-planner",
            status="completed",
            content=[TextContent(text="Plan complete.")],
        ),
    )

    translated = (
        *backend._translate_event(message, session_id="session-1"),
        *backend._translate_event(action, session_id="session-1"),
        *backend._translate_event(observation, session_id="session-1"),
        *backend._translate_event(task_plan, session_id="session-1"),
        *backend._translate_event(subagent_action, session_id="session-1"),
        *backend._translate_event(subagent_observation, session_id="session-1"),
        *backend._translate_event(
            AgentErrorEvent(
                id="error-1",
                tool_name="terminal",
                tool_call_id="call-1",
                error="provider-specific detail",
            ),
            session_id="session-1",
            actions_by_id={action.id: action},
        ),
        *backend._translate_event(PauseEvent(id="pause-1"), session_id="session-1"),
    )

    assert isinstance(translated[0], BackendAgentMessageEvent)
    assert translated[0].message == "Analysis complete."
    assert isinstance(translated[1], BackendToolCallEvent)
    assert translated[1].tool_call.arguments == {"command": "pwd"}
    assert isinstance(translated[2], BackendToolExecutionEvent)
    assert translated[2].tool_execution.exit_code == 0
    task_event = next(event for event in translated if isinstance(event, BackendTaskPlanEvent))
    assert [task.status for task in task_event.tasks] == [
        BackendTaskStatus.DONE,
        BackendTaskStatus.IN_PROGRESS,
    ]
    subagent_events = [
        event.subagent for event in translated if isinstance(event, BackendSubagentEvent)
    ]
    assert [item.status for item in subagent_events] == [
        BackendSubagentStatus.PROPOSED,
        BackendSubagentStatus.COMPLETED,
    ]
    errors = [event for event in translated if isinstance(event, BackendErrorEvent)]
    assert [event.error_code for event in errors] == ["HW-AGENT-002"]
    assert "provider-specific detail" not in repr(errors)
    executions = [
        event.tool_execution for event in translated if isinstance(event, BackendToolExecutionEvent)
    ]
    failed = executions[-1]
    assert failed.tool_call_id == "call-1"
    assert failed.action_id == action.id
    assert failed.tool_name == "terminal"
    assert failed.exit_code == 1
    assert failed.result == "provider-specific detail"
    assert failed.summary == "terminal failed"
    assert isinstance(translated[-1], BackendLifecycleEvent)
    assert translated[-1].lifecycle == BackendLifecycle.PAUSED


def test_persisted_observations_reconstruct_one_grouped_approval_without_duplicates(
    tmp_path: Path,
) -> None:
    first = _terminal_action_event("action-1", "call-1", "printf first")
    second = _terminal_action_event("action-2", "call-2", "printf second")
    first = first.model_copy(update={"llm_response_id": "response-group"})
    second = second.model_copy(update={"llm_response_id": "response-group"})
    observations = (
        ObservationEvent(
            id="observation-1",
            tool_name="terminal",
            tool_call_id=first.tool_call_id,
            action_id=first.id,
            observation=TerminalObservation(command="printf first", exit_code=0),
        ),
        ObservationEvent(
            id="observation-2",
            tool_name="terminal",
            tool_call_id=second.tool_call_id,
            action_id=second.id,
            observation=TerminalObservation(command="printf second", exit_code=0),
        ),
    )
    conversation = _ControlledConversation()
    state = _BranchState(conversation.id)
    state.execution_status = ConversationExecutionStatus.FINISHED
    state.events = (first, second, *observations)
    conversation.state = state
    backend = _backend(
        tmp_path,
        cast(
            ConversationFactory,
            lambda _event_callback, _token_callback: conversation,
        ),
    )

    events = backend.reconcile(
        session_id="session-1",
        known_source_event_ids=frozenset(),
    )
    resolutions = [
        event for event in events if isinstance(event, BackendConfirmationResolutionEvent)
    ]
    executions = [
        event.tool_execution for event in events if isinstance(event, BackendToolExecutionEvent)
    ]

    assert len(resolutions) == 2
    assert {event.tool_call.tool_call_id for event in resolutions} == {"call-1", "call-2"}
    assert len({event.action_group_id for event in resolutions}) == 1
    assert all(event.approved for event in resolutions)
    assert {(event.action_id, event.tool_call_id) for event in executions} == {
        ("action-1", "call-1"),
        ("action-2", "call-2"),
    }
    backend.close()


def test_approval_reconstruction_does_not_group_reused_response_ids_across_turns(
    tmp_path: Path,
) -> None:
    first = _terminal_action_event("action-1", "call-1", "printf first")
    second = _terminal_action_event("action-2", "call-2", "printf second")
    later = _terminal_action_event("action-3", "call-3", "printf later")
    first = first.model_copy(update={"llm_response_id": "reused-response"})
    second = second.model_copy(update={"llm_response_id": "reused-response"})
    later = later.model_copy(update={"llm_response_id": "reused-response"})
    state = _BranchState(uuid.uuid4())
    state.execution_status = ConversationExecutionStatus.FINISHED
    state.events = (
        first,
        second,
        ObservationEvent(
            id="observation-1",
            tool_name="terminal",
            tool_call_id=first.tool_call_id,
            action_id=first.id,
            observation=TerminalObservation(command="printf first", exit_code=0),
        ),
        ObservationEvent(
            id="observation-2",
            tool_name="terminal",
            tool_call_id=second.tool_call_id,
            action_id=second.id,
            observation=TerminalObservation(command="printf second", exit_code=0),
        ),
        later,
        ObservationEvent(
            id="observation-3",
            tool_name="terminal",
            tool_call_id=later.tool_call_id,
            action_id=later.id,
            observation=TerminalObservation(command="printf later", exit_code=0),
        ),
    )
    conversation = _ControlledConversation()
    conversation.state = state
    backend = _backend(
        tmp_path,
        cast(
            ConversationFactory,
            lambda _event_callback, _token_callback: conversation,
        ),
    )

    resolutions = [
        event
        for event in backend.reconcile(
            session_id="session-1",
            known_source_event_ids=frozenset(),
        )
        if isinstance(event, BackendConfirmationResolutionEvent)
    ]

    groups = {event.tool_call.tool_call_id: event.action_group_id for event in resolutions}
    assert groups["call-1"] == groups["call-2"]
    assert groups["call-3"] != groups["call-1"]
    assert len(resolutions) == 3
    backend.close()


def test_persisted_user_rejection_reconstructs_denied_confirmation(
    tmp_path: Path,
) -> None:
    action = _terminal_action_event("action-1", "call-1", "printf rejected")
    rejection = UserRejectObservation(
        id="rejection-1",
        tool_name="terminal",
        tool_call_id=action.tool_call_id,
        action_id=action.id,
    )
    conversation = _ControlledConversation()
    state = _BranchState(conversation.id)
    state.execution_status = ConversationExecutionStatus.FINISHED
    state.events = (action, rejection)
    conversation.state = state
    backend = _backend(
        tmp_path,
        cast(
            ConversationFactory,
            lambda _event_callback, _token_callback: conversation,
        ),
    )

    events = backend.reconcile(
        session_id="session-1",
        known_source_event_ids=frozenset(),
    )
    resolutions = [
        event for event in events if isinstance(event, BackendConfirmationResolutionEvent)
    ]

    assert len(resolutions) == 1
    assert resolutions[0].tool_call.tool_call_id == "call-1"
    assert not resolutions[0].approved
    backend.close()


def test_openhands_adapter_uses_typed_public_state_only() -> None:
    source = inspect.getsource(openhands_sdk_module)

    assert "type(event).__name__" not in source
    assert "getattr(" not in source
    assert "restore_pending" not in source
    assert "self._pending" not in source
    assert "self._captured" not in source


def test_openhands_status_mappings_are_exhaustive_and_fail_closed() -> None:
    assert {status: _backend_lifecycle(status) for status in ConversationExecutionStatus} == {
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
    }
    assert _backend_lifecycle(cast(ConversationExecutionStatus, object())) == (
        BackendLifecycle.ERROR
    )
    assert {status: _task_status(status) for status in ("todo", "in_progress", "done")} == {
        "todo": BackendTaskStatus.TODO,
        "in_progress": BackendTaskStatus.IN_PROGRESS,
        "done": BackendTaskStatus.DONE,
    }
    assert _task_status("unknown-upstream-status") == BackendTaskStatus.TODO


def test_public_conversation_state_projects_a_content_safe_error(
    tmp_path: Path,
) -> None:
    conversation = _ControlledConversation()
    conversation.state.execution_status = ConversationExecutionStatus.ERROR
    backend = _backend(
        tmp_path,
        cast(
            ConversationFactory,
            lambda _event_callback, _token_callback: conversation,
        ),
    )

    events = backend.reconcile(
        session_id="session-1",
        known_source_event_ids=frozenset(),
    )

    assert any(
        isinstance(event, BackendErrorEvent)
        and event.error_code == BackendErrorCode.CONVERSATION_STOPPED
        for event in events
    )
    backend.close()


def test_typed_conversation_error_replaces_generic_stopped_state(
    tmp_path: Path,
) -> None:
    conversation = _ControlledConversation()
    state = _BranchState(conversation.id)
    state.execution_status = ConversationExecutionStatus.ERROR
    state.events = (
        ConversationErrorEvent(
            id="provider-authentication-error",
            source="environment",
            code="OpenAIError",
            detail="Incorrect API key provided: private-provider-detail",
        ),
    )
    conversation.state = state
    backend = _backend(
        tmp_path,
        cast(
            ConversationFactory,
            lambda _event_callback, _token_callback: conversation,
        ),
    )

    events = backend.reconcile(
        session_id="session-1",
        known_source_event_ids=frozenset(),
    )

    errors = [event for event in events if isinstance(event, BackendErrorEvent)]
    assert [event.error_code for event in errors] == [
        BackendErrorCode.PROVIDER_AUTHENTICATION_FAILED
    ]
    assert "private-provider-detail" not in repr(events)
    backend.close()


@pytest.mark.parametrize(
    ("kind", "retryable", "expected"),
    [
        (FailureKind.AUTH, False, BackendErrorCode.PROVIDER_AUTHENTICATION_FAILED),
        (FailureKind.QUOTA, False, BackendErrorCode.PROVIDER_QUOTA_EXHAUSTED),
        (FailureKind.RATE_LIMIT, True, BackendErrorCode.PROVIDER_RATE_LIMITED),
        (FailureKind.CONFIG, False, BackendErrorCode.MODEL_CONFIGURATION_INVALID),
        (FailureKind.TRANSIENT, True, BackendErrorCode.PROVIDER_UNAVAILABLE),
        (FailureKind.AGENT_ACTION, True, BackendErrorCode.ACTION_FAILED),
        (FailureKind.AGENT_ACTION, False, BackendErrorCode.CONVERSATION_STOPPED),
        (FailureKind.INTERNAL, False, BackendErrorCode.WORKER_STOPPED),
        (FailureKind.UNKNOWN, False, BackendErrorCode.UNKNOWN),
    ],
)
def test_openhands_failure_classification_maps_to_stable_diagnostics(
    kind: FailureKind,
    retryable: bool,
    expected: BackendErrorCode,
) -> None:
    classification = ErrorClassification(kind=kind, retryable=retryable)

    assert classified_error_code(classification, fallback=BackendErrorCode.UNKNOWN) == expected


@pytest.mark.parametrize(
    "status",
    [
        ConversationExecutionStatus.IDLE,
        ConversationExecutionStatus.PAUSED,
        ConversationExecutionStatus.FINISHED,
        ConversationExecutionStatus.ERROR,
        ConversationExecutionStatus.STUCK,
        ConversationExecutionStatus.DELETING,
    ],
)
def test_inactive_conversation_with_unmatched_actions_has_unknown_outcome(
    tmp_path: Path,
    status: ConversationExecutionStatus,
) -> None:
    conversation = _ControlledConversation()
    conversation.state = _BranchState(conversation.id)
    conversation.state.execution_status = status
    conversation.state.events = (
        _terminal_action_event("uncertain-action", "uncertain-call", "touch output.txt"),
    )
    backend = _backend(
        tmp_path,
        cast(
            ConversationFactory,
            lambda _event_callback, _token_callback: conversation,
        ),
    )

    events = backend.reconcile(
        session_id="session-1",
        known_source_event_ids=frozenset(),
    )

    assert any(
        isinstance(event, BackendErrorEvent)
        and event.error_code == BackendErrorCode.ACTION_OUTCOME_UNKNOWN
        for event in events
    )
    backend.close()


def test_cancelled_unmatched_action_is_safe_only_at_an_intentional_pause(
    tmp_path: Path,
) -> None:
    conversation = _ControlledConversation()
    conversation.state = _BranchState(conversation.id)
    conversation.state.execution_status = ConversationExecutionStatus.PAUSED
    conversation.state.events = (
        _terminal_action_event("paused-action", "paused-call", "touch output.txt"),
    )
    backend = _backend(
        tmp_path,
        cast(
            ConversationFactory,
            lambda _event_callback, _token_callback: conversation,
        ),
    )
    backend._run_cancelled.set()

    paused_events = backend.reconcile(
        session_id="session-1",
        known_source_event_ids=frozenset(),
    )

    assert not any(
        isinstance(event, BackendErrorEvent)
        and event.error_code == BackendErrorCode.ACTION_OUTCOME_UNKNOWN
        for event in paused_events
    )
    assert backend._interrupted_outcome_error(cast(BaseConversation, conversation)) is None

    conversation.state.execution_status = ConversationExecutionStatus.FINISHED
    finished_events = backend.reconcile(
        session_id="session-1",
        known_source_event_ids=frozenset(),
    )
    blocked_turn = backend.submit_turn(session_id="session-1", prompt="Do not continue")

    assert any(
        isinstance(event, BackendErrorEvent)
        and event.error_code == BackendErrorCode.ACTION_OUTCOME_UNKNOWN
        for event in finished_events
    )
    assert isinstance(blocked_turn[0], BackendErrorEvent)
    assert blocked_turn[0].error_code == BackendErrorCode.ACTION_OUTCOME_UNKNOWN
    backend.close()


def test_real_sdk_test_llm_turn_runs_in_background_and_reconciles_once(
    tmp_path: Path,
) -> None:
    llm = TestLLM.from_messages([_assistant_message("Analysis complete.")])
    backend = _backend(tmp_path, _conversation_factory(tmp_path, llm, tools=[]))

    immediate = backend.submit_turn(session_id="session-1", prompt="Analyze synthetic data")

    assert isinstance(immediate[0], BackendLifecycleEvent)
    assert immediate[0].lifecycle == BackendLifecycle.RUNNING
    events = _wait_for_lifecycle(backend, BackendLifecycle.FINISHED)
    messages = [event.message for event in events if isinstance(event, BackendAgentMessageEvent)]
    assert messages == ["Analysis complete."]
    assert llm.call_count == 1
    source_ids = frozenset(
        event.source_event_id for event in events if event.source_event_id is not None
    )
    assert (
        backend.reconcile(
            session_id="session-1",
            known_source_event_ids=source_ids,
        )
        == ()
    )
    backend.close()


def test_usage_is_reported_as_total_agent_and_condenser_metrics() -> None:
    agent_metrics = Metrics(model_name="agent-model")
    agent_metrics.add_token_usage(100, 20, 5, 2, 32_768, "agent-response")
    condenser_metrics = Metrics(model_name="condenser-model")
    condenser_metrics.add_token_usage(40, 10, 0, 0, 32_768, "condenser-response")
    state = cast(
        ConversationState,
        SimpleNamespace(
            stats=ConversationStats(
                usage_to_metrics={
                    "agent": agent_metrics,
                    "condenser": condenser_metrics,
                }
            )
        ),
    )

    usages = {usage.usage_id: usage for usage in _usage(state)}

    assert set(usages) == {"total", "agent", "condenser"}
    assert usages["total"].call_count == 2
    assert usages["total"].prompt_tokens == 140
    assert usages["agent"].completion_tokens == 20
    assert usages["condenser"].completion_tokens == 10

    first_source = _usage_source_event_id("anchor", usages["agent"])
    assert first_source == _usage_source_event_id("anchor", usages["agent"])
    changed = replace(
        usages["agent"],
        completion_tokens=usages["agent"].completion_tokens + 1,
    )
    assert _usage_source_event_id("anchor", changed) != first_source


def test_usage_is_transient_during_execution_and_durable_at_the_run_boundary(
    tmp_path: Path,
) -> None:
    conversation = _ControlledConversation()
    metrics = Metrics(model_name="agent-model")
    metrics.add_token_usage(100, 20, 0, 0, 32_768, "response-1")
    conversation.state.stats = ConversationStats(usage_to_metrics={"agent": metrics})
    conversation.state.execution_status = ConversationExecutionStatus.RUNNING
    backend = _backend(
        tmp_path,
        cast(
            ConversationFactory,
            lambda _event_callback, _token_callback: conversation,
        ),
    )
    with backend._run_lock:
        backend._execution_active = True

    first_active = backend.reconcile(
        session_id="session-1",
        known_source_event_ids=frozenset(),
    )
    metrics.add_token_usage(40, 10, 0, 0, 32_768, "response-2")
    second_active = backend.reconcile(
        session_id="session-1",
        known_source_event_ids=frozenset(),
    )

    assert not any(isinstance(event, BackendUsageEvent) for event in first_active)
    assert not any(isinstance(event, BackendUsageEvent) for event in second_active)

    with backend._run_lock:
        backend._execution_active = False
    conversation.state.execution_status = ConversationExecutionStatus.FINISHED
    final = backend.reconcile(
        session_id="session-1",
        known_source_event_ids=frozenset(),
    )
    final_usage = {
        event.usage.usage_id: event.usage for event in final if isinstance(event, BackendUsageEvent)
    }

    assert set(final_usage) == {"total", "agent"}
    assert final_usage["total"].call_count == 2
    assert final_usage["total"].prompt_tokens == 140
    assert final_usage["total"].completion_tokens == 30
    final_source_event_ids = frozenset(
        event.source_event_id for event in final if event.source_event_id is not None
    )
    assert (
        backend.reconcile(
            session_id="session-1",
            known_source_event_ids=final_source_event_ids,
        )
        == ()
    )
    backend.close()


@pytest.mark.parametrize(
    "sdk_status",
    [
        ConversationExecutionStatus.IDLE,
        ConversationExecutionStatus.PAUSED,
    ],
)
def test_active_worker_hides_transitional_non_running_sdk_state(
    tmp_path: Path,
    sdk_status: ConversationExecutionStatus,
) -> None:
    conversation = _ControlledConversation()
    conversation.state.execution_status = sdk_status
    backend = _backend(
        tmp_path,
        cast(
            ConversationFactory,
            lambda _event_callback, _token_callback: conversation,
        ),
    )
    with backend._run_lock:
        backend._execution_active = True

    try:
        events = backend.reconcile(
            session_id="session-1",
            known_source_event_ids=frozenset(),
        )
        lifecycle_events = [event for event in events if isinstance(event, BackendLifecycleEvent)]

        assert lifecycle_events
        assert lifecycle_events[-1].lifecycle == BackendLifecycle.RUNNING
    finally:
        with backend._run_lock:
            backend._execution_active = False
        backend.close()


def test_real_sdk_task_tracker_updates_the_shared_task_plan(tmp_path: Path) -> None:
    llm = TestLLM.from_messages(
        [
            Message(
                role="assistant",
                content=[TextContent(text="")],
                tool_calls=[
                    MessageToolCall(
                        id="task-plan-call",
                        name=TaskTrackerTool.name,
                        arguments=json.dumps(
                            {
                                "command": "plan",
                                "task_list": [
                                    {
                                        "title": "Inspect synthetic inputs",
                                        "notes": "Private implementation detail",
                                        "status": "done",
                                    },
                                    {
                                        "title": "Verify the analysis",
                                        "status": "in_progress",
                                    },
                                ],
                            }
                        ),
                        origin="completion",
                    )
                ],
            ),
            _assistant_message("The task plan is ready."),
        ]
    )
    backend = _backend(
        tmp_path,
        _conversation_factory(
            tmp_path,
            llm,
            tools=[Tool(name=TaskTrackerTool.name)],
        ),
    )
    backend.submit_turn(session_id="session-1", prompt="Plan the synthetic analysis")
    group = _wait_for_pending_group(backend)
    backend.resolve_confirmation(
        session_id="session-1",
        action_group_id=group.group_id,
        approved=True,
    )

    events = _wait_for_lifecycle(backend, BackendLifecycle.FINISHED)
    task_plan = next(event for event in events if isinstance(event, BackendTaskPlanEvent))

    assert [(task.title, task.status) for task in task_plan.tasks] == [
        ("Inspect synthetic inputs", BackendTaskStatus.DONE),
        ("Verify the analysis", BackendTaskStatus.IN_PROGRESS),
    ]
    assert "Private implementation detail" not in repr(task_plan)
    assert llm.call_count == 2
    backend.close()


def test_restart_reconciliation_does_not_repeat_the_model_call(tmp_path: Path) -> None:
    conversation_id = uuid.uuid4()
    persistence_dir = tmp_path / "openhands"
    first_llm = TestLLM.from_messages([_assistant_message("Persisted result.")])
    first = _backend(
        tmp_path,
        _conversation_factory(
            tmp_path,
            first_llm,
            tools=[],
            conversation_id=conversation_id,
            persistence_dir=persistence_dir,
        ),
    )
    first.submit_turn(session_id="session-1", prompt="Run once")
    first_events = _wait_for_lifecycle(first, BackendLifecycle.FINISHED)
    first.close()

    restored_llm = TestLLM.from_messages([])
    restored = _backend(
        tmp_path,
        _conversation_factory(
            tmp_path,
            restored_llm,
            tools=[],
            conversation_id=conversation_id,
            persistence_dir=persistence_dir,
        ),
    )
    restored_events = restored.reconcile(
        session_id="session-1",
        known_source_event_ids=frozenset(),
    )

    assert restored_llm.call_count == 0
    assert [event.source_event_id for event in restored_events] == [
        event.source_event_id for event in first_events
    ]
    restored.close()


def test_corrupted_openhands_event_store_fails_closed_without_model_work(
    tmp_path: Path,
) -> None:
    conversation_id = uuid.uuid4()
    persistence_dir = tmp_path / "openhands"
    first_llm = TestLLM.from_messages([_assistant_message("Persisted result.")])
    first = _backend(
        tmp_path,
        _conversation_factory(
            tmp_path,
            first_llm,
            tools=[],
            conversation_id=conversation_id,
            persistence_dir=persistence_dir,
        ),
    )
    first.submit_turn(session_id="session-1", prompt="Persist one result")
    _wait_for_lifecycle(first, BackendLifecycle.FINISHED)
    first.close()

    event_file = sorted(persistence_dir.rglob("event-*.json"))[0]
    event_file.write_text("{", encoding="utf-8")
    restored_llm = TestLLM.from_messages([])
    restored = _backend(
        tmp_path,
        _conversation_factory(
            tmp_path,
            restored_llm,
            tools=[],
            conversation_id=conversation_id,
            persistence_dir=persistence_dir,
        ),
    )

    reconciled = restored.reconcile(
        session_id="session-1",
        known_source_event_ids=frozenset(),
    )
    submitted = restored.submit_turn(session_id="session-1", prompt="Do not continue")

    assert event_file.read_text(encoding="utf-8") == "{"
    assert restored_llm.call_count == 0
    assert any(
        isinstance(event, BackendErrorEvent) and event.error_code == BackendErrorCode.WORKER_STOPPED
        for event in (*reconciled, *submitted)
    )
    assert str(event_file) not in repr((*reconciled, *submitted))
    restored.close()


def test_pending_action_restart_recovers_without_duplicate_model_or_tool_work(
    tmp_path: Path,
) -> None:
    conversation_id = uuid.uuid4()
    persistence_dir = tmp_path / "openhands"
    first_llm = TestLLM.from_messages(
        [_tool_message(("call-restart", "printf restored > restored.txt"))]
    )
    first = _backend(
        tmp_path,
        _conversation_factory(
            tmp_path,
            first_llm,
            tools=[Tool(name=TerminalTool.name)],
            conversation_id=conversation_id,
            persistence_dir=persistence_dir,
        ),
    )
    first.submit_turn(session_id="session-1", prompt="Create one synthetic file")
    original_group = _wait_for_pending_group(first)

    assert first_llm.call_count == 1
    assert not (tmp_path / "workspace" / "restored.txt").exists()
    first.close()

    restored_llm = _StrictToolHistoryTestLLM.from_messages(
        [_assistant_message("The file was created once.")]
    )
    restored = _backend(
        tmp_path,
        _conversation_factory(
            tmp_path,
            restored_llm,
            tools=[Tool(name=TerminalTool.name)],
            conversation_id=conversation_id,
            persistence_dir=persistence_dir,
        ),
    )
    restored_group = restored.pending_action_group(session_id="session-1")

    assert restored_group == original_group
    assert restored_llm.call_count == 0
    restored.resolve_confirmation(
        session_id="session-1",
        action_group_id=original_group.group_id,
        approved=True,
    )
    _wait_for_lifecycle(restored, BackendLifecycle.FINISHED)

    assert (tmp_path / "workspace" / "restored.txt").read_text(encoding="utf-8") == "restored"
    assert restored_llm.call_count == 1
    assert restored.pending_action_group(session_id="session-1") is None
    restored.close()


def test_grouped_pending_actions_restart_with_matched_tool_history(
    tmp_path: Path,
) -> None:
    conversation_id = uuid.uuid4()
    persistence_dir = tmp_path / "openhands"
    first_llm = TestLLM.from_messages(
        [
            _tool_message(
                ("call-restart-first", "printf first > first.txt"),
                ("call-restart-second", "printf second > second.txt"),
            )
        ]
    )
    first = _backend(
        tmp_path,
        _conversation_factory(
            tmp_path,
            first_llm,
            tools=[Tool(name=TerminalTool.name)],
            conversation_id=conversation_id,
            persistence_dir=persistence_dir,
        ),
    )
    first.submit_turn(session_id="session-1", prompt="Create two synthetic files")
    original_group = _wait_for_pending_group(first)
    first.close()

    restored_llm = _StrictToolHistoryTestLLM.from_messages(
        [_assistant_message("Both files were created once.")]
    )
    restored = _backend(
        tmp_path,
        _conversation_factory(
            tmp_path,
            restored_llm,
            tools=[Tool(name=TerminalTool.name)],
            conversation_id=conversation_id,
            persistence_dir=persistence_dir,
        ),
    )
    restored_group = restored.pending_action_group(session_id="session-1")

    assert restored_group == original_group
    restored.resolve_confirmation(
        session_id="session-1",
        action_group_id=original_group.group_id,
        approved=True,
    )
    _wait_for_lifecycle(restored, BackendLifecycle.FINISHED)

    assert (tmp_path / "workspace" / "first.txt").read_text(encoding="utf-8") == "first"
    assert (tmp_path / "workspace" / "second.txt").read_text(encoding="utf-8") == "second"
    assert restored_llm.call_count == 1
    restored.close()


def test_rejected_pending_action_restart_rebuilds_history_before_next_turn(
    tmp_path: Path,
) -> None:
    conversation_id = uuid.uuid4()
    persistence_dir = tmp_path / "openhands"
    first_llm = TestLLM.from_messages(
        [_tool_message(("call-restart-reject", "printf denied > denied.txt"))]
    )
    first = _backend(
        tmp_path,
        _conversation_factory(
            tmp_path,
            first_llm,
            tools=[Tool(name=TerminalTool.name)],
            conversation_id=conversation_id,
            persistence_dir=persistence_dir,
        ),
    )
    first.submit_turn(session_id="session-1", prompt="Create a file")
    original_group = _wait_for_pending_group(first)
    first.close()

    restored_llm = _StrictToolHistoryTestLLM.from_messages(
        [_assistant_message("The rejected action was not executed.")]
    )
    restored = _backend(
        tmp_path,
        _conversation_factory(
            tmp_path,
            restored_llm,
            tools=[Tool(name=TerminalTool.name)],
            conversation_id=conversation_id,
            persistence_dir=persistence_dir,
        ),
    )
    rejected = restored.resolve_confirmation(
        session_id="session-1",
        action_group_id=original_group.group_id,
        approved=False,
    )

    assert any(
        isinstance(event, BackendConfirmationResolutionEvent) and not event.approved
        for event in rejected
    )
    restored.submit_turn(session_id="session-1", prompt="Continue without creating the file")
    _wait_for_lifecycle(restored, BackendLifecycle.FINISHED)

    assert not (tmp_path / "workspace" / "denied.txt").exists()
    assert restored_llm.call_count == 1
    restored.close()


def test_gateway_interface_handoff_preserves_real_sdk_session_exactly_once(
    tmp_path: Path,
) -> None:
    conversation_id = uuid.uuid4()
    first_llm = TestLLM.from_messages(
        [_tool_message(("call-gateway-handoff", "printf handoff > handoff.txt"))]
    )
    browser_gateway = _openhands_gateway(
        tmp_path,
        first_llm,
        conversation_id=conversation_id,
    )
    chat = SessionCommand(
        command_id="browser-chat",
        session_id="session-1",
        kind=CommandKind.CHAT,
        actor_id="test-user",
        created_at="2026-07-25T00:00:00Z",
        payload={"prompt": "Create one synthetic handoff file"},
    )
    browser_gateway.handle(chat)
    browser_projection = _wait_for_gateway_pending_approval(browser_gateway)
    pending = browser_projection.pending_approval
    assert pending is not None

    restored_llm = _StrictToolHistoryTestLLM.from_messages(
        [_assistant_message("The handoff file was created once.")]
    )
    cli_gateway = _openhands_gateway(
        tmp_path,
        restored_llm,
        conversation_id=conversation_id,
    )
    approve = SessionCommand(
        command_id="cli-approve",
        session_id="session-1",
        kind=CommandKind.APPROVE,
        actor_id="test-user",
        created_at="2026-07-25T00:00:01Z",
        payload={"target_type": "action-set", "target_id": pending.group_id},
    )

    with pytest.raises(SessionOwnershipError, match="active in another Heartwood process"):
        cli_gateway.handle(approve)

    browser_gateway.stop()
    first_approval = cli_gateway.handle(approve)
    _wait_for_gateway_lifecycle(cli_gateway, "finished")
    retried_approval = cli_gateway.handle(approve)

    assert retried_approval.events == first_approval.events
    assert first_approval.replayed is False
    assert retried_approval.replayed is True
    assert first_llm.call_count == 1
    assert restored_llm.call_count == 1
    assert (tmp_path / "handoff.txt").read_text(encoding="utf-8") == "handoff"

    audit_command = SessionCommand(
        command_id="cli-audit",
        session_id="session-1",
        kind=CommandKind.AUDIT_EXPORT,
        actor_id="test-user",
        created_at="2026-07-25T00:00:02Z",
    )
    cli_gateway.handle(audit_command)
    replayed = cli_gateway.replay_events(session_id="session-1")
    repeated_replay = cli_gateway.replay_events(session_id="session-1")
    audit = cli_gateway.audit_export("session-1")
    repeated_audit = cli_gateway.audit_export("session-1")
    cli_projection = cli_gateway.session_projection(session_id="session-1")
    browser_persisted_projection = browser_gateway.persisted_session_projection(
        session_id="session-1"
    )

    assert replayed == repeated_replay
    assert [event.sequence for event in replayed] == list(range(len(replayed)))
    assert audit["content"] == repeated_audit["content"]
    assert _durable_projection(cli_projection) == _durable_projection(browser_persisted_projection)
    cli_gateway.stop()


def test_completed_tool_turn_restarts_without_repeating_model_or_tool_work(
    tmp_path: Path,
) -> None:
    conversation_id = uuid.uuid4()
    persistence_dir = tmp_path / "openhands"
    first_llm = TestLLM.from_messages(
        [
            _tool_message(("call-once", "printf once > once.txt")),
            _assistant_message("The file was created."),
        ]
    )
    first = _backend(
        tmp_path,
        _conversation_factory(
            tmp_path,
            first_llm,
            tools=[Tool(name=TerminalTool.name)],
            conversation_id=conversation_id,
            persistence_dir=persistence_dir,
        ),
    )
    first.submit_turn(session_id="session-1", prompt="Create one file")
    group = _wait_for_pending_group(first)
    first.resolve_confirmation(
        session_id="session-1",
        action_group_id=group.group_id,
        approved=True,
    )
    original_events = _wait_for_lifecycle(first, BackendLifecycle.FINISHED)
    first.close()

    restored_llm = TestLLM.from_messages([])
    restored = _backend(
        tmp_path,
        _conversation_factory(
            tmp_path,
            restored_llm,
            tools=[Tool(name=TerminalTool.name)],
            conversation_id=conversation_id,
            persistence_dir=persistence_dir,
        ),
    )
    restored_events = restored.reconcile(
        session_id="session-1",
        known_source_event_ids=frozenset(),
    )

    assert (tmp_path / "workspace" / "once.txt").read_text(encoding="utf-8") == "once"
    assert restored_llm.call_count == 0
    assert restored.pending_action_group(session_id="session-1") is None
    assert [event.source_event_id for event in restored_events] == [
        event.source_event_id for event in original_events
    ]
    restored.close()


def test_real_sdk_grouped_approval_executes_every_action_once(tmp_path: Path) -> None:
    llm = TestLLM.from_messages(
        [
            _tool_message(
                ("call-1", "printf first > first.txt"),
                ("call-2", "printf second > second.txt"),
            ),
            _assistant_message("Both files were created."),
        ]
    )
    backend = _backend(
        tmp_path,
        _conversation_factory(tmp_path, llm, tools=[Tool(name=TerminalTool.name)]),
    )
    backend.submit_turn(session_id="session-1", prompt="Create two synthetic files")
    group = _wait_for_pending_group(backend)

    resolved = backend.resolve_confirmation(
        session_id="session-1",
        action_group_id=group.group_id,
        approved=True,
    )

    assert [event.kind for event in resolved] == [
        BackendEventKind.CONFIRMATION_RESOLVED,
        BackendEventKind.CONFIRMATION_RESOLVED,
        BackendEventKind.LIFECYCLE,
    ]
    assert isinstance(resolved[-1], BackendLifecycleEvent)
    assert resolved[-1].lifecycle == BackendLifecycle.RUNNING
    _wait_for_lifecycle(backend, BackendLifecycle.FINISHED)
    assert (tmp_path / "workspace" / "first.txt").read_text(encoding="utf-8") == "first"
    assert (tmp_path / "workspace" / "second.txt").read_text(encoding="utf-8") == "second"
    assert llm.call_count == 2
    backend.close()


def test_real_sdk_grouped_rejection_executes_nothing_and_does_not_continue(
    tmp_path: Path,
) -> None:
    llm = TestLLM.from_messages(
        [
            _tool_message(("call-1", "printf rejected > rejected.txt")),
            _assistant_message("This response must not be consumed."),
        ]
    )
    backend = _backend(
        tmp_path,
        _conversation_factory(tmp_path, llm, tools=[Tool(name=TerminalTool.name)]),
    )
    backend.submit_turn(session_id="session-1", prompt="Create a file")
    group = _wait_for_pending_group(backend)

    resolved = backend.resolve_confirmation(
        session_id="session-1",
        action_group_id=group.group_id,
        approved=False,
    )

    assert [event.kind for event in resolved[:1]] == [BackendEventKind.CONFIRMATION_RESOLVED]
    assert not (tmp_path / "workspace" / "rejected.txt").exists()
    assert llm.call_count == 1
    assert backend.pending_action_group(session_id="session-1") is None
    backend.close()


def test_active_run_can_be_steered_paused_and_resumed(tmp_path: Path) -> None:
    conversation = _ControlledConversation()
    backend = _backend(
        tmp_path,
        cast(
            ConversationFactory,
            lambda _event_callback, _token_callback: conversation,
        ),
    )

    submitted = Event()

    def submit() -> None:
        backend.submit_turn(session_id="session-1", prompt="Start")
        submitted.set()

    submitter = Thread(target=submit, daemon=True)
    submitter.start()
    assert submitted.wait(timeout=2)
    submitter.join(timeout=2)
    assert not submitter.is_alive()
    assert conversation.started.wait(timeout=2)
    run_thread = backend._run_thread
    backend._start_run(
        session_id="session-1",
        conversation=cast(BaseConversation, conversation),
    )
    assert backend._run_thread is run_thread

    active_state = _BranchState(conversation.id)
    active_state.execution_status = ConversationExecutionStatus.RUNNING
    active_state.events = (_terminal_action_event("active-action", "active-call", "printf safe"),)
    conversation.state = active_state
    assert backend.pending_action_group(session_id="session-1") is None
    assert backend.submit_turn(session_id="session-1", prompt="Steer") == ()
    paused_directly = backend.pause(session_id="session-1")
    assert conversation.stopped.wait(timeout=2)
    paused = (
        *paused_directly,
        *backend.reconcile(
            session_id="session-1",
            known_source_event_ids=frozenset(),
        ),
    )

    assert conversation.messages == [("Start", "heartwood-user"), ("Steer", "heartwood-user")]
    assert any(
        isinstance(event, BackendLifecycleEvent) and event.lifecycle == BackendLifecycle.PAUSED
        for event in paused
    )
    resumed = backend.resume(session_id="session-1")
    assert isinstance(resumed[0], BackendLifecycleEvent)
    assert resumed[0].lifecycle == BackendLifecycle.RUNNING
    backend.close()


def test_resume_waits_for_an_interrupt_still_transitioning_to_paused(
    tmp_path: Path,
) -> None:
    conversation = _DelayedInterruptConversation()
    backend = _backend(
        tmp_path,
        cast(
            ConversationFactory,
            lambda _event_callback, _token_callback: conversation,
        ),
    )
    backend.submit_turn(session_id="session-1", prompt="Start")
    assert conversation.started.wait(timeout=2)

    paused = backend.pause(session_id="session-1")
    assert conversation.stopped.is_set()
    assert any(
        isinstance(event, BackendLifecycleEvent) and event.lifecycle == BackendLifecycle.PAUSED
        for event in paused
    )
    resumed = backend.resume(session_id="session-1")

    assert isinstance(resumed[0], BackendLifecycleEvent)
    assert resumed[0].lifecycle == BackendLifecycle.RUNNING
    backend.close()


def test_pause_reports_failure_without_acknowledging_an_unstopped_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conversation = _StubbornConversation()
    backend = _backend(
        tmp_path,
        cast(
            ConversationFactory,
            lambda _event_callback, _token_callback: conversation,
        ),
    )
    monkeypatch.setattr(
        openhands_sdk_module,
        "_AGENT_WORKER_TRANSITION_TIMEOUT_SECONDS",
        0.05,
    )
    backend.submit_turn(session_id="session-1", prompt="Start")
    assert conversation.started.wait(timeout=2)

    paused = backend.pause(session_id="session-1")

    assert len(paused) == 1
    assert isinstance(paused[0], BackendErrorEvent)
    assert paused[0].error_code == BackendErrorCode.WORKER_STOPPED
    conversation.release.set()
    assert conversation.finished.wait(timeout=2)
    backend.close()


def test_user_pause_does_not_resume_an_internal_pending_view_repair(
    tmp_path: Path,
) -> None:
    conversation: _ViewRepairPauseConversation | None = None

    def factory(
        event_callback: Callable[[OpenHandsEvent], None],
        _token_callback: Callable[[LLMStreamChunk], None],
    ) -> _ViewRepairPauseConversation:
        nonlocal conversation
        conversation = _ViewRepairPauseConversation(event_callback)
        return conversation

    backend = _backend(tmp_path, cast(ConversationFactory, factory))
    group = backend.pending_action_group(session_id="session-1")
    assert group is not None
    backend.resolve_confirmation(
        session_id="session-1",
        action_group_id=group.group_id,
        approved=True,
    )
    assert conversation is not None
    assert conversation.view_repair_boundary.wait(timeout=2)
    assert conversation.pause_calls == 1

    paused = backend.pause(session_id="session-1")

    assert conversation.stopped.wait(timeout=2)
    assert conversation.run_count == 1
    assert not conversation.second_run_started.is_set()
    assert any(
        isinstance(event, BackendLifecycleEvent) and event.lifecycle == BackendLifecycle.PAUSED
        for event in paused
    )
    backend.close()


def test_internal_view_repair_remains_one_running_interface_operation(
    tmp_path: Path,
) -> None:
    conversation: _ViewRepairPauseConversation | None = None

    def factory(
        event_callback: Callable[[OpenHandsEvent], None],
        _token_callback: Callable[[LLMStreamChunk], None],
    ) -> _ViewRepairPauseConversation:
        nonlocal conversation
        conversation = _ViewRepairPauseConversation(event_callback)
        return conversation

    backend = _backend(tmp_path, cast(ConversationFactory, factory))
    try:
        group = backend.pending_action_group(session_id="session-1")
        assert group is not None
        backend.resolve_confirmation(
            session_id="session-1",
            action_group_id=group.group_id,
            approved=True,
        )
        assert conversation is not None
        assert conversation.view_repair_boundary.wait(timeout=2)

        events = backend.reconcile(
            session_id="session-1",
            known_source_event_ids=frozenset(),
        )
        lifecycle_events = [event for event in events if isinstance(event, BackendLifecycleEvent)]

        assert lifecycle_events
        assert lifecycle_events[-1].lifecycle == BackendLifecycle.RUNNING
        assert not any(event.lifecycle == BackendLifecycle.PAUSED for event in lifecycle_events)
        assert (
            backend._translate_event(
                PauseEvent(id="internal-view-repair-pause"),
                session_id="session-1",
            )
            == ()
        )

        conversation.release.set()
        assert conversation.stopped.wait(timeout=2)
        assert conversation.second_run_started.wait(timeout=2)
    finally:
        if conversation is not None:
            conversation.release.set()
        backend.close()


def test_pause_wins_before_an_automatic_run_is_admitted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conversation: _AutomaticRepairConversation | None = None

    def factory(
        event_callback: Callable[[OpenHandsEvent], None],
        _token_callback: Callable[[LLMStreamChunk], None],
    ) -> _AutomaticRepairConversation:
        nonlocal conversation
        conversation = _AutomaticRepairConversation(event_callback)
        return conversation

    backend = _backend(tmp_path, cast(ConversationFactory, factory))
    group = backend.pending_action_group(session_id="session-1")
    assert group is not None
    original_complete = backend._complete_pending_action_view_repair
    before_admission = Event()
    release_admission = Event()

    def stop_before_admission(current: BaseConversation) -> bool:
        should_continue = original_complete(current)
        assert should_continue
        before_admission.set()
        assert release_admission.wait(timeout=2)
        return should_continue

    monkeypatch.setattr(
        backend,
        "_complete_pending_action_view_repair",
        stop_before_admission,
    )
    backend.resolve_confirmation(
        session_id="session-1",
        action_group_id=group.group_id,
        approved=True,
    )
    assert before_admission.wait(timeout=2)
    paused: list[tuple[BackendEvent, ...]] = []
    pause_thread = Thread(
        target=lambda: paused.append(backend.pause(session_id="session-1")),
        daemon=True,
    )
    pause_thread.start()
    assert conversation is not None
    assert conversation.interrupt_called.wait(timeout=2)
    release_admission.set()
    pause_thread.join(timeout=2)

    assert not pause_thread.is_alive()
    assert conversation.run_count == 1
    assert not conversation.second_run_started.is_set()
    assert any(
        isinstance(event, BackendLifecycleEvent) and event.lifecycle == BackendLifecycle.PAUSED
        for event in paused[0]
    )
    backend.close()


@pytest.mark.parametrize("operation", ["pause", "close"])
def test_pause_or_close_cancels_an_admitted_run_before_openhands_starts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    conversation: _AutomaticRepairConversation | None = None

    def factory(
        event_callback: Callable[[OpenHandsEvent], None],
        _token_callback: Callable[[LLMStreamChunk], None],
    ) -> _AutomaticRepairConversation:
        nonlocal conversation
        conversation = _AutomaticRepairConversation(event_callback)
        return conversation

    backend = _backend(tmp_path, cast(ConversationFactory, factory))
    group = backend.pending_action_group(session_id="session-1")
    assert group is not None
    original_admit = backend._admit_conversation_run
    admitted = Event()
    release_admission = Event()

    def block_after_admission(
        current: BaseConversation,
    ) -> asyncio.Task[object] | None:
        run = original_admit(current)
        assert conversation is not None
        if conversation.run_count == 1:
            admitted.set()
            assert release_admission.wait(timeout=2)
        return run

    monkeypatch.setattr(backend, "_admit_conversation_run", block_after_admission)
    backend.resolve_confirmation(
        session_id="session-1",
        action_group_id=group.group_id,
        approved=True,
    )
    assert admitted.wait(timeout=2)
    result: list[tuple[BackendEvent, ...]] = []

    def stop_backend() -> None:
        if operation == "pause":
            result.append(backend.pause(session_id="session-1"))
        else:
            backend.close()

    stop_thread = Thread(target=stop_backend, daemon=True)
    stop_thread.start()
    assert conversation is not None
    assert conversation.interrupt_called.wait(timeout=2)
    release_admission.set()
    stop_thread.join(timeout=2)

    assert not stop_thread.is_alive()
    assert conversation.run_count == 1
    assert not conversation.second_run_started.is_set()
    if operation == "pause":
        assert any(
            isinstance(event, BackendLifecycleEvent) and event.lifecycle == BackendLifecycle.PAUSED
            for event in result[0]
        )
        backend.close()
    else:
        assert conversation.closed


def test_confirmation_continuation_waits_for_the_previous_run_boundary(
    tmp_path: Path,
) -> None:
    conversation = _ConfirmationBoundaryConversation()
    backend = _backend(
        tmp_path,
        cast(
            ConversationFactory,
            lambda _event_callback, _token_callback: conversation,
        ),
    )
    backend.submit_turn(session_id="session-1", prompt="Propose one action")
    assert conversation.pending.wait(timeout=2)
    group = _wait_for_pending_group(backend)

    continued = backend.resolve_confirmation(
        session_id="session-1",
        action_group_id=group.group_id,
        approved=True,
    )

    assert isinstance(continued[-1], BackendLifecycleEvent)
    assert continued[-1].lifecycle == BackendLifecycle.RUNNING
    assert conversation.continued.wait(timeout=2)
    assert conversation.run_count == 2
    backend.close()


def test_persisted_progress_is_published_before_the_run_finishes(tmp_path: Path) -> None:
    conversation = _ProgressConversation()
    emitted: list[BackendEvent] = []
    progress_published = Event()
    backend = _backend(
        tmp_path,
        cast(
            ConversationFactory,
            lambda _event_callback, _token_callback: conversation,
        ),
    )

    def publish(events: tuple[BackendEvent, ...]) -> None:
        emitted.extend(events)
        if any(event.kind == BackendEventKind.AGENT_MESSAGE for event in events):
            progress_published.set()

    backend.bind_runtime(event_sink=publish, token_sink=lambda _delta: None)
    backend.submit_turn(session_id="session-1", prompt="Start")
    assert conversation.started.wait(timeout=2)
    assert progress_published.wait(timeout=2)

    assert not conversation.finished.is_set()
    conversation.release.set()
    assert conversation.finished.wait(timeout=2)
    backend.close()


def test_synchronous_progress_sink_does_not_block_the_openhands_event_loop(
    tmp_path: Path,
) -> None:
    conversation = _ProgressConversation()
    sink_started = Event()
    release_sink = Event()
    backend = _backend(
        tmp_path,
        cast(
            ConversationFactory,
            lambda _event_callback, _token_callback: conversation,
        ),
    )

    def publish(events: tuple[BackendEvent, ...]) -> None:
        if any(event.kind == BackendEventKind.AGENT_MESSAGE for event in events):
            sink_started.set()
            if not release_sink.wait(timeout=2):
                raise TimeoutError("test progress sink was not released")

    backend.bind_runtime(event_sink=publish, token_sink=lambda _delta: None)
    try:
        backend.submit_turn(session_id="session-1", prompt="Start")
        assert conversation.started.wait(timeout=2)
        assert sink_started.wait(timeout=2)
        conversation.release.set()
        assert conversation.finished.wait(timeout=2)
    finally:
        release_sink.set()
        backend.close()


def test_stale_running_model_turn_fails_closed_without_repeating_provider_work(
    tmp_path: Path,
) -> None:
    conversation = _ControlledConversation()
    conversation.state.execution_status = ConversationExecutionStatus.RUNNING
    backend = _backend(
        tmp_path,
        cast(
            ConversationFactory,
            lambda _event_callback, _token_callback: conversation,
        ),
    )

    events = backend.reconcile(
        session_id="session-1",
        known_source_event_ids=frozenset(),
    )
    assert isinstance(events[0], BackendLifecycleEvent)
    assert events[0].lifecycle == BackendLifecycle.ERROR
    assert any(
        isinstance(event, BackendErrorEvent)
        and event.error_code == BackendErrorCode.AGENT_OUTCOME_UNKNOWN
        for event in events
    )

    resumed = backend.resume(session_id="session-1")
    assert isinstance(resumed[0], BackendErrorEvent)
    assert resumed[0].error_code == BackendErrorCode.AGENT_OUTCOME_UNKNOWN
    submitted = backend.submit_turn(session_id="session-1", prompt="Do not repeat")
    assert isinstance(submitted[0], BackendErrorEvent)
    assert submitted[0].error_code == BackendErrorCode.AGENT_OUTCOME_UNKNOWN
    assert not conversation.started.is_set()

    conversation.state.execution_status = ConversationExecutionStatus.FINISHED
    invalid_resume = backend.resume(session_id="session-1")
    assert isinstance(invalid_resume[0], BackendErrorEvent)
    assert invalid_resume[0].error_code == BackendErrorCode.INVALID_STATE
    backend.close()


def test_interrupted_unmatched_action_requires_explicit_recovery(
    tmp_path: Path,
) -> None:
    conversation = _ControlledConversation()
    conversation.state = _BranchState(conversation.id)
    conversation.state.execution_status = ConversationExecutionStatus.RUNNING
    conversation.state.events = (
        _terminal_action_event("uncertain-action", "uncertain-call", "touch output.txt"),
    )
    backend = _backend(
        tmp_path,
        cast(
            ConversationFactory,
            lambda _event_callback, _token_callback: conversation,
        ),
    )

    events = backend.reconcile(
        session_id="session-1",
        known_source_event_ids=frozenset(),
    )

    assert any(
        isinstance(event, BackendLifecycleEvent) and event.lifecycle == BackendLifecycle.ERROR
        for event in events
    )
    assert any(
        isinstance(event, BackendErrorEvent)
        and event.error_code == BackendErrorCode.ACTION_OUTCOME_UNKNOWN
        for event in events
    )
    assert backend.pending_action_group(session_id="session-1") is None
    blocked_turn = backend.submit_turn(session_id="session-1", prompt="Do not continue")
    blocked_resume = backend.resume(session_id="session-1")
    assert isinstance(blocked_turn[0], BackendErrorEvent)
    assert blocked_turn[0].error_code == BackendErrorCode.ACTION_OUTCOME_UNKNOWN
    assert isinstance(blocked_resume[0], BackendErrorEvent)
    assert blocked_resume[0].error_code == BackendErrorCode.ACTION_OUTCOME_UNKNOWN


def test_background_failure_finishes_in_error_instead_of_running(
    tmp_path: Path,
) -> None:
    conversation = _FailingConversation()
    emitted: list[BackendEvent] = []
    backend = _backend(
        tmp_path,
        cast(
            ConversationFactory,
            lambda _event_callback, _token_callback: conversation,
        ),
    )
    backend.bind_runtime(
        event_sink=lambda events: emitted.extend(events),
        token_sink=lambda _delta: None,
    )

    backend.submit_turn(session_id="session-1", prompt="Fail safely")
    events = _wait_for_lifecycle(backend, BackendLifecycle.ERROR)

    assert any(event.kind == BackendEventKind.ERROR for event in emitted)
    assert all(
        event.lifecycle != BackendLifecycle.RUNNING
        for event in events
        if isinstance(event, BackendLifecycleEvent)
    )
    backend.close()


def test_close_does_not_release_a_worker_that_failed_to_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conversation = _StubbornConversation()
    backend = _backend(
        tmp_path,
        cast(
            ConversationFactory,
            lambda _event_callback, _token_callback: conversation,
        ),
    )
    backend.submit_turn(session_id="session-1", prompt="Wait")
    assert conversation.started.wait(timeout=2)
    monkeypatch.setattr(
        "heartwood.gateway._openhands_sdk._AGENT_WORKER_SHUTDOWN_TIMEOUT_SECONDS",
        0.01,
    )

    with pytest.raises(OpenHandsSdkError, match="session ownership remains active"):
        backend.close()

    assert not conversation.closed
    conversation.release.set()
    assert conversation.finished.wait(timeout=2)
    backend.close()


def test_close_of_an_active_run_does_not_emit_a_false_worker_error(
    tmp_path: Path,
) -> None:
    conversation = _ControlledConversation()
    emitted: list[BackendEvent] = []
    backend = _backend(
        tmp_path,
        cast(
            ConversationFactory,
            lambda _event_callback, _token_callback: conversation,
        ),
    )
    backend.bind_runtime(
        event_sink=lambda events: emitted.extend(events),
        token_sink=lambda _delta: None,
    )
    backend.submit_turn(session_id="session-1", prompt="Stop cleanly")
    assert conversation.started.wait(timeout=2)

    backend.close()

    assert conversation.closed
    assert not any(
        isinstance(event, BackendErrorEvent) and event.error_code == BackendErrorCode.WORKER_STOPPED
        for event in emitted
    )


def test_view_repair_completion_uses_the_owned_conversation_during_close(
    tmp_path: Path,
) -> None:
    conversation = _ControlledConversation()
    backend = _backend(
        tmp_path,
        cast(
            ConversationFactory,
            lambda _event_callback, _token_callback: conversation,
        ),
    )
    backend.reconcile(session_id="session-1", known_source_event_ids=frozenset())
    conversation.state.execution_status = ConversationExecutionStatus.PAUSED
    with backend._view_repair_lock:
        backend._view_repair_boundary_reached = True
        backend._view_repair_paused_internally = True
    with backend._conversation_lock:
        backend._conversation_closing = True

    assert backend._complete_pending_action_view_repair(cast(BaseConversation, conversation))

    with backend._conversation_lock:
        backend._conversation_closing = False
    backend.close()


def test_closed_backend_does_not_resurrect_its_conversation(tmp_path: Path) -> None:
    conversation = _ControlledConversation()
    factory_calls = 0

    def factory(
        _event_callback: Callable[[OpenHandsEvent], None],
        _token_callback: Callable[[LLMStreamChunk], None],
    ) -> BaseConversation:
        nonlocal factory_calls
        factory_calls += 1
        return cast(BaseConversation, conversation)

    backend = _backend(tmp_path, factory)
    backend.reconcile(session_id="session-1", known_source_event_ids=frozenset())
    backend.close()

    events = backend.reconcile(
        session_id="session-1",
        known_source_event_ids=frozenset(),
    )

    assert factory_calls == 1
    assert conversation.closed
    assert len(events) == 1
    assert isinstance(events[0], BackendErrorEvent)
    assert events[0].error_code == BackendErrorCode.WORKER_STOPPED


def test_backend_rejects_conversation_access_while_close_is_in_progress(
    tmp_path: Path,
) -> None:
    conversation = _BlockingShutdownConversation()
    backend = _backend(
        tmp_path,
        cast(
            ConversationFactory,
            lambda _event_callback, _token_callback: conversation,
        ),
    )
    backend.submit_turn(session_id="session-1", prompt="Wait")
    assert conversation.started.wait(timeout=2)
    close_errors: list[Exception] = []

    def close_backend() -> None:
        try:
            backend.close()
        except Exception as error:  # pragma: no cover - asserted below
            close_errors.append(error)

    close_thread = Thread(target=close_backend)
    close_thread.start()
    assert conversation.shutdown_started.wait(timeout=2)

    events = backend.reconcile(
        session_id="session-1",
        known_source_event_ids=frozenset(),
    )

    assert len(events) == 1
    assert isinstance(events[0], BackendErrorEvent)
    assert events[0].error_code == BackendErrorCode.WORKER_STOPPED
    conversation.allow_shutdown.set()
    close_thread.join(timeout=2)
    assert not close_thread.is_alive()
    assert not close_errors
    assert conversation.closed


def test_sequential_specialized_agent_workflow_exposes_parent_lineage(
    tmp_path: Path,
) -> None:
    specialist_name = "research-planner"
    parent_llm = TestLLM.from_messages(
        [
            Message(
                role="assistant",
                content=[TextContent(text="")],
                tool_calls=[
                    MessageToolCall(
                        id="task-call-1",
                        name="task",
                        arguments=(
                            '{"description":"Plan analysis","prompt":"Plan a synthetic '
                            f'cohort analysis","subagent_type":"{specialist_name}"}}'
                        ),
                        origin="completion",
                    )
                ],
            ),
            _assistant_message("Validated analysis plan."),
            _assistant_message("The sequential plan is ready."),
        ]
    )
    backend = OpenHandsSdkBackend(
        profile=_local_profile(),
        workspace=tmp_path / "workspace",
        skills_dir=tmp_path / "skills",
        persistence_dir=tmp_path / "openhands",
        conversation_key="specialist-lineage",
        specialist_catalog=_specialist_catalog(),
        env={},
        conversation_factory=_conversation_factory(
            tmp_path,
            parent_llm,
            tools=[_specialist_tool()],
        ),
    )
    backend._register_specialized_agents()
    backend.submit_turn(session_id="session-1", prompt="Develop an analysis plan")
    group = _wait_for_pending_group(backend)
    continued = backend.resolve_confirmation(
        session_id="session-1",
        action_group_id=group.group_id,
        approved=True,
    )
    events = (*continued, *_wait_for_lifecycle(backend, BackendLifecycle.FINISHED))
    subagents = [event.subagent for event in events if isinstance(event, BackendSubagentEvent)]

    assert {item.status for item in subagents} == {
        BackendSubagentStatus.PROPOSED,
        BackendSubagentStatus.RUNNING,
        BackendSubagentStatus.COMPLETED,
    }
    assert {item.invocation_id for item in subagents} == {"task-call-1"}
    assert {item.task_id for item in subagents} == {None, "task_00000001"}
    assert all(item.parent_session_id == "session-1" for item in subagents)
    assert subagents[0].parent_action_id
    assert parent_llm.call_count == 2
    backend.close()


@pytest.mark.parametrize(
    ("specialist_id", "delegated_prompt", "specialist_result"),
    [
        (
            "research-planner",
            "Plan a synthetic cohort analysis.",
            "Define the cohort, validate denominators, and record assumptions.",
        ),
        (
            "data-quality-reviewer",
            "Review supplied aggregate missingness and duplicate counts.",
            "Verify missingness denominators and resolve duplicate person records.",
        ),
        (
            "cohort-feature-reviewer",
            "Review supplied index dates, windows, and feature definitions.",
            "Resolve index-date ambiguity and verify that features precede outcomes.",
        ),
        (
            "statistical-reviewer",
            "Review a supplied train-test split and held-out model metrics.",
            "Use patient-level splitting and report uncertainty for held-out metrics.",
        ),
        (
            "reproducibility-reviewer",
            "Review supplied revisions, parameters, seeds, and output checks.",
            "Record immutable inputs, the environment lock, seeds, and validation commands.",
        ),
    ],
)
def test_packaged_advisory_specialists_return_output_to_the_parent_agent(
    tmp_path: Path,
    specialist_id: str,
    delegated_prompt: str,
    specialist_result: str,
) -> None:
    llm = TestLLM.from_messages(
        [
            Message(
                role="assistant",
                content=[TextContent(text="")],
                tool_calls=[
                    MessageToolCall(
                        id="packaged-task-call",
                        name="task",
                        arguments=(
                            '{"description":"Review synthetic evidence","prompt":'
                            f'{json.dumps(delegated_prompt)},"subagent_type":'
                            f"{json.dumps(specialist_id)}}}"
                        ),
                        origin="completion",
                    )
                ],
            ),
            _assistant_message(specialist_result),
            _assistant_message("The specialist review was incorporated into the parent task."),
        ]
    )
    backend = OpenHandsSdkBackend(
        profile=_local_profile(),
        workspace=tmp_path / "workspace",
        skills_dir=tmp_path / "skills",
        persistence_dir=tmp_path / "openhands",
        conversation_key="packaged-specialist",
        specialist_catalog=_specialist_catalog(),
        env={},
        conversation_factory=_conversation_factory(
            tmp_path,
            llm,
            tools=[_specialist_tool()],
        ),
    )
    backend._register_specialized_agents()
    backend.submit_turn(session_id="session-1", prompt="Develop an analysis plan")
    group = _wait_for_pending_group(backend)
    continued = backend.resolve_confirmation(
        session_id="session-1",
        action_group_id=group.group_id,
        approved=True,
    )
    events = (*continued, *_wait_for_lifecycle(backend, BackendLifecycle.FINISHED))

    expected_label = _specialist_catalog().role(specialist_id).label
    assert any(
        isinstance(event, BackendSubagentEvent)
        and event.subagent.agent_name == specialist_id
        and event.subagent.role_label == expected_label
        and event.subagent.status == BackendSubagentStatus.COMPLETED
        for event in events
    )
    assert any(
        isinstance(event, BackendAgentMessageEvent)
        and event.message == "The specialist review was incorporated into the parent task."
        for event in events
    )
    assert llm.call_count == 2
    backend.close()


def test_parent_cancels_specialist_delegation_before_the_child_starts(
    tmp_path: Path,
) -> None:
    llm = TestLLM.from_messages(
        [
            Message(
                role="assistant",
                content=[TextContent(text="")],
                tool_calls=[
                    MessageToolCall(
                        id="rejected-specialist-call",
                        name="task",
                        arguments=(
                            '{"description":"Review synthetic evidence","prompt":'
                            '"Review supplied aggregate counts","subagent_type":'
                            '"data-quality-reviewer"}'
                        ),
                        origin="completion",
                    )
                ],
            ),
            _assistant_message("This child response must not be consumed."),
        ]
    )
    backend = OpenHandsSdkBackend(
        profile=_local_profile(),
        workspace=tmp_path / "workspace",
        skills_dir=tmp_path / "skills",
        persistence_dir=tmp_path / "openhands",
        conversation_key="rejected-specialist",
        specialist_catalog=_specialist_catalog(),
        env={},
        conversation_factory=_conversation_factory(
            tmp_path,
            llm,
            tools=[_specialist_tool()],
        ),
    )
    backend._register_specialized_agents()
    backend.submit_turn(session_id="session-1", prompt="Review the aggregates")
    group = _wait_for_pending_group(backend)

    events = backend.resolve_confirmation(
        session_id="session-1",
        action_group_id=group.group_id,
        approved=False,
    )

    assert llm.call_count == 1
    assert all(
        not isinstance(event, BackendSubagentEvent)
        or event.subagent.status != BackendSubagentStatus.COMPLETED
        for event in events
    )
    assert backend.pending_action_group(session_id="session-1") is None
    backend.close()


def test_specialist_resume_fails_closed_until_lineage_is_restart_durable(
    tmp_path: Path,
) -> None:
    llm = TestLLM.from_messages(
        [
            _task_message(
                "initial-review-call",
                description="Review cohort definition",
                prompt="Review the supplied synthetic cohort definition.",
                specialist_id="cohort-feature-reviewer",
            ),
            _assistant_message("The index date needs one explicit tie-breaking rule."),
            _task_message(
                "resumed-review-call",
                description="Review revised cohort",
                prompt="Review the supplied tie-breaking rule and finish the assessment.",
                specialist_id="cohort-feature-reviewer",
                resume="task_00000001",
            ),
        ]
    )
    backend = OpenHandsSdkBackend(
        profile=_local_profile(),
        workspace=tmp_path / "workspace",
        skills_dir=tmp_path / "skills",
        persistence_dir=tmp_path / "openhands",
        conversation_key="resumed-specialist",
        specialist_catalog=_specialist_catalog(),
        env={},
        conversation_factory=_conversation_factory(
            tmp_path,
            llm,
            tools=[_specialist_tool()],
        ),
    )
    backend._register_specialized_agents()
    backend.submit_turn(session_id="session-1", prompt="Review the cohort")
    first_group = _wait_for_pending_group(backend)
    continued = backend.resolve_confirmation(
        session_id="session-1",
        action_group_id=first_group.group_id,
        approved=True,
    )
    deadline = time.monotonic() + 10
    while backend._run_active() and time.monotonic() < deadline:
        time.sleep(0.02)
    events = (
        *continued,
        *backend.reconcile(
            session_id="session-1",
            known_source_event_ids=frozenset(),
        ),
    )

    assert not backend._run_active()
    assert backend.pending_action_group(session_id="session-1") is None
    assert llm.call_count == 2
    assert any(
        isinstance(event, BackendConfirmationResolutionEvent)
        and event.tool_call.tool_call_id == "resumed-review-call"
        and not event.approved
        for event in events
    )
    assert any(
        isinstance(event, BackendSubagentEvent)
        and event.subagent.task_id == "task_00000001"
        and event.subagent.status == BackendSubagentStatus.COMPLETED
        for event in events
    )
    assert all(
        not isinstance(event, BackendSubagentEvent)
        or event.subagent.invocation_id != "resumed-review-call"
        or event.subagent.status != BackendSubagentStatus.COMPLETED
        for event in events
    )
    backend.close()


def test_specialist_failure_is_projected_to_the_parent(
    tmp_path: Path,
) -> None:
    llm = TestLLM.from_messages(
        [
            _task_message(
                "failed-review-call",
                description="Review synthetic evidence",
                prompt="Review the supplied synthetic data-quality evidence.",
                specialist_id="data-quality-reviewer",
            ),
            RuntimeError(f"synthetic specialist failure at {tmp_path / 'private.csv'}"),
            _assistant_message("The specialist could not complete the review."),
        ]
    )
    backend = OpenHandsSdkBackend(
        profile=_local_profile(),
        workspace=tmp_path / "workspace",
        skills_dir=tmp_path / "skills",
        persistence_dir=tmp_path / "openhands",
        conversation_key="failed-specialist",
        specialist_catalog=_specialist_catalog(),
        env={},
        conversation_factory=_conversation_factory(
            tmp_path,
            llm,
            tools=[_specialist_tool()],
        ),
    )
    backend._register_specialized_agents()
    backend.submit_turn(session_id="session-1", prompt="Review data quality")
    group = _wait_for_pending_group(backend)
    continued = backend.resolve_confirmation(
        session_id="session-1",
        action_group_id=group.group_id,
        approved=True,
    )
    events = (*continued, *_wait_for_lifecycle(backend, BackendLifecycle.FINISHED))

    assert any(
        isinstance(event, BackendSubagentEvent)
        and event.subagent.agent_name == "data-quality-reviewer"
        and event.subagent.status == BackendSubagentStatus.ERROR
        for event in events
    )
    assert "synthetic specialist failure" not in repr(events)
    persisted = "\n".join(
        path.read_text(encoding="utf-8") for path in (tmp_path / "openhands").rglob("*.json")
    )
    assert "synthetic specialist failure" not in persisted
    assert "private.csv" not in persisted
    backend.close()


def test_completed_specialist_workflow_replays_without_model_calls(
    tmp_path: Path,
) -> None:
    conversation_id = uuid.uuid4()
    persistence_dir = tmp_path / "openhands"
    first_llm = TestLLM.from_messages(
        [
            Message(
                role="assistant",
                content=[TextContent(text="")],
                tool_calls=[
                    MessageToolCall(
                        id="replayed-specialist-call",
                        name="task",
                        arguments=(
                            '{"description":"Review cohort timing","prompt":'
                            '"Review supplied synthetic index dates","subagent_type":'
                            '"cohort-feature-reviewer"}'
                        ),
                        origin="completion",
                    )
                ],
            ),
            _assistant_message("The supplied feature windows avoid outcome leakage."),
            _assistant_message("The cohort review is complete."),
        ]
    )
    first = OpenHandsSdkBackend(
        profile=_local_profile(),
        workspace=tmp_path / "workspace",
        skills_dir=tmp_path / "skills",
        persistence_dir=persistence_dir,
        conversation_key="specialist-replay",
        specialist_catalog=_specialist_catalog(),
        env={},
        conversation_factory=_conversation_factory(
            tmp_path,
            first_llm,
            tools=[_specialist_tool()],
            conversation_id=conversation_id,
            persistence_dir=persistence_dir,
        ),
    )
    first._register_specialized_agents()
    first.submit_turn(session_id="session-1", prompt="Review cohort timing")
    group = _wait_for_pending_group(first)
    first.resolve_confirmation(
        session_id="session-1",
        action_group_id=group.group_id,
        approved=True,
    )
    _wait_for_lifecycle(first, BackendLifecycle.FINISHED)
    first.close()

    restored_llm = TestLLM.from_messages([])
    restored = OpenHandsSdkBackend(
        profile=_local_profile(),
        workspace=tmp_path / "workspace",
        skills_dir=tmp_path / "skills",
        persistence_dir=persistence_dir,
        conversation_key="specialist-replay",
        specialist_catalog=_specialist_catalog(),
        env={},
        conversation_factory=_conversation_factory(
            tmp_path,
            restored_llm,
            tools=[_specialist_tool()],
            conversation_id=conversation_id,
            persistence_dir=persistence_dir,
        ),
    )

    replayed = restored.reconcile(
        session_id="session-1",
        known_source_event_ids=frozenset(),
    )

    assert restored_llm.call_count == 0
    expected_label = _specialist_catalog().role("cohort-feature-reviewer").label
    assert any(
        isinstance(event, BackendSubagentEvent)
        and event.subagent.agent_name == "cohort-feature-reviewer"
        and event.subagent.role_label == expected_label
        and event.subagent.status == BackendSubagentStatus.COMPLETED
        for event in replayed
    )
    assert any(
        isinstance(event, BackendAgentMessageEvent)
        and event.message == "The cohort review is complete."
        for event in replayed
    )
    restored.close()


def test_translation_reports_analyzed_risk_and_nonzero_exit() -> None:
    action = _terminal_action_event("action-1", "call-1", "false")
    tool_call = _tool_call(action, analyzed_risk="medium")
    observation = ObservationEvent(
        id="observation-1",
        tool_name="terminal",
        tool_call_id="call-1",
        action_id=action.id,
        observation=TerminalObservation(command="false", exit_code=127),
    )
    translated = _tool_observation(
        observation,
        source_event_id="openhands:observation-1",
    )

    assert tool_call.risk == "medium"
    assert _analyzed_risk(cast(SecurityAnalyzerBase, _FailingAnalyzer()), action) == "high"
    assert isinstance(translated, BackendToolExecutionEvent)
    assert translated.tool_execution.tool_call_id == "call-1"
    assert translated.tool_execution.action_id == "action-1"
    assert translated.tool_execution.exit_code == 127
    assert translated.tool_execution.summary == "terminal failed"


def test_typed_action_evidence_uses_openhands_ids_and_only_structured_file_paths(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    file_arguments = {
        "command": "create",
        "path": str(workspace / "results" / "summary.py"),
        "file_text": "answer = 42\n",
    }
    file_action = _terminal_action_event(
        "action-file",
        "call-file",
        "placeholder",
    ).model_copy(
        update={
            "tool_name": "file_editor",
            "tool_call": MessageToolCall(
                id="call-file",
                name="file_editor",
                arguments=json.dumps(file_arguments),
                origin="completion",
            ),
            "action": FileEditorAction(
                command="create",
                path=str(workspace / "results" / "summary.py"),
                file_text="answer = 42\n",
            ),
        }
    )
    view_action = file_action.model_copy(
        update={
            "id": "action-view",
            "tool_call_id": "call-view",
            "tool_call": file_action.tool_call.model_copy(update={"id": "call-view"}),
            "action": FileEditorAction(
                command="view",
                path=str(workspace / "results" / "summary.py"),
            ),
        }
    )
    escaping_action = file_action.model_copy(
        update={
            "id": "action-escape",
            "tool_call_id": "call-escape",
            "tool_call": file_action.tool_call.model_copy(update={"id": "call-escape"}),
            "action": FileEditorAction(
                command="create",
                path=str(tmp_path / "outside.py"),
                file_text="outside = True\n",
            ),
        }
    )

    file_call = _tool_call(file_action, workspace=workspace)
    view_call = _tool_call(view_action, workspace=workspace)
    escaping_call = _tool_call(escaping_action, workspace=workspace)
    terminal_call = _tool_call(
        _terminal_action_event("action-terminal", "call-terminal", "touch not-evidence.txt"),
        workspace=workspace,
    )
    task_call = _tool_call(_subagent_action_event(), workspace=workspace)

    assert file_call.action_id == "action-file"
    assert file_call.tool_call_id == "call-file"
    assert file_call.kind == "file-editor"
    assert file_call.affected_paths == ("results/summary.py",)
    assert file_call.project_path == "results/summary.py"
    assert view_call.affected_paths == ()
    assert view_call.project_path == "results/summary.py"
    assert escaping_call.affected_paths == ()
    assert escaping_call.project_path is None
    assert terminal_call.kind == "terminal"
    assert terminal_call.affected_paths == ()
    assert terminal_call.project_path is None
    assert task_call.kind == "task"
    assert task_call.summary == "Plan analysis"


@pytest.mark.parametrize(
    "private_path",
    [
        "results/.HeArTwOoD/private.txt",
        "results/.GiT/config",
    ],
)
def test_typed_action_evidence_excludes_case_variant_private_paths(
    tmp_path: Path,
    private_path: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    absolute_path = workspace / private_path
    arguments = {
        "command": "create",
        "path": str(absolute_path),
        "file_text": "private\n",
    }
    action = _terminal_action_event(
        "action-private",
        "call-private",
        "placeholder",
    ).model_copy(
        update={
            "tool_name": "file_editor",
            "tool_call": MessageToolCall(
                id="call-private",
                name="file_editor",
                arguments=json.dumps(arguments),
                origin="completion",
            ),
            "action": FileEditorAction(
                command="create",
                path=str(absolute_path),
                file_text="private\n",
            ),
        }
    )

    tool_call = _tool_call(action, workspace=workspace)

    assert tool_call.project_path is None
    assert tool_call.affected_paths == ()


def test_tool_result_is_bounded_before_private_session_persistence() -> None:
    output = "head-sentinel\n" + ("result\n" * 5_000) + "tail-sentinel\n"
    observation = ObservationEvent(
        id="observation-1",
        tool_name="terminal",
        tool_call_id="call-1",
        action_id="action-1",
        observation=TerminalObservation.from_text(
            output,
            command="python analysis.py",
            exit_code=0,
        ),
    )

    translated = _tool_observation(
        observation,
        source_event_id="openhands:observation-1",
    )

    assert isinstance(translated, BackendToolExecutionEvent)
    assert translated.tool_execution.result is not None
    assert len(translated.tool_execution.result) <= 16_000
    assert len(translated.tool_execution.result.splitlines()) <= 240
    assert translated.tool_execution.result.startswith("head-sentinel\n")
    assert translated.tool_execution.result.endswith("tail-sentinel\n")
    assert "Heartwood output truncated" in translated.tool_execution.result
    assert translated.tool_execution.result_truncated is True


def test_tool_result_preserves_bounded_text_exactly() -> None:
    observation = ObservationEvent(
        id="observation-1",
        tool_name="terminal",
        tool_call_id="call-1",
        action_id="action-1",
        observation=TerminalObservation.from_text(
            "first\r\nsecond\n",
            command="python analysis.py",
            exit_code=0,
        ),
    )

    translated = _tool_observation(
        observation,
        source_event_id="openhands:observation-1",
    )

    assert isinstance(translated, BackendToolExecutionEvent)
    assert translated.tool_execution.result == "first\r\nsecond\n"
    assert translated.tool_execution.result_truncated is False


def test_openhands_backend_preflights_credential_reference(tmp_path: Path) -> None:
    backend = OpenHandsSdkBackend(
        profile=_remote_profile(),
        workspace=tmp_path / "workspace",
        skills_dir=tmp_path / "skills",
        persistence_dir=tmp_path / "openhands",
        conversation_key="credential-test",
        env={},
        conversation_factory=_finished_conversation_factory(
            tmp_path,
            TestLLM.from_messages([]),
        ),
    )

    assert backend.configuration_error == "active model profile credential reference is unavailable"


@pytest.mark.parametrize(
    ("model", "endpoint"),
    [
        ("openai/gpt-5.4", "https://api.openai.com/v1/chat/completions"),
        ("openai/gpt-4.1", "https://api.openai.com/v1/responses"),
    ],
)
def test_openhands_backend_rejects_stale_provider_routes(
    tmp_path: Path,
    model: str,
    endpoint: str,
) -> None:
    backend = OpenHandsSdkBackend(
        profile=ModelProfile(
            profile_id="openai",
            model=model,
            policy_endpoint=endpoint,
            credential_kind="environment",
            api_key_env="OPENAI_API_KEY",
        ),
        workspace=tmp_path / "workspace",
        skills_dir=tmp_path / "skills",
        persistence_dir=tmp_path / "openhands",
        conversation_key="route-test",
        env={"OPENAI_API_KEY": "test-key"},
        conversation_factory=_finished_conversation_factory(
            tmp_path,
            TestLLM.from_messages([]),
        ),
    )

    assert backend.configuration_error == (
        "The active model profile request path does not match OpenHands"
    )


def test_openhands_finish_events_map_only_to_a_final_agent_message(
    tmp_path: Path,
) -> None:
    action, observation = _finish_event_pair()
    backend = _backend(
        tmp_path,
        _finished_conversation_factory(tmp_path, TestLLM.from_messages([])),
    )

    translated_action = backend._translate_event(action, session_id="session-1")
    translated_observation = backend._translate_event(
        observation,
        session_id="session-1",
    )

    assert translated_action == (
        BackendAgentMessageEvent(
            message="The requested file was created.",
            source_event_id=f"openhands:{action.id}:message",
        ),
    )
    assert translated_observation == ()


def test_openhands_finish_events_persist_without_tool_or_audit_activity(
    tmp_path: Path,
) -> None:
    action, observation = _finish_event_pair()
    conversation = _ControlledConversation()
    state = _BranchState(conversation.id)
    state.execution_status = ConversationExecutionStatus.FINISHED
    state.events = (action, observation)
    conversation.state = state
    backend = _backend(
        tmp_path,
        cast(
            ConversationFactory,
            lambda _event_callback, _token_callback: conversation,
        ),
    )
    service = SessionService.local_default(
        tmp_path / "sessions",
        session_id="session-1",
        backend=backend,
        env={},
        clock=lambda: "2026-07-25T00:00:00Z",
    )
    try:
        service.reconcile()
        service.handle(
            SessionCommand(
                command_id="audit",
                session_id="session-1",
                kind=CommandKind.AUDIT_EXPORT,
                actor_id="test-user",
                created_at="2026-07-25T00:00:00Z",
            )
        )

        replayed = service.replay_events()
        assert [
            event.payload["content"]
            for event in replayed
            if event.kind == EventKind.AGENT_MESSAGE_EMITTED
        ] == ["The requested file was created."]
        assert all(
            event.kind not in {EventKind.TOOL_CALL_PROPOSED, EventKind.TOOL_EXECUTION_RECORDED}
            for event in replayed
        )
        audit_events = [
            json.loads(line) for line in service.store.read_audit_export().splitlines() if line
        ]
        assert all(event.get("payload", {}).get("tool_name") != "finish" for event in audit_events)
    finally:
        service.close()


def _backend(
    tmp_path: Path,
    factory: ConversationFactory,
    *,
    mode: Literal["always-confirm", "confirm-risky"] = "always-confirm",
) -> OpenHandsSdkBackend:
    return OpenHandsSdkBackend(
        profile=_local_profile(),
        workspace=tmp_path / "workspace",
        skills_dir=tmp_path / "skills",
        persistence_dir=tmp_path / "openhands",
        conversation_key="backend-test",
        action_confirmation_mode=mode,
        env={},
        conversation_factory=factory,
    )


def _finish_event_pair() -> tuple[ActionEvent, ObservationEvent]:
    action = ActionEvent(
        id="finish-action",
        thought=[],
        action=FinishAction(message="The requested file was created."),
        tool_name=FinishTool.name,
        tool_call_id="finish-call",
        tool_call=MessageToolCall(
            id="finish-call",
            name=FinishTool.name,
            arguments='{"message":"The requested file was created."}',
            origin="completion",
        ),
        llm_response_id="finish-response",
        security_risk=SecurityRisk.LOW,
        summary="Report completion",
    )
    return (
        action,
        ObservationEvent(
            id="finish-observation",
            tool_name=FinishTool.name,
            tool_call_id="finish-call",
            observation=FinishObservation(),
            action_id=action.id,
        ),
    )


def _conversation_factory(
    tmp_path: Path,
    llm: TestLLM,
    *,
    tools: list[Tool],
    conversation_id: uuid.UUID | None = None,
    persistence_dir: Path | None = None,
    workspace: Path | None = None,
) -> ConversationFactory:
    def factory(
        callback: Callable[[OpenHandsEvent], None],
        token_callback: Callable[[LLMStreamChunk], None],
    ) -> BaseConversation:
        resolved_persistence_dir = persistence_dir or tmp_path / "openhands"
        resolved_conversation_id = conversation_id or uuid.uuid4()
        settings = OpenHandsAgentSettings(
            llm=llm,
            tools=tools,
            enable_switch_llm_tool=False,
            tool_concurrency_limit=1,
        )
        conversation = LocalConversation(
            agent=settings.create_agent(),
            workspace=workspace or tmp_path / "workspace",
            persistence_dir=resolved_persistence_dir,
            conversation_id=resolved_conversation_id,
            file_store=ContentMinimizedLocalFileStore(
                LocalConversation.get_persistence_dir(
                    resolved_persistence_dir,
                    resolved_conversation_id,
                )
            ),
            callbacks=[callback],
            token_callbacks=[token_callback],
            visualizer=None,
            delete_on_close=False,
        )
        conversation.set_confirmation_policy(AlwaysConfirm())
        return conversation

    return factory


def _finished_conversation_factory(
    tmp_path: Path,
    llm: TestLLM,
) -> ConversationFactory:
    return _conversation_factory(tmp_path, llm, tools=[])


def _openhands_gateway(
    project_root: Path,
    llm: TestLLM,
    *,
    conversation_id: uuid.UUID,
) -> SessionGateway:
    def service_factory(sessions_root: Path, session_id: str) -> SessionService:
        backend = OpenHandsSdkBackend(
            profile=_local_profile(),
            workspace=project_root,
            skills_dir=project_root / "skills",
            persistence_dir=sessions_root / session_id / "openhands",
            conversation_key=f"{project_root}#{session_id}",
            env={},
            conversation_factory=_conversation_factory(
                project_root,
                llm,
                tools=[Tool(name=TerminalTool.name)],
                conversation_id=conversation_id,
                persistence_dir=sessions_root / session_id / "openhands",
                workspace=project_root,
            ),
        )
        return SessionService.local_default(
            sessions_root,
            session_id=session_id,
            backend=backend,
            env={},
            clock=lambda: "2026-07-25T00:00:00Z",
        )

    return SessionGateway(
        project=ProjectContext(project_root),
        service_factory=service_factory,
        env={},
        backend_id="openhands-sdk",
    )


def _wait_for_gateway_pending_approval(
    gateway: SessionGateway,
) -> SessionProjection:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        projection = gateway.session_projection(session_id="session-1")
        if projection.pending_approval is not None:
            return projection
        time.sleep(0.02)
    pytest.fail("gateway did not expose the OpenHands confirmation group")


def _wait_for_gateway_lifecycle(
    gateway: SessionGateway,
    lifecycle: str,
) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        projection = gateway.session_projection(session_id="session-1")
        if projection.lifecycle.status == lifecycle:
            return
        time.sleep(0.02)
    pytest.fail(f"gateway did not reach {lifecycle}")


def _durable_projection(projection: SessionProjection) -> dict[str, JsonValue]:
    payload = projection.model_dump(mode="json", by_alias=True)
    payload.pop("streamEpoch")
    payload.pop("streamRevision")
    return cast(dict[str, JsonValue], payload)


def _wait_for_lifecycle(
    backend: OpenHandsSdkBackend,
    lifecycle: BackendLifecycle,
) -> tuple[BackendEvent, ...]:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        events = backend.reconcile(
            session_id="session-1",
            known_source_event_ids=frozenset(),
        )
        if any(
            isinstance(event, BackendLifecycleEvent) and event.lifecycle == lifecycle
            for event in events
        ):
            return events
        time.sleep(0.02)
    pytest.fail(f"OpenHands did not reach {lifecycle}")


def _wait_for_pending_group(backend: OpenHandsSdkBackend) -> PendingActionGroup:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        group = backend.pending_action_group(session_id="session-1")
        if group is not None:
            return group
        time.sleep(0.02)
    pytest.fail("OpenHands did not request confirmation")


def _assistant_message(text: str) -> Message:
    return Message(role="assistant", content=[TextContent(text=text)])


def _tool_message(*calls: tuple[str, str]) -> Message:
    return Message(
        role="assistant",
        content=[TextContent(text="")],
        tool_calls=[
            MessageToolCall(
                id=tool_call_id,
                name="terminal",
                arguments=json.dumps({"command": command}),
                origin="completion",
            )
            for tool_call_id, command in calls
        ],
    )


class _StrictToolHistoryTestLLM(TestLLM):
    """Reject tool results that do not retain their preceding tool call."""

    def completion(
        self,
        messages: list[Message],
        tools: Sequence[ToolDefinition[Action, Observation]] | None = None,
        add_security_risk_prediction: bool = False,
        on_token: TokenCallbackType | None = None,
        call_context: LLMCallContext | None = None,
        **kwargs: object,
    ) -> LLMResponse:
        seen_tool_calls: set[str] = set()
        for message in messages:
            if message.tool_calls:
                seen_tool_calls.update(call.id for call in message.tool_calls)
            if message.role == "tool":
                assert message.tool_call_id in seen_tool_calls
        return super().completion(
            messages=messages,
            tools=tools,
            add_security_risk_prediction=add_security_risk_prediction,
            on_token=on_token,
            call_context=call_context,
            **kwargs,
        )


def _terminal_action_event(
    event_id: str,
    tool_call_id: str,
    command: str,
) -> ActionEvent:
    return ActionEvent(
        id=event_id,
        thought=[],
        action=TerminalAction(command=command),
        tool_name="terminal",
        tool_call_id=tool_call_id,
        tool_call=MessageToolCall(
            id=tool_call_id,
            name="terminal",
            arguments=json.dumps({"command": command}),
            origin="completion",
        ),
        llm_response_id=f"response-{event_id}",
        security_risk=SecurityRisk.LOW,
        summary="Run a bounded command",
    )


def _subagent_action_event() -> ActionEvent:
    return ActionEvent(
        id="subagent-action",
        thought=[],
        action=TaskAction(
            description="Plan analysis",
            prompt="Plan a synthetic analysis",
            subagent_type="research-planner",
        ),
        tool_name="task",
        tool_call_id="subagent-call",
        tool_call=MessageToolCall(
            id="subagent-call",
            name="task",
            arguments=(
                '{"description":"Plan analysis","prompt":"Plan a synthetic analysis",'
                '"subagent_type":"research-planner"}'
            ),
            origin="completion",
        ),
        llm_response_id="response-subagent",
        security_risk=SecurityRisk.LOW,
        summary="Delegate analysis planning",
    )


def _task_message(
    call_id: str,
    *,
    description: str,
    prompt: str,
    specialist_id: str,
    resume: str | None = None,
) -> Message:
    arguments: dict[str, str] = {
        "description": description,
        "prompt": prompt,
        "subagent_type": specialist_id,
    }
    if resume is not None:
        arguments["resume"] = resume
    return Message(
        role="assistant",
        content=[TextContent(text="")],
        tool_calls=[
            MessageToolCall(
                id=call_id,
                name="task",
                arguments=json.dumps(arguments, sort_keys=True),
                origin="completion",
            )
        ],
    )


def _local_profile(
    *,
    max_input_tokens: int | None = None,
    max_output_tokens: int | None = None,
) -> ModelProfile:
    return ModelProfile(
        profile_id="heartwood",
        model="openai/local-model",
        base_url="http://127.0.0.1:8765/v1",
        policy_endpoint="http://127.0.0.1:8765/v1/chat/completions",
        credential_kind="none",
        max_input_tokens=max_input_tokens,
        max_output_tokens=max_output_tokens,
    )


def _remote_profile() -> ModelProfile:
    return ModelProfile(
        profile_id="remote",
        model="openai/remote",
        policy_endpoint="https://api.openai.com/v1/chat/completions",
        credential_kind="environment",
        api_key_env="OPENAI_API_KEY",
    )


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _verified_skills_root() -> Path:
    return _repository_root() / "vendor" / "heartwood-skills" / "skills" / "verified"


@cache
def _specialist_catalog() -> SpecialistCatalog:
    return load_specialist_catalog(
        _repository_root() / "agents" / "verified",
        _verified_skills_root(),
    )


def _specialist_tool() -> Tool:
    return Tool(
        name=HeartwoodSpecialistToolSet.name,
        params={
            "specialists": [
                {
                    "specialist_id": role.specialist_id,
                    "label": role.label,
                    "description": role.definition.description,
                }
                for role in _specialist_catalog().available_roles
            ]
        },
    )


class _FailingAnalyzer:
    def security_risk(self, action: ActionEvent) -> object:  # noqa: ARG002
        raise RuntimeError("synthetic analyzer failure")


class _ControlledConversation:
    def __init__(self) -> None:
        self.id = uuid.uuid4()
        self.started = Event()
        self.release = Event()
        self.stopped = Event()
        self.messages: list[tuple[str, str | None]] = []
        self.closed = False
        self.interrupted = False
        self.pause_calls = 0
        self.state = _ControlledState(self.id)

    def send_message(self, message: str, sender: str | None = None) -> None:
        self.messages.append((message, sender))

    async def arun(self) -> None:
        self.state.execution_status = ConversationExecutionStatus.RUNNING
        self.started.set()
        while not self.release.wait(timeout=0.01):
            pass
        self.state.execution_status = (
            ConversationExecutionStatus.PAUSED
            if self.interrupted
            else ConversationExecutionStatus.FINISHED
        )
        self.stopped.set()

    def interrupt(self) -> None:
        self.interrupted = True
        self.state.execution_status = ConversationExecutionStatus.PAUSED
        self.release.set()

    def pause(self) -> None:
        self.pause_calls += 1
        self.state.execution_status = ConversationExecutionStatus.PAUSED

    def reject_pending_actions(self, reason: str) -> None:  # noqa: ARG002
        self.state.execution_status = ConversationExecutionStatus.FINISHED

    def close(self) -> None:
        self.closed = True


class _DelayedInterruptConversation(_ControlledConversation):
    async def arun(self) -> None:
        self.state.execution_status = ConversationExecutionStatus.RUNNING
        self.started.set()
        while not self.release.is_set():
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        self.state.execution_status = ConversationExecutionStatus.PAUSED
        self.stopped.set()

    def interrupt(self) -> None:
        self.interrupted = True
        self.release.set()


class _ControlledState:
    def __init__(self, conversation_id: uuid.UUID) -> None:
        self.id = conversation_id
        self.execution_status = ConversationExecutionStatus.IDLE
        self.view = SimpleNamespace(events=())
        self.stats: ConversationStats | SimpleNamespace = SimpleNamespace(
            usage_to_metrics={},
            get_combined_metrics=lambda: SimpleNamespace(
                get_snapshot=lambda: SimpleNamespace(accumulated_token_usage=None)
            ),
        )

    def active_branch(self) -> Sequence[OpenHandsEvent]:
        return ()

    def rebuild_view(self) -> None:
        self.view.events = tuple(self.active_branch())


class _BranchState(_ControlledState):
    def __init__(self, conversation_id: uuid.UUID) -> None:
        super().__init__(conversation_id)
        self.events: tuple[OpenHandsEvent, ...] = ()

    def active_branch(self) -> Sequence[OpenHandsEvent]:
        return self.events


class _PendingState(_ControlledState):
    def __init__(self, conversation_id: uuid.UUID, action: ActionEvent) -> None:
        super().__init__(conversation_id)
        self.execution_status = ConversationExecutionStatus.WAITING_FOR_CONFIRMATION
        self.action = action
        self.rebuild_view()

    def active_branch(self) -> Sequence[OpenHandsEvent]:
        return (self.action,)


class _RejectFailingConversation(_ControlledConversation):
    def __init__(self, action: ActionEvent) -> None:
        super().__init__()
        self.state = _PendingState(self.id, action)

    def reject_pending_actions(self, reason: str) -> None:  # noqa: ARG002
        raise RuntimeError("private rejection failure")


class _ConfirmationBoundaryConversation(_ControlledConversation):
    def __init__(self) -> None:
        super().__init__()
        self.pending = Event()
        self.continued = Event()
        self.run_count = 0
        self.action = _terminal_action_event("boundary-action", "boundary-call", "true")

    async def arun(self) -> None:
        self.run_count += 1
        if self.run_count == 1:
            self.state = _PendingState(self.id, self.action)
            self.pending.set()
            await asyncio.sleep(0.05)
            return
        self.state = _ControlledState(self.id)
        self.state.execution_status = ConversationExecutionStatus.FINISHED
        self.continued.set()


class _ProgressConversation(_ControlledConversation):
    def __init__(self) -> None:
        super().__init__()
        self.state = _BranchState(self.id)
        self.finished = Event()

    async def arun(self) -> None:
        self.state.execution_status = ConversationExecutionStatus.RUNNING
        assert isinstance(self.state, _BranchState)
        self.state.events = (
            MessageEvent(
                id="progress-message",
                source="agent",
                llm_message=Message(
                    role="assistant",
                    content=[TextContent(text="Inspecting the synthetic inputs.")],
                ),
            ),
        )
        self.started.set()
        while not self.release.is_set():
            await asyncio.sleep(0.01)
        self.state.execution_status = ConversationExecutionStatus.FINISHED
        self.finished.set()


class _StubbornConversation(_ControlledConversation):
    def __init__(self) -> None:
        super().__init__()
        self.finished = Event()

    async def arun(self) -> None:
        self.state.execution_status = ConversationExecutionStatus.RUNNING
        self.started.set()
        while not self.release.is_set():
            try:
                await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                continue
        self.state.execution_status = ConversationExecutionStatus.FINISHED
        self.finished.set()

    def interrupt(self) -> None:
        return None


class _ViewRepairPauseConversation(_ControlledConversation):
    def __init__(self, event_callback: Callable[[OpenHandsEvent], None]) -> None:
        super().__init__()
        self.event_callback = event_callback
        self.action = _terminal_action_event("repair-action", "repair-call", "true")
        state = _BranchState(self.id)
        state.events = (self.action,)
        state.execution_status = ConversationExecutionStatus.WAITING_FOR_CONFIRMATION
        self.state = state
        self.view_repair_boundary = Event()
        self.second_run_started = Event()
        self.run_count = 0

    async def arun(self) -> None:
        self.run_count += 1
        if self.run_count > 1:
            self.second_run_started.set()
            return
        self.state.execution_status = ConversationExecutionStatus.RUNNING
        observation = ObservationEvent(
            id="repair-observation",
            tool_name="terminal",
            tool_call_id=self.action.tool_call_id,
            action_id=self.action.id,
            observation=TerminalObservation(command="true", exit_code=0),
        )
        assert isinstance(self.state, _BranchState)
        self.state.events = (self.action, observation)
        self.event_callback(observation)
        self.view_repair_boundary.set()
        while not self.release.is_set():
            await asyncio.sleep(0.01)
        self.stopped.set()

    def interrupt(self) -> None:
        self.interrupted = True
        self.state.execution_status = ConversationExecutionStatus.PAUSED
        self.release.set()


class _AutomaticRepairConversation(_ControlledConversation):
    def __init__(self, event_callback: Callable[[OpenHandsEvent], None]) -> None:
        super().__init__()
        self.event_callback = event_callback
        self.action = _terminal_action_event("automatic-action", "automatic-call", "true")
        state = _BranchState(self.id)
        state.events = (self.action,)
        state.execution_status = ConversationExecutionStatus.WAITING_FOR_CONFIRMATION
        self.state = state
        self.interrupt_called = Event()
        self.second_run_started = Event()
        self.run_count = 0

    async def arun(self) -> None:
        self.run_count += 1
        if self.run_count > 1:
            self.second_run_started.set()
            return
        self.state.execution_status = ConversationExecutionStatus.RUNNING
        observation = ObservationEvent(
            id="automatic-observation",
            tool_name="terminal",
            tool_call_id=self.action.tool_call_id,
            action_id=self.action.id,
            observation=TerminalObservation(command="true", exit_code=0),
        )
        assert isinstance(self.state, _BranchState)
        self.state.events = (self.action, observation)
        self.event_callback(observation)

    def interrupt(self) -> None:
        self.state.execution_status = ConversationExecutionStatus.PAUSED
        self.interrupt_called.set()


class _BlockingShutdownConversation(_ControlledConversation):
    def __init__(self) -> None:
        super().__init__()
        self.shutdown_started = Event()
        self.allow_shutdown = Event()

    async def arun(self) -> None:
        self.state.execution_status = ConversationExecutionStatus.RUNNING
        self.started.set()
        try:
            while not self.release.is_set():
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            pass
        self.shutdown_started.set()
        while not self.allow_shutdown.is_set():
            await asyncio.sleep(0.01)
        self.state.execution_status = ConversationExecutionStatus.PAUSED
        self.stopped.set()


class _MetadataObservation(Observation):
    metadata: dict[str, object]


class _FailingConversation(_ControlledConversation):
    async def arun(self) -> None:
        self.state.execution_status = ConversationExecutionStatus.RUNNING
        raise RuntimeError("synthetic failure")
