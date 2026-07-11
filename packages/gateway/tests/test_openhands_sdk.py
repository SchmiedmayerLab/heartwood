# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from typing import Literal, cast

import pytest

from heartwood.core_adapter import BackendEventKind
from heartwood.gateway import ModelProfile, OpenHandsSdkBackend
from heartwood.gateway._openhands_sdk import (
    ConversationFactory,
    _agent_context,
    _analyzed_risk,
    _configure_upstream_defaults,
    _security_configuration,
    _terminal_tool_params,
    _tool_call,
    _tool_observation,
)


def test_verified_skills_load_through_openhands_native_loader() -> None:
    skill_module = import_module("openhands.sdk.skills")
    repository, knowledge, agent = skill_module.load_skills_from_dir(
        Path(__file__).resolve().parents[3] / "skills" / "verified"
    )
    names = set(repository) | set(knowledge) | set(agent)

    assert names == {"aggregate-export", "baseline-model", "omop-cohort-summary"}


def test_openhands_context_loads_only_explicitly_verified_skills() -> None:
    captured: dict[str, object] = {}

    def agent_context(**options: object) -> object:
        captured.update(options)
        return object()

    context = _agent_context(SimpleNamespace(AgentContext=agent_context), ["verified-skill"])

    assert context is not None
    assert captured["skills"] == ["verified-skill"]
    assert captured["load_user_skills"] is False
    assert captured["load_public_skills"] is False
    assert captured["load_project_skills"] is False


def test_terminal_tool_masks_all_configured_provider_environment_keys() -> None:
    environment_profile = ModelProfile(
        profile_id="hosted",
        model="openai/model",
        policy_endpoint="https://api.openai.com/v1/chat/completions",
        credential_kind="environment",
        api_key_env="OPENAI_API_KEY",
    )
    local_profile = ModelProfile(
        profile_id="local",
        model="openai/local",
        base_url="http://127.0.0.1:8765/v1",
        policy_endpoint="http://127.0.0.1:8765/v1/chat/completions",
        credential_kind="none",
    )

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

    _configure_upstream_defaults({"LOG_LEVEL": "ERROR"})

    assert os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] == "True"
    assert os.environ["LOG_LEVEL"] == "ERROR"
    assert os.environ["OPENHANDS_SUPPRESS_BANNER"] == "1"


def test_openhands_security_configuration_uses_upstream_defense_in_depth() -> None:
    security = import_module("openhands.sdk.security")
    analyzer, always = _security_configuration("always-confirm")
    risk_analyzer, risky = _security_configuration("confirm-risky")

    assert isinstance(analyzer, security.EnsembleSecurityAnalyzer)
    assert isinstance(risk_analyzer, security.EnsembleSecurityAnalyzer)
    assert analyzer.propagate_unknown is True
    assert [type(item) for item in analyzer.analyzers] == [
        security.PolicyRailSecurityAnalyzer,
        security.PatternSecurityAnalyzer,
        security.LLMSecurityAnalyzer,
    ]
    assert isinstance(always, security.AlwaysConfirm)
    assert always.should_confirm(security.SecurityRisk.LOW) is True
    assert isinstance(risky, security.ConfirmRisky)
    assert risky.threshold == security.SecurityRisk.MEDIUM
    assert risky.confirm_unknown is True
    assert risky.should_confirm(security.SecurityRisk.LOW) is False
    assert risky.should_confirm(security.SecurityRisk.MEDIUM) is True
    assert risky.should_confirm(security.SecurityRisk.HIGH) is True
    assert risky.should_confirm(security.SecurityRisk.UNKNOWN) is True


