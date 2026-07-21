# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Tests for the portable real-model coding-agent qualification contract."""

from __future__ import annotations

import importlib.util
import json
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest

from heartwood.audit import AuditLog
from heartwood.session import SessionEvent


def _root() -> Path:
    return Path(__file__).resolve().parents[3]


def _module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _event(sequence: int, kind: str, payload: dict[str, Any]) -> SessionEvent:
    return SessionEvent(
        event_id=f"qualification-event-{sequence:06d}",
        session_id="qualification",
        sequence=sequence,
        kind=cast(Any, kind),
        occurred_at="2026-07-20T00:00:00Z",
        payload=payload,
    )


def _acceptance_files(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path]:
    events = (
        _event(
            0,
            "model_call.decision.recorded",
            {
                "decision": {"decision": "allow"},
                "model_profile": {"action_confirmation_mode": "always-confirm"},
            },
        ),
        _event(
            1,
            "tool_call.proposed",
            {"tool_call_id": "tool-1", "tool_name": "terminal"},
        ),
        _event(
            2,
            "confirmation.requested",
            {"request": {"tool_call_id": "tool-1"}},
        ),
        _event(
            3,
            "confirmation.resolved",
            {"tool_call_id": "tool-1", "decision": "approved"},
        ),
        _event(
            4,
            "tool.execution.recorded",
            {"tool_name": "terminal", "exit_code": 0},
        ),
        _event(5, "agent_message.emitted", {"content": "Complete"}),
        _event(6, "audit.export.recorded", {"scrubbed": True}),
    )
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        "".join(event.model_dump_json() + "\n" for event in events),
        encoding="utf-8",
    )
    audit_path = tmp_path / "audit-export.jsonl"
    audit = AuditLog(audit_path)
    for event in events:
        audit.append(
            session_id=event.session_id,
            event_type=str(event.kind),
            occurred_at=event.occurred_at,
            payload={"safe": True},
        )
    artifact_path = tmp_path / "cohort-summary.json"
    artifact_path.write_text(
        json.dumps(
            {
                "summary": {
                    "source_participant_count": 24,
                    "participant_count": 20,
                    "source_condition_occurrence_count": 39,
                    "condition_occurrence_count": 35,
                },
                "quality_checks": {"aggregate_only_output": True},
                "export_guard": {"exportable": True},
            }
        ),
        encoding="utf-8",
    )
    replay_path = tmp_path / "replay.txt"
    replay_path.write_text(
        "Action set approved (1 action)\nTool terminal exit=0\n",
        encoding="utf-8",
    )
    inference_path = tmp_path / "inference.json"
    inference_path.write_text('{"content_nonempty": true}\n', encoding="utf-8")
    return events_path, audit_path, artifact_path, replay_path, inference_path


def test_coding_agent_qualification_verifies_complete_acceptance_evidence(
    tmp_path: Path,
) -> None:
    module = _module(
        "verify_coding_agent_e2e",
        _root() / "images/generic/scripts/verify_coding_agent_e2e.py",
    )
    verify = cast(Callable[..., dict[str, object]], module.verify_run)
    events, audit, artifact, replay, inference = _acceptance_files(tmp_path)

    summary = verify(
        events_path=events,
        audit_path=audit,
        artifact_path=artifact,
        replay_path=replay,
        inference_path=inference,
    )

    assert summary["tool_execution_count"] == 1
    assert cast(dict[str, bool], summary["checks"])["audit_export_verified"] is True


def test_coding_agent_qualification_rejects_incomplete_replay(tmp_path: Path) -> None:
    module = _module(
        "verify_coding_agent_e2e_incomplete",
        _root() / "images/generic/scripts/verify_coding_agent_e2e.py",
    )
    verify = cast(Callable[..., dict[str, object]], module.verify_run)
    events, audit, artifact, replay, inference = _acceptance_files(tmp_path)
    replay.write_text("Action set approved (1 action)\n", encoding="utf-8")

    with pytest.raises(ValueError, match="fresh-process replay"):
        verify(
            events_path=events,
            audit_path=audit,
            artifact_path=artifact,
            replay_path=replay,
            inference_path=inference,
        )


def test_coding_agent_qualification_requires_explicit_tool_exit_code(
    tmp_path: Path,
) -> None:
    module = _module(
        "verify_coding_agent_e2e_exit_code",
        _root() / "images/generic/scripts/verify_coding_agent_e2e.py",
    )
    verify = cast(Callable[..., dict[str, object]], module.verify_run)
    events, audit, artifact, replay, inference = _acceptance_files(tmp_path)
    payloads = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines()]
    execution = next(
        payload for payload in payloads if payload["kind"] == "tool.execution.recorded"
    )
    execution["payload"].pop("exit_code")
    events.write_text(
        "".join(json.dumps(payload) + "\n" for payload in payloads),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="no valid exit code"):
        verify(
            events_path=events,
            audit_path=audit,
            artifact_path=artifact,
            replay_path=replay,
            inference_path=inference,
        )


def test_coding_agent_qualification_accepts_relative_artifact_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module(
        "verify_coding_agent_e2e_relative_artifact",
        _root() / "images/generic/scripts/verify_coding_agent_e2e.py",
    )
    verify = cast(Callable[..., dict[str, object]], module.verify_run)
    events, audit, artifact, replay, inference = _acceptance_files(tmp_path)
    monkeypatch.chdir(tmp_path)

    summary = verify(
        events_path=events,
        audit_path=audit,
        artifact_path=Path(artifact.name),
        replay_path=replay,
        inference_path=inference,
    )

    assert summary["tool_execution_count"] == 1


def test_gpu_qualification_configuration_resolves_runtime_and_model() -> None:
    module = _module(
        "gpu_qualification_config",
        _root() / "images/gpu/qualification_config.py",
    )
    load = cast(Callable[[Path, str], dict[str, Any]], module.load_configuration)

    resolved = load(
        _root() / "images/gpu/compatibility.toml",
        "terra-t4-qwen25-coder-7b-awq",
    )

    assert resolved["runtime"]["cuda_version"] == "12.9"
    assert resolved["configuration"]["tool_call_parser"] == "hermes"
    assert resolved["configuration"]["context_window"] == 18_432
    assert resolved["configuration"]["enforce_eager"] is True
    assert resolved["configuration"]["model_revision"] == (
        "8e8ed243bbe6f9a5aff549a0924562fc719b2b8a"
    )
