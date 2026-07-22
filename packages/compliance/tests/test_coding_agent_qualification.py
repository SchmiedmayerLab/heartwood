# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Tests for the portable real-model coding-agent qualification contract."""

from __future__ import annotations

import importlib.util
import json
import sys
import tomllib
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
        _event(
            5,
            "tool.execution.recorded",
            {"tool_name": "finish", "exit_code": 0},
        ),
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

    assert summary["tool_execution_count"] == 2
    assert cast(dict[str, bool], summary["checks"])["audit_export_verified"] is True


def test_coding_agent_qualification_requires_successful_completion(
    tmp_path: Path,
) -> None:
    module = _module(
        "verify_coding_agent_e2e_completion",
        _root() / "images/generic/scripts/verify_coding_agent_e2e.py",
    )
    verify = cast(Callable[..., dict[str, object]], module.verify_run)
    events, audit, artifact, replay, inference = _acceptance_files(tmp_path)
    payloads = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines()]
    finish = next(
        payload
        for payload in payloads
        if payload["kind"] == "tool.execution.recorded"
        and payload["payload"].get("tool_name") == "finish"
    )
    finish["payload"]["tool_name"] = "unknown"
    events.write_text(
        "".join(json.dumps(payload) + "\n" for payload in payloads),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="no successful completion"):
        verify(
            events_path=events,
            audit_path=audit,
            artifact_path=artifact,
            replay_path=replay,
            inference_path=inference,
        )


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

    assert summary["tool_execution_count"] == 2


def test_gpu_qualification_configuration_resolves_runtime_and_model() -> None:
    module = _module(
        "gpu_qualification_config",
        _root() / "images/gpu/qualification_config.py",
    )
    load = cast(Callable[[Path, str], dict[str, Any]], module.load_configuration)

    resolved = load(
        _root() / "images/gpu/compatibility.toml",
        "terra-2xt4-qwen3-coder-30b-awq",
    )

    assert resolved["runtime"]["cuda_version"] == "12.9"
    assert resolved["configuration"]["tool_call_parser"] == "qwen3_coder"
    assert resolved["configuration"]["agent_tool_mode"] == "openhands-native"
    assert resolved["configuration"]["context_window"] == 18_432
    assert resolved["configuration"]["gpu_count"] == 2
    assert resolved["configuration"]["tensor_parallel_size"] == 2
    assert resolved["configuration"]["enforce_eager"] is True
    assert resolved["configuration"]["model_revision"] == (
        "e69e73813144d9b715648d8384b3f2c035397411"
    )


def test_gpu_qualification_catalog_lists_all_terra_profiles() -> None:
    module = _module(
        "gpu_qualification_config_list",
        _root() / "images/gpu/qualification_config.py",
    )

    configurations = module.list_configurations(
        _root() / "images/gpu/compatibility.toml",
        platform="terra",
    )

    assert {configuration["configuration_id"] for configuration in configurations} == {
        "terra-2xt4-qwen3-coder-30b-awq",
    }


def test_gpu_qualification_catalog_rejects_malformed_entries(tmp_path: Path) -> None:
    module = _module(
        "gpu_qualification_config_invalid_list",
        _root() / "images/gpu/qualification_config.py",
    )
    matrix = tmp_path / "compatibility.toml"
    matrix.write_text('configurations = ["invalid"]\n', encoding="utf-8")

    with pytest.raises(ValueError, match="malformed configuration"):
        module.list_configurations(matrix)


def test_gpu_qualification_cli_rejects_id_with_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module(
        "gpu_qualification_config_conflicting_arguments",
        _root() / "images/gpu/qualification_config.py",
    )
    monkeypatch.setattr(sys, "argv", ["qualification_config.py", "profile", "--list"])

    with pytest.raises(SystemExit) as error:
        module.main()

    assert error.value.code == 2


def test_gpu_qualification_script_supports_native_runtime_and_external_matrix() -> None:
    script = (_root() / "images/gpu/coding_agent_e2e.sh").read_text(encoding="utf-8")

    assert "HEARTWOOD_GPU_COMPATIBILITY_MATRIX" in script
    assert '--matrix "${compatibility_matrix}"' in script
    assert 'HEARTWOOD_VLLM_ROOT="${vllm_root}"' in script
    assert 'verify_runtime.sh" "${vllm_root}"' in script
    assert 'verify_runtime.sh" /opt' not in script