def test_openhands_backend_translates_message_action_and_confirmation(tmp_path: Path) -> None:
    conversation = _FakeConversation()
    backend = _backend(tmp_path, conversation)

    events = backend.submit_turn(session_id="session-1", prompt="create a file")

    assert [event.kind for event in events] == [
        BackendEventKind.AGENT_MESSAGE,
        BackendEventKind.TOOL_CALL_PROPOSED,
        BackendEventKind.CONFIRMATION_REQUESTED,
    ]
    assert events[0].message == "I will inspect the workspace."
    assert events[1].tool_call is not None
    assert events[1].tool_call.tool_name == "terminal"
    assert conversation.messages == [("create a file", "heartwood-user")]


def test_openhands_backend_reports_selected_confirmation_mode(tmp_path: Path) -> None:
    backend = _backend(tmp_path, _FakeConversation(), mode="confirm-risky")

    assert backend.action_confirmation_mode == "confirm-risky"
    assert backend.model_profile_id == "local"


def test_openhands_backend_preflights_credential_reference(tmp_path: Path) -> None:
    profile = ModelProfile(
        profile_id="remote",
        model="openai/model",
        policy_endpoint="https://api.openai.com/v1/chat/completions",
        credential_kind="environment",
        api_key_env="OPENAI_API_KEY",
    )
    backend = OpenHandsSdkBackend(
        profile=profile,
        workspace=tmp_path / "workspace",
        skills_dir=tmp_path / "skills",
        persistence_dir=tmp_path / "openhands",
        env={},
        conversation_factory=cast(ConversationFactory, lambda _callback: _FakeConversation()),
    )

    assert backend.configuration_error == "active model profile credential reference is unavailable"


def test_openhands_translation_reports_ensemble_risk_and_fails_closed() -> None:
    tool_call = _tool_call(
        ActionEvent(),
        session_id="session-1",
        analyzed_risk="medium",
    )

    assert tool_call.risk == "medium"
    assert _analyzed_risk(_FailingAnalyzer(), ActionEvent()) == "high"


def test_openhands_translation_marks_nonzero_terminal_exit_as_failed() -> None:
    translated = _tool_observation(
        SimpleNamespace(
            tool_name="terminal",
            observation=SimpleNamespace(exit_code=127, is_error=False),
        )
    )

    assert translated.tool_execution is not None
    assert translated.tool_execution.exit_code == 127
    assert translated.tool_execution.summary == "terminal failed"


def test_openhands_backend_allows_one_pending_action_and_continues(tmp_path: Path) -> None:
    conversation = _FakeConversation()
    backend = _backend(tmp_path, conversation)
    pending = backend.submit_turn(session_id="session-1", prompt="create a file")
    tool_call = pending[1].tool_call
    assert tool_call is not None

    events = backend.resolve_confirmation(
        session_id="session-1",
        tool_call_id=tool_call.tool_call_id,
        approved=True,
    )

    assert [event.kind for event in events] == [
        BackendEventKind.CONFIRMATION_RESOLVED,
        BackendEventKind.TOOL_EXECUTION,
        BackendEventKind.AGENT_MESSAGE,
    ]
    assert events[1].tool_execution is not None
    assert events[1].tool_execution.exit_code == 0
    assert conversation.rejection_reasons == []


def test_openhands_backend_rejects_pending_action_without_model_continuation(
    tmp_path: Path,
) -> None:
    conversation = _FakeConversation()
    backend = _backend(tmp_path, conversation)
    pending = backend.submit_turn(session_id="session-1", prompt="create a file")
    tool_call = pending[1].tool_call
    assert tool_call is not None

    events = backend.resolve_confirmation(
        session_id="session-1",
        tool_call_id=tool_call.tool_call_id,
        approved=False,
    )

    assert [event.kind for event in events] == [BackendEventKind.CONFIRMATION_RESOLVED]
    assert events[0].approved is False
    assert conversation.rejection_reasons == ["User rejected the action"]
    assert conversation.run_count == 1


def test_openhands_backend_requires_pending_resolution_before_new_task(tmp_path: Path) -> None:
    conversation = _FakeConversation()
    backend = _backend(tmp_path, conversation)
    backend.submit_turn(session_id="session-1", prompt="first")

    events = backend.submit_turn(session_id="session-1", prompt="second")

    assert events[0].kind == BackendEventKind.ERROR
    assert "resolve the pending action" in str(events[0].message)


