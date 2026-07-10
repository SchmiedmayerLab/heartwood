# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from heartwood.core_adapter import (
    BackendEvent,
    BackendEventKind,
    DeterministicAgentBackend,
    ProposedToolCall,
    SessionService,
)
from heartwood.schemas import PolicyProfile
from heartwood.session import CommandKind, EventKind, JsonValue, SessionCommand


def test_detection_persists_replayable_events(tmp_path: Path) -> None:
    service = SessionService.synthetic_default(tmp_path)

    result = service.handle(_command(CommandKind.DETECT))

    assert [event.kind for event in result.events] == [
        EventKind.COMMAND_RECEIVED.value,
        EventKind.DETECTION_PROPOSED.value,
    ]
    assert service.replay_events() == result.events


def test_task_records_route_decision_and_waits_for_action_confirmation(tmp_path: Path) -> None:
    service = SessionService.synthetic_default(tmp_path)

    result = service.handle(_command(CommandKind.CHAT, prompt="summarize the cohort"))

    assert [event.kind for event in result.events] == [
        EventKind.COMMAND_RECEIVED.value,
        EventKind.USER_MESSAGE_RECORDED.value,
        EventKind.MODEL_CALL_DECISION_RECORDED.value,
        EventKind.AGENT_MESSAGE_EMITTED.value,
        EventKind.TOOL_CALL_PROPOSED.value,
        EventKind.CONFIRMATION_REQUESTED.value,
    ]
    assert result.events[1].payload == {
        "actor_id": "human",
        "command_id": "command-chat",
        "content": "summarize the cohort",
    }
    decision = result.events[2].payload["decision"]
    assert isinstance(decision, dict)
    assert decision["decision"] == "allow"
    profile = result.events[2].payload["model_profile"]
    assert isinstance(profile, dict)
    assert profile["profile_id"] == "deterministic-local"
    assert "summarize the cohort" in json.dumps(
        [event.model_dump(mode="json") for event in service.replay_events()]
    )
    audit_text = service.store.audit_path.read_text(encoding="utf-8")
    assert "summarize the cohort" not in audit_text
    assert '"content":"[scrubbed]"' in audit_text
    assert stat.S_IMODE(service.store.session_dir.stat().st_mode) == 0o700
    for path in (
        service.store.commands_path,
        service.store.events_path,
        service.store.audit_path,
    ):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_run_is_a_compatibility_alias_for_task_submission(tmp_path: Path) -> None:
    service = SessionService.synthetic_default(tmp_path)

    result = service.handle(_command(CommandKind.RUN, prompt="run the workflow"))

    assert any(event.kind == EventKind.CONFIRMATION_REQUESTED.value for event in result.events)


def test_risk_based_mode_auto_executes_low_risk_action_and_records_mode(
    tmp_path: Path,
) -> None:
    service = SessionService.local_default(
        tmp_path,
        session_id="session-synthetic-001",
        backend=DeterministicAgentBackend(action_confirmation_mode="confirm-risky"),
        env={},
    )

    result = service.handle(_command(CommandKind.CHAT, prompt="summarize the cohort"))

    kinds = [event.kind for event in result.events]
    assert EventKind.TOOL_CALL_PROPOSED.value in kinds
    assert EventKind.TOOL_EXECUTION_RECORDED.value in kinds
    assert EventKind.CONFIRMATION_REQUESTED.value not in kinds
    profile = result.events[2].payload["model_profile"]
    assert isinstance(profile, dict)
    assert profile["action_confirmation_mode"] == "confirm-risky"


def test_approved_action_records_tool_execution(tmp_path: Path) -> None:
    service = SessionService.synthetic_default(tmp_path)
    service.handle(_command(CommandKind.CHAT, prompt="run the workflow"))

    result = service.handle(
        _command(
            CommandKind.APPROVE,
            target_type="tool-call",
            target_id="session-synthetic-001-toolcall-0",
        )
    )

    assert [event.kind for event in result.events] == [
        EventKind.COMMAND_RECEIVED.value,
        EventKind.CONFIRMATION_RESOLVED.value,
        EventKind.TOOL_EXECUTION_RECORDED.value,
    ]
    assert result.events[1].payload["decision"] == "approved"
    assert result.events[2].payload["exit_code"] == 0


