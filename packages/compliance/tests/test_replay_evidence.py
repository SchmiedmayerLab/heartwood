# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Fresh-process evaluation replay preserves the operator's state binding."""

import json
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from heartwood.compliance.replay_evidence import main, replay_evidence
from heartwood.compliance.research_runner import _fresh_replay
from heartwood.gateway import ProjectContext, SessionGateway


@pytest.mark.parametrize("protected", [False, True])
def test_fresh_replay_uses_the_bound_state_and_rejects_changed_evidence(
    tmp_path: Path, protected: bool
) -> None:
    project = tmp_path / "research"
    project.mkdir()
    identity = uuid4()
    context = ProjectContext(
        project,
        protected_state=(tmp_path / str(identity)).resolve() if protected else None,
        project_id=identity if protected else None,
    )
    if protected:
        # A project-owned decoy cannot replace the outside state selected by the operator.
        (project / ".heartwood").mkdir()
        (project / ".heartwood/research-notes.txt").write_text("untrusted project state")
    gateway = SessionGateway(project=context, env={})
    try:
        session_id = gateway.create_session("Synthetic replay binding")["session_id"]
        expected = replay_evidence(gateway, session_id)
        assert _fresh_replay(gateway, session_id, expected)
        assert not _fresh_replay(gateway, session_id, {**expected, "projection_sha256": "0" * 64})
        if protected:
            assert (
                project / ".heartwood/research-notes.txt"
            ).read_text() == "untrusted project state"
            assert sorted(path.name for path in (project / ".heartwood").iterdir()) == [
                "research-notes.txt"
            ]
    finally:
        gateway.stop()


def test_replay_command_emits_only_bound_state_digests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "research"
    project.mkdir()
    identity = uuid4()
    protected_state = (tmp_path / str(identity)).resolve()
    gateway = SessionGateway(
        project=ProjectContext(project, protected_state=protected_state, project_id=identity),
        env={},
    )
    try:
        session_id = gateway.create_session("Synthetic private session title")["session_id"]
        expected = replay_evidence(gateway, session_id)
    finally:
        gateway.stop()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "replay_evidence",
            str(project),
            session_id,
            "--protected-state",
            str(protected_state),
            "--project-id",
            str(identity),
        ],
    )
    capsys.readouterr()
    assert main() == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == expected
    assert captured.err == ""
    assert "Synthetic private session title" not in captured.out
    assert str(project) not in captured.out
    assert str(protected_state) not in captured.out
    assert not (project / ".heartwood").exists()
