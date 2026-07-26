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
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Literal, cast

import pytest
from litellm.types.utils import Delta, StreamingChoices
from openhands.sdk import Conversation, LLMStreamChunk, Tool
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
)
from openhands.sdk.event import (
    Event as OpenHandsEvent,
)
from openhands.sdk.event.conversation_error import ConversationErrorEvent
from openhands.sdk.llm import Message, MessageToolCall, Metrics, TextContent
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
    get_registered_agent_definitions,
    load_agents_from_dir,
    register_agent_if_absent,
)
from openhands.sdk.testing import TestLLM
from openhands.sdk.tool.schema import Observation
from openhands.tools import TaskToolSet, TaskTrackerTool, TerminalTool
from openhands.tools.task import TaskAction, TaskObservation
from openhands.tools.task_tracker import TaskTrackerObservation
from openhands.tools.task_tracker.definition import TaskItem
from openhands.tools.terminal import TerminalAction, TerminalObservation

from heartwood.core_adapter import (
    BackendAgentMessageEvent,
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
    PendingActionGroup,
)
from heartwood.gateway import ModelProfile, OpenHandsSdkBackend
from heartwood.gateway._openhands_sdk import (
    ConversationFactory,
    OpenHandsSdkError,
    _agent_context,
    _agent_settings,
    _analyzed_risk,
    _configure_upstream_defaults,
    _context_condenser_settings,
    _conversation_runtime_options,
    _llm_max_message_chars,
    _llm_options,
    _llm_resilience_options,
    _observation_exit_code,
    _security_configuration,
    _terminal_tool_params,
    _tool_arguments,
    _tool_call,
    _tool_observation,
    _usage,
    _usage_source_event_id,
)


@pytest.fixture(autouse=True)
def _disable_profile_store_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_base, "has_vision_profile_available", lambda: False)


def test_verified_skills_load_through_openhands_native_loader() -> None:
    repository, knowledge, agent = load_skills_from_dir(_repository_root() / "skills" / "verified")

    assert set(repository) | set(knowledge) | set(agent) == {
        "aggregate-export",
        "baseline-model",
        "omop-cohort-summary",
    }


def test_research_planner_is_a_tool_free_specialized_agent(tmp_path: Path) -> None:
    definitions = load_agents_from_dir(_repository_root() / "agents" / "verified")

    assert len(definitions) == 1
    definition = definitions[0]
    assert definition.name == "research-planner"
    assert definition.tools == []
    assert definition.model == "inherit"
    assert definition.max_iteration_per_run == 12
    assert "Do not claim to inspect files" in definition.system_prompt

    backend = OpenHandsSdkBackend(
        profile=_local_profile(),
        workspace=tmp_path / "workspace",
        skills_dir=tmp_path / "skills",
        persistence_dir=tmp_path / "openhands",
        conversation_key="specialist-registration",
        agents_dir=_repository_root() / "agents" / "verified",
        env={},
        conversation_factory=_finished_conversation_factory(
            tmp_path,
            TestLLM.from_messages([]),
        ),
    )
    backend._register_specialized_agents()
    assert "research-planner" in {item.name for item in get_registered_agent_definitions()}


