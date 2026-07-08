# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Tests for the core session service."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from heartwood.core_adapter import FileSessionStore, SessionService, SessionStoreBoundaryError
from heartwood.session import CommandKind, EventKind, JsonValue, SessionCommand


def _command(kind: CommandKind, **payload: JsonValue) -> SessionCommand:
    return SessionCommand(
        command_id=f"command-{kind.value}",
        session_id="session-synthetic-001",
        kind=kind,
        actor_id="synthetic-user",
        created_at="2026-01-01T00:00:00Z",
        payload=payload,
    )


def test_session_detect_emits_platform_and_dataset_proposals(tmp_path: Path) -> None:
    service = SessionService.synthetic_default(tmp_path)
    result = service.handle(_command(CommandKind.DETECT))

    assert [event.kind for event in result.events] == [
        EventKind.COMMAND_RECEIVED.value,
        EventKind.DETECTION_PROPOSED.value,
    ]
    detection = result.events[1].payload
    platform = detection["platform"]
    dataset = detection["dataset"]
    assert isinstance(platform, dict)
    assert isinstance(dataset, dict)
    assert platform["adapter_id"] == "generic"
    assert dataset["dataset_type"] == "omop-cdm"
    assert "rows" not in json.dumps(detection)


def test_session_records_approval_and_resume_events(tmp_path: Path) -> None:
    service = SessionService.synthetic_default(tmp_path)
    service.handle(_command(CommandKind.DETECT))
    service.handle(
        _command(
            CommandKind.APPROVE,
            approval_id="approval-1",
            target_type="skill",
            target_id="heartwood.synthetic.omop-summary",
        )
    )

    resumed = SessionService.synthetic_default(tmp_path)
    events = resumed.replay_events()
    assert [event.sequence for event in events] == [0, 1, 2, 3]
    assert events[-1].kind == EventKind.APPROVAL_RECORDED.value


def test_session_store_next_sequence_is_cached_after_resume(tmp_path: Path) -> None:
    service = SessionService.synthetic_default(tmp_path)
    service.handle(_command(CommandKind.DETECT))

    store = FileSessionStore(tmp_path, "session-synthetic-001")
    assert store.next_sequence() == 2
    assert store.next_sequence() == 3


def test_session_run_records_policy_decision_without_prompt_content(tmp_path: Path) -> None:
    service = SessionService.synthetic_default(tmp_path)
    service.handle(
        _command(
            CommandKind.APPROVE,
            target_type="model-call",
            target_id="decision-synthetic-model-call",
        )
    )
    result = service.handle(
        _command(
            CommandKind.RUN,
            prompt="show me the first records",
            endpoint="https://model.local.invalid/v1/chat",
        )
    )

    assert result.events[1].kind == EventKind.MODEL_CALL_DECISION_RECORDED.value
    decision = result.events[1].payload["decision"]
    assert isinstance(decision, dict)
    assert decision["decision"] == "allow"
    assert result.events[2].kind == EventKind.TOOL_EXECUTION_RECORDED.value
    assert "show me" not in json.dumps([event.model_dump(mode="json") for event in result.events])
    commands = (tmp_path / "session-synthetic-001" / "commands.jsonl").read_text(encoding="utf-8")
    assert "show me" not in commands


def test_session_denied_model_endpoint_still_records_noop_tool_failure(tmp_path: Path) -> None:
    service = SessionService.synthetic_default(tmp_path)
    result = service.handle(
        _command(
            CommandKind.RUN,
            prompt="run",
            endpoint="https://public.example.invalid/v1/chat",
        )
    )

    decision = result.events[1].payload["decision"]
    assert isinstance(decision, dict)
    assert decision["decision"] == "deny"
    assert result.events[2].payload["exit_code"] == 1


def test_session_run_requires_prior_model_call_approval(tmp_path: Path) -> None:
    service = SessionService.synthetic_default(tmp_path)
    result = service.handle(
        _command(
            CommandKind.RUN,
            prompt="run",
            approved=True,
            endpoint="https://model.local.invalid/v1/chat",
        )
    )

    decision = result.events[1].payload["decision"]
    assert isinstance(decision, dict)
    assert decision["decision"] == "allow"
    assert result.events[2].payload["exit_code"] == 1


def test_session_audit_export_writes_scrubbed_jsonl(tmp_path: Path) -> None:
    service = SessionService.synthetic_default(tmp_path)
    service.handle(_command(CommandKind.RUN, prompt="sensitive prompt", approved=True))
    result = service.handle(_command(CommandKind.AUDIT_EXPORT))

    path = Path(str(result.events[-1].payload["path"]))
    exported = path.read_text(encoding="utf-8")
    assert "sensitive prompt" not in exported
    assert "audit.export.recorded" in exported


def test_unimplemented_commands_emit_structured_error(tmp_path: Path) -> None:
    service = SessionService.synthetic_default(tmp_path)
    result = service.handle(_command(CommandKind.CHAT))
    assert result.events[-1].kind == EventKind.ERROR_RECORDED.value


def test_session_store_rejects_session_path_escape(tmp_path: Path) -> None:
    with pytest.raises(SessionStoreBoundaryError):
        FileSessionStore(tmp_path, "../outside")


def test_session_rejects_mismatched_command_session(tmp_path: Path) -> None:
    service = SessionService.synthetic_default(tmp_path)
    command = _command(CommandKind.DETECT).model_copy(update={"session_id": "other-session"})

    with pytest.raises(ValueError, match="does not match store session"):
        service.handle(command)


def test_local_default_uses_non_synthetic_clock(tmp_path: Path) -> None:
    command = _command(CommandKind.DETECT).model_copy(update={"session_id": "session-local"})
    result = SessionService.local_default(tmp_path, env={}).handle(command)

    assert result.events[0].occurred_at != "2026-01-01T00:00:00Z"