def test_openhands_backend_returns_content_free_error_on_sdk_failure(tmp_path: Path) -> None:
    conversation = _FakeConversation(fail=True)
    backend = _backend(tmp_path, conversation)

    events = backend.submit_turn(session_id="session-1", prompt="private prompt")

    assert events[0].kind == BackendEventKind.ERROR
    assert events[0].message == "OpenHands conversation failed: RuntimeError"
    assert "private prompt" not in str(events)


def test_openhands_backend_closes_conversation(tmp_path: Path) -> None:
    conversation = _FakeConversation()
    backend = _backend(tmp_path, conversation)
    backend.submit_turn(session_id="session-1", prompt="start")

    backend.close()

    assert conversation.closed is True


def test_openhands_backend_does_not_create_conversation_until_first_agent_operation(
    tmp_path: Path,
) -> None:
    conversation = _FakeConversation()
    factory_calls = 0

    def factory(callback: Callable[[object], None]) -> _FakeConversation:
        nonlocal factory_calls
        factory_calls += 1
        conversation.install_callback(callback)
        return conversation

    backend = OpenHandsSdkBackend(
        profile=ModelProfile(
            profile_id="local",
            model="openai/local-model",
            base_url="http://127.0.0.1:8765/v1",
            policy_endpoint="http://127.0.0.1:8765/v1/chat/completions",
            credential_kind="none",
        ),
        workspace=tmp_path / "workspace",
        skills_dir=tmp_path / "skills",
        persistence_dir=tmp_path / "openhands",
        env={},
        conversation_factory=cast(ConversationFactory, factory),
    )

    backend.pause()
    assert factory_calls == 0
    backend.submit_turn(session_id="session-1", prompt="start")
    assert factory_calls == 1


def test_openhands_backend_reports_unknown_confirmation_and_restores_pending(
    tmp_path: Path,
) -> None:
    conversation = _FakeConversation()
    backend = _backend(tmp_path, conversation)

    missing = backend.resolve_confirmation(
        session_id="session-1",
        tool_call_id="missing",
        approved=True,
    )
    backend.restore_pending(None)

    assert missing[0].kind == BackendEventKind.ERROR
    assert "no matching pending action" in str(missing[0].message)


def test_openhands_backend_rejects_parallel_pending_actions(tmp_path: Path) -> None:
    conversation = _ParallelConversation()
    backend = _backend(tmp_path, conversation)
    events = backend.submit_turn(session_id="session-1", prompt="parallel")
    pending_ids = [
        event.tool_call.tool_call_id
        for event in events
        if event.kind == BackendEventKind.CONFIRMATION_REQUESTED and event.tool_call is not None
    ]

    resolved = backend.resolve_confirmation(
        session_id="session-1",
        tool_call_id=pending_ids[0],
        approved=True,
    )

    assert len(pending_ids) == 2
    assert resolved[0].kind == BackendEventKind.ERROR
    assert "parallel pending actions" in str(resolved[0].message)
    assert conversation.rejection_reasons == [
        "Heartwood requires one-at-a-time action confirmation"
    ]


def test_openhands_backend_pause_and_resume_translate_error_observation(
    tmp_path: Path,
) -> None:
    conversation = _ErrorConversation()
    backend = _backend(tmp_path, conversation)

    backend.pause()
    events = backend.resume(session_id="session-1")

    assert conversation.state.execution_status.value == "finished"
    assert [event.kind for event in events] == [
        BackendEventKind.TOOL_EXECUTION,
        BackendEventKind.ERROR,
    ]
    assert events[0].tool_execution is not None
    assert events[0].tool_execution.exit_code == 1
    assert events[1].message == "synthetic conversation error"


class MessageEvent:
    def __init__(self, text: str) -> None:
        self.source = "agent"
        self.llm_message = SimpleNamespace(content=[SimpleNamespace(text=text)])