def test_gpu_qualification_report_uses_external_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module(
        "verify_coding_agent_e2e_external_matrix",
        _root() / "images/generic/scripts/verify_coding_agent_e2e.py",
    )
    matrix = tmp_path / "compatibility.toml"
    matrix.write_text(
        '[[configurations]]\nconfiguration_id = "external-profile"\nplatform = "terra"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("HEARTWOOD_GPU_COMPATIBILITY_MATRIX", str(matrix))

    configuration = module._configuration(_root(), "external-profile")

    assert configuration == {
        "configuration_id": "external-profile",
        "platform": "terra",
    }


def test_gpu_compatibility_records_rejected_terra_configuration() -> None:
    with (_root() / "images/gpu/compatibility.toml").open("rb") as file:
        matrix = tomllib.load(file)

    unsupported = {
        entry["configuration_id"]: entry for entry in matrix["unsupported_configurations"]
    }
    assert set(unsupported) == {
        "terra-t4-qwen25-coder-7b-awq",
        "terra-t4-qwen25-coder-14b-awq",
        "terra-4xt4-qwen3-coder-30b-fp8",
        "terra-4xt4-qwen3-coder-30b-awq",
        "terra-4xt4-gpt-oss-20b",
        "terra-4xt4-gpt-oss-120b",
    }
    assert all(entry["platform"] == "terra" for entry in unsupported.values())
    assert all(entry["evaluated_at"] == "2026-07-21" for entry in unsupported.values())
    assert "tool-use workflow" in unsupported["terra-t4-qwen25-coder-7b-awq"]["reason"]
    assert "compute capability 8.0" in unsupported["terra-4xt4-gpt-oss-120b"]["reason"]


def test_gpu_compatibility_records_inconclusive_carina_attempts() -> None:
    with (_root() / "images/gpu/compatibility.toml").open("rb") as file:
        matrix = tomllib.load(file)

    inconclusive = {
        entry["configuration_id"]: entry for entry in matrix["inconclusive_configurations"]
    }
    assert inconclusive["carina-2xl40s-gpt-oss-120b"]["evaluated_at"] == "2026-07-22"
    assert inconclusive["carina-2xl40s-qwen3-coder-next-fp8"]["evaluated_at"] == ("2026-07-22")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("model_revision", "main", "immutable commit"),
        ("evaluated_at", "July 22, 2026", "ISO date"),
    ],
)
def test_gpu_compatibility_rejects_ambiguous_attempt_evidence(
    field: str,
    value: str,
    message: str,
) -> None:
    verifier = _module(
        f"gpu_compatibility_{field}",
        _root() / "deploy/verify_gpu_compatibility.py",
    )
    attempt = {
        "configuration_id": "carina-attempt",
        "platform": "carina",
        "gpu_model": "NVIDIA L40S",
        "gpu_count": 2,
        "model_repository": "example/model",
        "model_revision": "0" * 40,
        "vllm_version": "0.25.1+cu129",
        "evaluated_at": "2026-07-22",
        "evidence": "https://example.com/evidence",
        "reason": "The bounded qualification did not complete.",
        field: value,
    }

    with pytest.raises(verifier.CompatibilityError, match=message):
        verifier._verify_nonselectable_configurations(
            [attempt],
            label="inconclusive",
            seen_ids=set(),
        )


def test_gpu_qualification_context_can_be_bounded_by_platform_memory() -> None:
    verifier = _module(
        "gpu_compatibility_verifier",
        _root() / "deploy/verify_gpu_compatibility.py",
    )
    loader = _module(
        "gpu_qualification_config_bounded_context",
        _root() / "images/gpu/qualification_config.py",
    )
    resolved = loader.load_configuration(
        _root() / "images/gpu/compatibility.toml",
        "terra-2xt4-qwen3-coder-30b-awq",
    )
    with (_root() / "images/generic/local-runtime/snapshots.toml").open("rb") as file:
        snapshot = tomllib.load(file)["snapshots"][resolved["configuration"]["model_snapshot"]]

    verifier._verify_configuration(
        resolved["configuration"],
        snapshot,
        resolved["runtime"],
    )

    invalid = {
        **resolved["configuration"],
        "context_window": snapshot["maximum_context_window"] + 1,
    }
    with pytest.raises(verifier.CompatibilityError, match="within model capacity"):
        verifier._verify_configuration(invalid, snapshot, resolved["runtime"])

    invalid_eager = {**resolved["configuration"], "enforce_eager": "true"}
    with pytest.raises(verifier.CompatibilityError, match="must be a boolean"):
        verifier._verify_configuration(invalid_eager, snapshot, resolved["runtime"])