def test_rejected_action_is_not_recorded_as_tool_execution(tmp_path: Path) -> None:
    service = SessionService.synthetic_default(tmp_path)
    service.handle(_command(CommandKind.CHAT, prompt="run the workflow"))

    result = service.handle(
        _command(
            CommandKind.DENY,
            target_type="tool-call",
            target_id="session-synthetic-001-toolcall-0",
        )
    )

    assert [event.kind for event in result.events] == [
        EventKind.COMMAND_RECEIVED.value,
        EventKind.CONFIRMATION_RESOLVED.value,
    ]
    assert result.events[1].payload["decision"] == "denied"


def test_interactive_approval_rejects_non_action_targets(tmp_path: Path) -> None:
    service = SessionService.synthetic_default(tmp_path)

    result = service.handle(
        _command(CommandKind.APPROVE, target_type="model-call", target_id="route")
    )

    assert result.events[-1].kind == EventKind.ERROR_RECORDED.value
    assert "only for pending tool actions" in str(result.events[-1].payload["reason"])


def test_action_decision_requires_matching_pending_action(tmp_path: Path) -> None:
    service = SessionService.synthetic_default(tmp_path)

    result = service.handle(
        _command(CommandKind.APPROVE, target_type="tool-call", target_id="missing")
    )

    assert result.events[-1].kind == EventKind.ERROR_RECORDED.value
    assert "no matching pending action" in str(result.events[-1].payload["reason"])


def test_second_task_requires_pending_action_resolution(tmp_path: Path) -> None:
    service = SessionService.synthetic_default(tmp_path)
    service.handle(_command(CommandKind.CHAT, prompt="first task"))

    result = service.handle(_command(CommandKind.CHAT, prompt="second task"))

    assert [event.kind for event in result.events] == [
        EventKind.COMMAND_RECEIVED.value,
        EventKind.USER_MESSAGE_RECORDED.value,
        EventKind.ERROR_RECORDED.value,
    ]
    assert result.events[-1].kind == EventKind.ERROR_RECORDED.value
    assert "resolve the pending action" in str(result.events[-1].payload["reason"])


def test_resume_requires_pending_action_resolution(tmp_path: Path) -> None:
    service = SessionService.synthetic_default(tmp_path)
    service.handle(_command(CommandKind.CHAT, prompt="propose an action"))

    result = service.handle(_command(CommandKind.RESUME))

    assert [event.kind for event in result.events] == [
        EventKind.COMMAND_RECEIVED.value,
        EventKind.ERROR_RECORDED.value,
    ]
    assert result.events[-1].payload["reason"] == "resolve the pending action before resuming"


def test_denied_route_never_calls_backend(tmp_path: Path) -> None:
    backend = _RecordingBackend(endpoint="https://public.example.invalid/v1/chat/completions")
    service = SessionService.local_default(
        tmp_path,
        backend=backend,
        policy_profile=PolicyProfile(
            policy_id="local-only",
            platform_id="generic",
            deny_egress_by_default=True,
            allowed_model_endpoints=("http://127.0.0.1:8765/v1/chat/completions",),
            credential_allowlist=(),
        ),
        clock=lambda: "2026-01-01T00:00:00Z",
    )

    result = service.handle(
        _command(CommandKind.CHAT, prompt="do not send").model_copy(
            update={"session_id": "session-local"}
        )
    )

    assert backend.prompts == []
    decision = result.events[2].payload["decision"]
    assert isinstance(decision, dict)
    assert decision["decision"] == "deny"
    assert result.events[-1].kind == EventKind.ERROR_RECORDED.value


