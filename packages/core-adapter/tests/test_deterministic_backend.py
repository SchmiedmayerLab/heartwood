# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Focused persistence tests for the deterministic backend."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from heartwood.core_adapter import DeterministicAgentBackend, ProposedToolCall
from heartwood.core_adapter._state import _write_private_json_atomic


@pytest.mark.parametrize(
    "state",
    [
        "{invalid",
        "[]",
        '{"tool_call_id":"","tool_name":"terminal"}',
        '{"tool_call_id":"call-1","tool_name":""}',
    ],
)
def test_corrupt_deterministic_state_fails_closed_and_preserves_evidence(
    tmp_path: Path,
    state: str,
) -> None:
    state_path = tmp_path / "session" / "deterministic.json"
    state_path.parent.mkdir()
    state_path.write_text(state, encoding="utf-8")

    with pytest.raises(ValueError, match="deterministic backend state is invalid"):
        DeterministicAgentBackend(persistence_path=state_path)

    assert state_path.read_text(encoding="utf-8") == state


def test_unreadable_deterministic_state_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = tmp_path / "session" / "deterministic.json"
    state_path.parent.mkdir()
    state_path.write_text("{}", encoding="utf-8")
    original_read_text = Path.read_text

    def fail_state_read(
        path: Path,
        encoding: str | None = None,
        errors: str | None = None,
    ) -> str:
        if path == state_path:
            raise PermissionError("synthetic unreadable state")
        return original_read_text(path, encoding=encoding, errors=errors)

    monkeypatch.setattr(Path, "read_text", fail_state_read)

    with pytest.raises(ValueError, match="deterministic backend state is invalid"):
        DeterministicAgentBackend(persistence_path=state_path)


def test_deterministic_state_uses_shared_private_atomic_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = tmp_path / "session" / "deterministic.json"
    writes: list[tuple[Path, dict[str, object]]] = []

    def record_write(path: Path, payload: dict[str, object]) -> None:
        writes.append((path, payload))
        _write_private_json_atomic(path, payload)

    monkeypatch.setattr(
        "heartwood.core_adapter._facade._write_private_json_atomic",
        record_write,
    )
    backend = DeterministicAgentBackend(persistence_path=state_path)

    backend.submit_turn(session_id="session-1", prompt="Persist privately")

    assert len(writes) == 1
    assert writes[0][0] == state_path
    assert writes[0][1]["tool_name"] == "heartwood.synthetic.noop"
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600


def test_deterministic_restart_preserves_typed_pending_action_evidence(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "session" / "deterministic.json"
    _write_private_json_atomic(
        state_path,
        {
            "tool_call_id": "call-1",
            "action_id": "action-1",
            "tool_name": "file_editor",
            "risk": "medium",
            "summary": "Create the synthetic result",
            "arguments": {
                "command": "create",
                "path": "/project/results/summary.txt",
            },
            "kind": "file-editor",
            "affected_paths": ["results/summary.txt"],
            "project_path": "results/summary.txt",
        },
    )

    restored = DeterministicAgentBackend(persistence_path=state_path)
    pending = restored.pending_action_group(session_id="session-1")

    assert pending is not None
    assert pending.actions == (
        ProposedToolCall(
            tool_call_id="call-1",
            action_id="action-1",
            tool_name="file_editor",
            risk="medium",
            summary="Create the synthetic result",
            arguments={
                "command": "create",
                "path": "/project/results/summary.txt",
            },
            kind="file-editor",
            affected_paths=("results/summary.txt",),
            project_path="results/summary.txt",
        ),
    )


@pytest.mark.parametrize(
    ("persisted_kind", "expected_kind"),
    [
        ("terminal", "terminal"),
        ("file-editor", "file-editor"),
        ("task", "task"),
        ("future-kind", "other"),
        (None, "other"),
    ],
)
def test_deterministic_restart_restores_only_known_action_kinds(
    tmp_path: Path,
    persisted_kind: object,
    expected_kind: str,
) -> None:
    state_path = tmp_path / "session" / "deterministic.json"
    _write_private_json_atomic(
        state_path,
        {
            "tool_call_id": "call-1",
            "action_id": "action-1",
            "tool_name": "synthetic_tool",
            "risk": "unknown",
            "summary": "Run a synthetic action",
            "arguments": {},
            "kind": persisted_kind,
            "affected_paths": "not-a-list",
        },
    )

    restored = DeterministicAgentBackend(persistence_path=state_path)
    pending = restored.pending_action_group(session_id="session-1")

    assert pending is not None
    assert pending.actions[0].kind == expected_kind
    assert pending.actions[0].affected_paths == ()