def test_tool_enabled_specialized_agents_fail_closed(tmp_path: Path) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "unsafe-specialist.md").write_text(
        "\n".join(
            (
                "---",
                "name: unsafe-specialist",
                "description: Attempts unaudited child actions.",
                "tools:",
                "  - terminal",
                "---",
                "",
                "Run a terminal action.",
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
        conversation_key="unsafe-specialist-registration",
        agents_dir=agents_dir,
        env={},
        conversation_factory=_finished_conversation_factory(
            tmp_path,
            TestLLM.from_messages([]),
        ),
    )

    with pytest.raises(OpenHandsSdkError, match="must be tool-free"):
        backend._register_specialized_agents()


def test_openhands_context_loads_only_explicitly_verified_skills() -> None:
    context = _agent_context([])

    assert context.skills == []
    assert context.load_user_skills is False
    assert context.load_public_skills is False
    assert context.load_project_skills is False
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

    assert settings.enable_sub_agents is True
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
    failed_errors = [
        event for event in failed_submissions if isinstance(event, BackendErrorEvent)
    ]
    assert len(failed_errors) == len(failed_submissions)
    assert [event.error_code for event in failed_errors] == [
        BackendErrorCode.WORKER_STOPPED,
        BackendErrorCode.WORKER_STOPPED,
    ]
    assert all(event.source_event_id is None for event in failed_submissions)
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
        ),
        *backend._translate_event(
            ConversationErrorEvent(
                id="conversation-error-1",
                source="agent",
                code="provider_failure",
                detail="private endpoint detail",
            ),
            session_id="session-1",
        ),
        *backend._translate_event(PauseEvent(id="pause-1"), session_id="session-1"),
    )

    assert isinstance(translated[0], BackendAgentMessageEvent)
    assert translated[0].message == "Analysis complete."
    assert isinstance(translated[1], BackendToolCallEvent)
    assert translated[1].tool_call.arguments == {"command": "pwd"}
    assert isinstance(translated[2], BackendToolExecutionEvent)
    assert translated[2].tool_execution.exit_code == 0
    task_event = next(
        event for event in translated if isinstance(event, BackendTaskPlanEvent)
    )
    assert [task.status for task in task_event.tasks] == [
        BackendTaskStatus.DONE,
        BackendTaskStatus.IN_PROGRESS,
    ]
    subagent_events = [
        event.subagent
        for event in translated
        if isinstance(event, BackendSubagentEvent)
    ]
    assert [item.status for item in subagent_events] == [
        BackendSubagentStatus.PROPOSED,
        BackendSubagentStatus.COMPLETED,
    ]
    errors = [event for event in translated if isinstance(event, BackendErrorEvent)]
    assert [event.error_code for event in errors] == ["HW-AGENT-002", "HW-AGENT-003"]
    assert "provider-specific detail" not in repr(errors)
    assert "private endpoint detail" not in repr(errors)
    assert isinstance(translated[-1], BackendLifecycleEvent)
    assert translated[-1].lifecycle == BackendLifecycle.PAUSED


def test_openhands_adapter_uses_typed_public_state_only() -> None:
    source = inspect.getsource(OpenHandsSdkBackend)

    assert "type(event).__name__" not in source
    assert "getattr(" not in source
    assert "restore_pending" not in source
    assert "self._pending" not in source
    assert "self._captured" not in source


