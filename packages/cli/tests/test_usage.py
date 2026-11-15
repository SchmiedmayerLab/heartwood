# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import json
from pathlib import Path

import pytest

from heartwood.cli import main
from heartwood.gateway import ProjectContext, SessionGateway
from heartwood.schemas.usage import ProjectUsageSummary
from heartwood.session import CommandKind, SessionCommand

_PROMPT = "synthetic-private-prompt-for-usage"


def _project_with_one_turn(root: Path) -> ProjectContext:
    project = ProjectContext(root)
    gateway = SessionGateway(project=project, env={}, backend_id="deterministic")
    try:
        gateway.handle(
            SessionCommand(
                command_id="usage-chat",
                session_id="session-1",
                kind=CommandKind.CHAT,
                created_at="2026-01-01T00:00:00Z",
                payload={"prompt": _PROMPT},
            )
        )
    finally:
        gateway.stop()
    return project


def test_usage_summary_prints_the_published_schema_and_readable_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _project_with_one_turn(tmp_path)
    monkeypatch.chdir(tmp_path)

    assert main(["usage", "summary", "--json"]) == 0
    output = capsys.readouterr().out
    summary = ProjectUsageSummary.model_validate_json(output)
    assert summary.summary_id == project.usage_summary_id()
    assert summary.counts is not None
    assert summary.counts.turns == 1
    assert _PROMPT not in output

    assert main(["usage", "summary"]) == 0
    text = capsys.readouterr().out
    assert "Audit history: verified" in text
    assert "Turns: 1" in text
    assert _PROMPT not in text


def test_usage_summary_fails_without_partial_counts_when_history_is_damaged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _project_with_one_turn(tmp_path)
    audit = project.sessions_dir / "session-1" / "audit.jsonl"
    audit.write_text(audit.read_text(encoding="utf-8").replace('"payload"', '"payload_"', 1))
    monkeypatch.chdir(tmp_path)

    assert main(["usage", "summary", "--json"]) == 1
    document = json.loads(capsys.readouterr().out)
    assert document["audit_verification"] == "failed"
    assert document["counts"] is None


def test_usage_summary_requires_project_state_without_creating_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)

    assert main(["usage", "summary"]) == 1
    assert "not initialized" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []
