# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Tests for the ``heartwood`` command-line entry point."""

from __future__ import annotations

from pathlib import Path

import pytest

from heartwood.cli import __version__, main


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


def test_unknown_command_is_rejected() -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["nope"])
    assert exit_info.value.code != 0
