# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Tests for the ``heartwood`` command-line entry point."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from heartwood.cli import __version__, _handle_chat_directive, _handle_detect, main
from heartwood.core_adapter import SessionResult
from heartwood.gateway import SessionGateway
from heartwood.session import SessionCommand, SessionEvent


class _EmptyDetectGateway:
    def replay_events(
        self,
        *,
        session_id: str,
        after_sequence: int | None = None,
    ) -> tuple[SessionEvent, ...]:
        _ = (session_id, after_sequence)
        return ()

    def handle(self, command: SessionCommand) -> SessionResult:
        _ = command
        return SessionResult(events=())


def test_no_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    code = main([])
    captured = capsys.readouterr()
    assert code == 0
    assert "usage: heartwood" in captured.out


def test_version_flag_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])
    assert exit_info.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_detect_reports_a_proposal(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    workspace = tmp_path / "sessions"

    code = main(["--workspace", str(workspace), "--session-id", "cli-test", "detect"])
    captured = capsys.readouterr()

    assert code == 0
    assert "environment detection" in captured.out
    assert "Platform:" in captured.out
    assert "proposal only" in captured.out
    assert "Session: cli-test" in captured.out
    assert (workspace / "cli-test" / "events.jsonl").is_file()


def test_detect_handles_missing_detection_event(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    gateway = cast(SessionGateway, _EmptyDetectGateway())

    code = _handle_detect(gateway, workspace=tmp_path, session_id="missing-detection")
    captured = capsys.readouterr()

    assert code == 1
    assert "No detection event recorded." in captured.out


def test_run_transcript_reports_policy_and_tool_events(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "sessions"

    approve = main(
        [
            "--workspace",
            str(workspace),
            "--session-id",
            "cli-run",
            "approve",
            "--target-type",
            "model-call",
            "--target-id",
            "decision-synthetic-model-call",
        ]
    )
    run = main(
        [
            "--workspace",
            str(workspace),
            "--session-id",
            "cli-run",
            "run",
            "--prompt",
            "synthetic run",
            "--endpoint",
            "https://model.local.invalid/v1/chat",
        ]
    )
    captured = capsys.readouterr()

    assert approve == 0
    assert run == 0
    assert "Approval recorded: model-call decision-synthetic-model-call approved" in captured.out
    assert "Model call: allow endpoint=https://model.local.invalid/v1/chat" in captured.out
    assert "Tool proposed: heartwood.synthetic.noop" in captured.out
    assert "Confirmation resolved:" in captured.out
    assert "Tool execution: heartwood.synthetic.noop exit=0" in captured.out
    assert "synthetic run" not in (workspace / "cli-run" / "commands.jsonl").read_text(
        encoding="utf-8"
    )


def test_denied_run_transcript_reports_confirmation_request(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "sessions"

    code = main(
        [
            "--workspace",
            str(workspace),
            "--session-id",
            "cli-denied",
            "run",
            "--endpoint",
            "https://public.example.invalid/v1/chat",
        ]
    )
    captured = capsys.readouterr()

    assert code == 0
    assert "Model call: deny endpoint=https://public.example.invalid/v1/chat" in captured.out
    assert "Confirmation requested:" in captured.out
    assert "Tool execution: heartwood.synthetic.noop exit=1" in captured.out


def test_deny_pause_resume_and_empty_replay_are_rendered(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "sessions"

    assert main(["--workspace", str(workspace), "--session-id", "empty", "replay"]) == 0
    assert (
        main(
            [
                "--workspace",
                str(workspace),
                "--session-id",
                "cli-lifecycle",
                "deny",
                "--target-type",
                "skill",
                "--target-id",
                "heartwood.synthetic.omop-summary",
                "--reason",
                "not needed",
            ]
        )
        == 0
    )
    assert main(["--workspace", str(workspace), "--session-id", "cli-lifecycle", "pause"]) == 0
    assert main(["--workspace", str(workspace), "--session-id", "cli-lifecycle", "resume"]) == 0
    captured = capsys.readouterr()

    assert "No session events recorded." in captured.out
    assert "Approval recorded: skill heartwood.synthetic.omop-summary denied" in captured.out
    assert "Session paused" in captured.out
    assert "Session resumed" in captured.out


def test_interactive_chat_directives(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "sessions"
    lines = iter(
        [
            "summarize",
            "/pause",
            "/resume",
            "/approve model-call decision-synthetic-model-call",
            "/audit-export",
            "/replay",
            "/unknown",
            "/quit",
        ]
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: next(lines))

    code = main(["--workspace", str(workspace), "--session-id", "cli-interactive", "chat"])
    captured = capsys.readouterr()

    assert code == 0
    assert "Heartwood chat." in captured.out
    assert "Agent:" in captured.out
    assert "Session paused" in captured.out
    assert "Session resumed" in captured.out
    assert "Approval recorded: model-call decision-synthetic-model-call approved" in captured.out
    assert "Audit export:" in captured.out
    assert "Unknown directive: /unknown" in captured.out


def test_interactive_chat_handles_malformed_directive(
    capsys: pytest.CaptureFixture[str],
) -> None:
    gateway = cast(SessionGateway, _EmptyDetectGateway())

    _handle_chat_directive(gateway, session_id="cli-interactive", line="/approve 'broken")
    captured = capsys.readouterr()

    assert "Invalid directive syntax." in captured.out


def test_replay_and_audit_export_use_persisted_session_events(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "sessions"
    export_copy = tmp_path / "audit-copy.jsonl"

    assert main(["--workspace", str(workspace), "--session-id", "cli-audit", "detect"]) == 0
    assert main(["--workspace", str(workspace), "--session-id", "cli-audit", "replay"]) == 0
    assert (
        main(
            [
                "--workspace",
                str(workspace),
                "--session-id",
                "cli-audit",
                "audit",
                "export",
                "--output",
                str(export_copy),
            ]
        )
        == 0
    )
    captured = capsys.readouterr()

    assert "Detection proposed: platform=generic dataset=omop-cdm" in captured.out
    assert "Audit export:" in captured.out
    assert export_copy.is_file()
    assert "detection.proposed" in export_copy.read_text(encoding="utf-8")


def test_reviewer_packet_command_writes_synthetic_bundle(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "sessions"
    output = tmp_path / "reviewer"

    assert main(["--workspace", str(workspace), "--session-id", "cli-review", "detect"]) == 0
    assert (
        main(
            [
                "--workspace",
                str(workspace),
                "--session-id",
                "cli-review",
                "reviewer",
                "packet",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    captured = capsys.readouterr()

    assert "Reviewer packet:" in captured.out
    assert (output / "reviewer-packet.md").is_file()
    assert "Synthetic Reviewer Packet" in (output / "reviewer-packet.md").read_text(
        encoding="utf-8"
    )


def test_chat_prompt_emits_agent_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = tmp_path / "sessions"

    code = main(
        [
            "--workspace",
            str(workspace),
            "--session-id",
            "cli-chat",
            "chat",
            "--prompt",
            "summarize",
        ]
    )
    captured = capsys.readouterr()

    assert code == 0
    assert "Agent:" in captured.out


def test_unknown_command_is_rejected() -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["nope"])
    assert exit_info.value.code != 0