class ActionEvent:
    def __init__(self, tool_call_id: str = "call-1") -> None:
        self.tool_call_id = tool_call_id
        self.tool_name = "terminal"
        self.security_risk = SimpleNamespace(value="LOW")
        self.summary = "inspect the workspace"


class ObservationEvent:
    def __init__(self) -> None:
        self.tool_call_id = "call-1"
        self.tool_name = "terminal"
        self.observation = SimpleNamespace(exit_code=0, is_error=False)


class UserRejectObservation:
    def __init__(self) -> None:
        self.tool_call_id = "call-1"
        self.tool_name = "terminal"


class AgentErrorEvent:
    def __init__(self) -> None:
        self.detail = "synthetic conversation error"


class _FailingAnalyzer:
    def security_risk(self, action: object) -> object:  # noqa: ARG002
        raise RuntimeError("synthetic analyzer failure")


@dataclass
class _Status:
    value: str = "idle"


class _FakeConversation:
    def __init__(self, *, fail: bool = False) -> None:
        self.state = SimpleNamespace(execution_status=_Status())
        self.callback: Callable[[object], None] | None = None
        self.messages: list[tuple[str, str | None]] = []
        self.rejection_reasons: list[str] = []
        self.run_count = 0
        self.fail = fail
        self.closed = False
        self.rejected = False

    def install_callback(self, callback: Callable[[object], None]) -> None:
        self.callback = callback

    def send_message(self, message: str, sender: str | None = None) -> None:
        self.messages.append((message, sender))

    def run(self) -> None:
        if self.fail:
            raise RuntimeError("private provider detail")
        callback = self.callback
        assert callback is not None
        self.run_count += 1
        if self.run_count == 1:
            callback(MessageEvent("I will inspect the workspace."))
            callback(ActionEvent())
            self.state.execution_status.value = "waiting_for_confirmation"
        elif self.rejected:
            callback(MessageEvent("The action was not executed."))
            self.state.execution_status.value = "finished"
        else:
            callback(ObservationEvent())
            callback(MessageEvent("The workspace inspection completed."))
            self.state.execution_status.value = "finished"

    def reject_pending_actions(self, reason: str = "User rejected the action") -> None:
        self.rejection_reasons.append(reason)
        self.rejected = True
        if self.callback is not None:
            self.callback(UserRejectObservation())

    def pause(self) -> None:
        self.state.execution_status.value = "paused"

    def close(self) -> None:
        self.closed = True


class _ParallelConversation(_FakeConversation):
    def run(self) -> None:
        callback = self.callback
        assert callback is not None
        callback(ActionEvent("call-1"))
        callback(ActionEvent("call-2"))
        self.state.execution_status.value = "waiting_for_confirmation"


class _ErrorConversation(_FakeConversation):
    def run(self) -> None:
        callback = self.callback
        assert callback is not None
        observation = ObservationEvent()
        observation.observation = SimpleNamespace(is_error=True)
        callback(observation)
        callback(AgentErrorEvent())
        self.state.execution_status.value = "finished"


def _backend(
    tmp_path: Path,
    conversation: _FakeConversation,
    *,
    mode: Literal["always-confirm", "confirm-risky"] = "always-confirm",
) -> OpenHandsSdkBackend:
    def factory(callback: Callable[[object], None]) -> _FakeConversation:
        conversation.install_callback(callback)
        return conversation

    return OpenHandsSdkBackend(
        profile=ModelProfile(
            profile_id="local",
            model="openai/local-model",
            base_url="http://127.0.0.1:8765/v1",
            policy_endpoint="http://127.0.0.1:8765/v1/chat/completions",
            credential_kind="none",
        ),
        workspace=tmp_path / "workspace",
        skills_dir=tmp_path / "skills",
        persistence_dir=tmp_path / "openhands",
        action_confirmation_mode=mode,
        env={},
        conversation_factory=cast(ConversationFactory, factory),
    )