def test_approved_action_rechecks_route_before_backend_continuation(tmp_path: Path) -> None:
    pending = ProposedToolCall(
        tool_call_id="session-local-action",
        tool_name="terminal",
        risk="low",
        summary="run a bounded command",
    )
    backend = _RecordingBackend(
        endpoint="http://127.0.0.1:8765/v1/chat/completions",
        response=(
            BackendEvent(kind=BackendEventKind.TOOL_CALL_PROPOSED, tool_call=pending),
            BackendEvent(kind=BackendEventKind.CONFIRMATION_REQUESTED, tool_call=pending),
        ),
    )
    service = SessionService.local_default(
        tmp_path,
        backend=backend,
        policy_profile=PolicyProfile(
            policy_id="local-only",
            platform_id="generic",
            deny_egress_by_default=True,
            allowed_model_endpoints=("http://127.0.0.1:8765/v1/chat/completions",),
            credential_allowlist=(),
        ),
        clock=lambda: "2026-01-01T00:00:00Z",
    )
    command = _command(CommandKind.CHAT, prompt="propose action").model_copy(
        update={"session_id": "session-local"}
    )
    service.handle(command)
    backend.endpoint = "https://public.example.invalid/v1/chat/completions"

    result = service.handle(
        _command(
            CommandKind.APPROVE,
            target_type="tool-call",
            target_id=pending.tool_call_id,
        ).model_copy(update={"session_id": "session-local"})
    )

    assert backend.resolutions == []
    assert [event.kind for event in result.events] == [
        EventKind.COMMAND_RECEIVED.value,
        EventKind.MODEL_CALL_DECISION_RECORDED.value,
        EventKind.ERROR_RECORDED.value,
    ]
    decision = result.events[1].payload["decision"]
    assert isinstance(decision, dict)
    assert decision["decision"] == "deny"


def test_resume_rechecks_route_before_backend_continuation(tmp_path: Path) -> None:
    backend = _RecordingBackend(endpoint="https://public.example.invalid/v1/chat/completions")
    service = SessionService.local_default(
        tmp_path,
        backend=backend,
        policy_profile=PolicyProfile(
            policy_id="local-only",
            platform_id="generic",
            deny_egress_by_default=True,
            allowed_model_endpoints=("http://127.0.0.1:8765/v1/chat/completions",),
            credential_allowlist=(),
        ),
        clock=lambda: "2026-01-01T00:00:00Z",
    )

    result = service.handle(
        _command(CommandKind.RESUME).model_copy(update={"session_id": "session-local"})
    )

    assert backend.resume_calls == 0
    assert [event.kind for event in result.events] == [
        EventKind.COMMAND_RECEIVED.value,
        EventKind.MODEL_CALL_DECISION_RECORDED.value,
        EventKind.ERROR_RECORDED.value,
    ]


def test_backend_configuration_fails_before_route_decision(tmp_path: Path) -> None:
    backend = _RecordingBackend(
        endpoint="https://model.local.invalid/v1/chat/completions",
        configuration_error="model profile is not ready",
    )
    service = SessionService.local_default(
        tmp_path,
        backend=backend,
        clock=lambda: "2026-01-01T00:00:00Z",
    )

    result = service.handle(
        _command(CommandKind.CHAT, prompt="do not send").model_copy(
            update={"session_id": "session-local"}
        )
    )

    assert backend.prompts == []
    assert [event.kind for event in result.events] == [
        EventKind.COMMAND_RECEIVED.value,
        EventKind.USER_MESSAGE_RECORDED.value,
        EventKind.ERROR_RECORDED.value,
    ]
    assert result.events[-1].payload["reason"] == "model profile is not ready"


def test_backend_error_is_translated_without_exception(tmp_path: Path) -> None:
    backend = _RecordingBackend(
        endpoint="https://model.local.invalid/v1/chat/completions",
        response=(BackendEvent(kind=BackendEventKind.ERROR, message="backend unavailable"),),
    )
    service = SessionService.local_default(
        tmp_path,
        backend=backend,
        clock=lambda: "2026-01-01T00:00:00Z",
    )

    result = service.handle(
        _command(CommandKind.CHAT, prompt="run").model_copy(update={"session_id": "session-local"})
    )

    assert result.events[-1].kind == EventKind.ERROR_RECORDED.value
    assert result.events[-1].payload["reason"] == "backend unavailable"


def test_empty_prompt_is_rejected_before_backend(tmp_path: Path) -> None:
    service = SessionService.synthetic_default(tmp_path)

    result = service.handle(_command(CommandKind.CHAT, prompt="  "))

    assert result.events[-1].kind == EventKind.ERROR_RECORDED.value
    assert result.events[-1].payload["reason"] == "prompt is required"