def test_real_sdk_test_llm_turn_runs_in_background_and_reconciles_once(
    tmp_path: Path,
) -> None:
    llm = TestLLM.from_messages([_assistant_message("Analysis complete.")])
    backend = _backend(tmp_path, _conversation_factory(tmp_path, llm, tools=[]))

    immediate = backend.submit_turn(session_id="session-1", prompt="Analyze synthetic data")

    assert isinstance(immediate[0], BackendLifecycleEvent)
    assert immediate[0].lifecycle == BackendLifecycle.RUNNING
    events = _wait_for_lifecycle(backend, BackendLifecycle.FINISHED)
    messages = [
        event.message for event in events if isinstance(event, BackendAgentMessageEvent)
    ]
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
    task_plan = next(
        event for event in events if isinstance(event, BackendTaskPlanEvent)
    )

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

    restored_llm = TestLLM.from_messages([_assistant_message("The file was created once.")])
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

    assert [event.kind for event in resolved] == [BackendEventKind.LIFECYCLE]
    assert isinstance(resolved[0], BackendLifecycleEvent)
    assert resolved[0].lifecycle == BackendLifecycle.RUNNING
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

    assert all(event.kind != BackendEventKind.CONFIRMATION_RESOLVED for event in resolved)
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

    started_at = time.monotonic()
    backend.submit_turn(session_id="session-1", prompt="Start")
    assert time.monotonic() - started_at < 0.25
    assert conversation.started.wait(timeout=2)
    run_thread = backend._run_thread
    backend._start_run(session_id="session-1")
    assert backend._run_thread is run_thread

    assert backend.submit_turn(session_id="session-1", prompt="Steer") == ()
    backend.pause()
    assert conversation.stopped.wait(timeout=2)
    paused = backend.reconcile(
        session_id="session-1",
        known_source_event_ids=frozenset(),
    )

    assert conversation.messages == [("Start", "heartwood-user"), ("Steer", "heartwood-user")]
    assert any(
        isinstance(event, BackendLifecycleEvent)
        and event.lifecycle == BackendLifecycle.PAUSED
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

    backend.pause()
    resumed = backend.resume(session_id="session-1")

    assert isinstance(resumed[0], BackendLifecycleEvent)
    assert resumed[0].lifecycle == BackendLifecycle.RUNNING
    backend.close()


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
    backend.submit_turn(session_id="session-1", prompt="Start")
    assert conversation.started.wait(timeout=2)

    deadline = time.monotonic() + 2
    while not any(event.kind == BackendEventKind.AGENT_MESSAGE for event in emitted):
        if time.monotonic() >= deadline:
            pytest.fail("persisted OpenHands progress was not published")
        time.sleep(0.02)

    assert not conversation.finished.is_set()
    conversation.release.set()
    assert conversation.finished.wait(timeout=2)
    backend.close()


def test_stale_running_state_becomes_paused_and_can_be_resumed_explicitly(
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
    assert events[0].lifecycle == BackendLifecycle.PAUSED

    resumed = backend.resume(session_id="session-1")
    assert isinstance(resumed[0], BackendLifecycleEvent)
    assert resumed[0].lifecycle == BackendLifecycle.RUNNING
    assert conversation.started.wait(timeout=2)
    assert conversation.stopped.wait(timeout=2)

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
        isinstance(event, BackendLifecycleEvent)
        and event.lifecycle == BackendLifecycle.ERROR
        for event in events
    )
    assert any(
        isinstance(event, BackendErrorEvent)
        and event.error_code == BackendErrorCode.ACTION_OUTCOME_UNKNOWN
        for event in events
    )
    assert backend.pending_action_group(session_id="session-1") is not None


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


def test_sequential_specialized_agent_workflow_exposes_parent_lineage(
    tmp_path: Path,
) -> None:
    specialist_name = "heartwood-test-research-planner"
    child_llm = TestLLM.from_messages([_assistant_message("Validated analysis plan.")])
    register_agent_if_absent(
        name=specialist_name,
        factory_func=lambda _parent_llm: OpenHandsAgentSettings(
            llm=child_llm,
            tools=[],
            enable_switch_llm_tool=False,
        ).create_agent(),
        description="Tool-free research planning specialist used for conformance.",
    )
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
            _assistant_message("The sequential plan is ready."),
        ]
    )
    backend = _backend(
        tmp_path,
        _conversation_factory(
            tmp_path,
            parent_llm,
            tools=[Tool(name=TaskToolSet.name)],
        ),
    )
    backend.submit_turn(session_id="session-1", prompt="Develop an analysis plan")
    group = _wait_for_pending_group(backend)
    continued = backend.resolve_confirmation(
        session_id="session-1",
        action_group_id=group.group_id,
        approved=True,
    )
    events = (*continued, *_wait_for_lifecycle(backend, BackendLifecycle.FINISHED))
    subagents = [
        event.subagent
        for event in events
        if isinstance(event, BackendSubagentEvent)
    ]

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
    assert translated.tool_execution.exit_code == 127
    assert translated.tool_execution.summary == "terminal failed"


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


def _conversation_factory(
    tmp_path: Path,
    llm: TestLLM,
    *,
    tools: list[Tool],
    conversation_id: uuid.UUID | None = None,
    persistence_dir: Path | None = None,
) -> ConversationFactory:
    def factory(
        callback: Callable[[OpenHandsEvent], None],
        token_callback: Callable[[LLMStreamChunk], None],
    ) -> BaseConversation:
        settings = OpenHandsAgentSettings(
            llm=llm,
            tools=tools,
            enable_switch_llm_tool=False,
            tool_concurrency_limit=1,
        )
        conversation = Conversation(
            agent=settings.create_agent(),
            workspace=tmp_path / "workspace",
            persistence_dir=persistence_dir or tmp_path / "openhands",
            conversation_id=conversation_id or uuid.uuid4(),
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
        self.stats = SimpleNamespace(
            usage_to_metrics={},
            get_combined_metrics=lambda: SimpleNamespace(
                get_snapshot=lambda: SimpleNamespace(accumulated_token_usage=None)
            ),
        )

    def active_branch(self) -> Sequence[OpenHandsEvent]:
        return ()


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
            await asyncio.sleep(0.01)
        self.state.execution_status = ConversationExecutionStatus.FINISHED
        self.finished.set()

    def interrupt(self) -> None:
        return None


class _MetadataObservation(Observation):
    metadata: dict[str, object]


class _FailingConversation(_ControlledConversation):
    async def arun(self) -> None:
        self.state.execution_status = ConversationExecutionStatus.RUNNING
        raise RuntimeError("synthetic failure")