def test_pause_resume_and_export_are_persisted(tmp_path: Path) -> None:
    service = SessionService.synthetic_default(tmp_path)

    paused = service.handle(_command(CommandKind.PAUSE))
    resumed = service.handle(_command(CommandKind.RESUME))
    exported = service.handle(_command(CommandKind.AUDIT_EXPORT))

    assert paused.events[-1].kind == EventKind.SESSION_PAUSED.value
    assert resumed.events[-1].kind == EventKind.SESSION_RESUMED.value
    assert exported.events[-1].kind == EventKind.AUDIT_EXPORT_RECORDED.value
    path = Path(str(exported.events[-1].payload["path"]))
    assert path.is_file()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "audit.export.recorded" in path.read_text(encoding="utf-8")


def test_service_rejects_command_for_another_session(tmp_path: Path) -> None:
    service = SessionService.synthetic_default(tmp_path)

    with pytest.raises(ValueError, match="does not match"):
        service.handle(_command(CommandKind.DETECT).model_copy(update={"session_id": "other"}))


def test_local_workspace_backend_writes_only_after_allow_once(tmp_path: Path) -> None:
    service = SessionService.local_default(
        tmp_path,
        session_id="session-local",
        env={"HEARTWOOD_AGENT_BACKEND": "local-workspace"},
        clock=lambda: "2026-01-01T00:00:00Z",
    )
    command = _command(CommandKind.CHAT, prompt="write summary").model_copy(
        update={"session_id": "session-local"}
    )
    service.handle(command)
    artifact = tmp_path / "session-local" / "agent-artifacts" / "synthetic-workspace-summary.md"
    assert not artifact.exists()

    service.handle(
        _command(
            CommandKind.APPROVE,
            target_type="tool-call",
            target_id="session-local-toolcall-0",
        ).model_copy(update={"session_id": "session-local"})
    )

    assert artifact.is_file()
    assert "Persisted prompt content: none" in artifact.read_text(encoding="utf-8")


def test_local_default_rejects_unknown_backend(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsupported HEARTWOOD_AGENT_BACKEND"):
        SessionService.local_default(
            tmp_path,
            env={"HEARTWOOD_AGENT_BACKEND": "missing"},
        )


class _RecordingBackend:
    def __init__(
        self,
        *,
        endpoint: str,
        response: tuple[BackendEvent, ...] = (),
        configuration_error: str | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.response = response
        self._configuration_error = configuration_error
        self.prompts: list[str] = []
        self.resolutions: list[tuple[str, bool]] = []
        self.resume_calls = 0

    @property
    def backend_id(self) -> str:
        return "recording"

    @property
    def configuration_error(self) -> str | None:
        return self._configuration_error

    @property
    def model_endpoint(self) -> str:
        return self.endpoint

    @property
    def model_profile_id(self) -> str:
        return "recording"

    @property
    def capability_tier(self) -> str:
        return "supervised"

    @property
    def credential_reference(self) -> str | None:
        return None

    @property
    def action_confirmation_mode(self) -> str:
        return "always-confirm"

    @property
    def continuation_requires_model_authorization(self) -> bool:
        return True

    def submit_turn(
        self,
        *,
        session_id: str,  # noqa: ARG002
        prompt: str,
    ) -> tuple[BackendEvent, ...]:
        self.prompts.append(prompt)
        return self.response

    def restore_pending(self, tool_call: object | None) -> None:  # noqa: ARG002
        return None

    def resolve_confirmation(
        self,
        *,
        session_id: str,  # noqa: ARG002
        tool_call_id: str,
        approved: bool,
    ) -> tuple[BackendEvent, ...]:
        self.resolutions.append((tool_call_id, approved))
        return ()

    def pause(self) -> None:
        return None

    def resume(self, *, session_id: str) -> tuple[BackendEvent, ...]:  # noqa: ARG002
        self.resume_calls += 1
        return ()

    def close(self) -> None:
        return None


def _command(kind: CommandKind, **payload: JsonValue) -> SessionCommand:
    return SessionCommand(
        command_id=f"command-{kind.value.replace('.', '-')}",
        session_id="session-synthetic-001",
        kind=kind,
        actor_id="human",
        created_at="2026-01-01T00:00:00Z",
        payload=payload,
    )
