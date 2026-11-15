# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""The offline Tasks CLI accepts complete captures and never echoes captured content."""

from __future__ import annotations

import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = Path(__file__).parents[1] / "scripts" / "check_platform_response.py"
_FIXTURE = (
    Path(__file__).parents[3] / "fixtures" / "synthetic" / "platform-contract" / "mcp-tasks.json"
)


def _run(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), "mcp-tasks", str(path)],
        check=False,
        capture_output=True,
        text=True,
    )


def test_cli_checks_the_published_task_transcript_directly() -> None:
    result = _run(_FIXTURE)
    assert result.returncode == 0, result.stderr
    reports = json.loads(result.stdout)["reports"]
    fixture = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    assert len(reports) == len(fixture["exchanges"])
    assert all(report["service"] == "mcp-tasks" for report in reports)
    assert all(report["limitations"] for report in reports)
    assert result.stderr == ""
    for exchange in fixture["exchanges"]:
        assert exchange["name"] not in result.stdout


@pytest.mark.parametrize(
    "transcript",
    [
        None,
        [],
        {},
        {"server_capabilities": {}},
        {"server_capabilities": {}, "exchanges": []},
        {"server_capabilities": [], "exchanges": [{}]},
        {"server_capabilities": {}, "exchanges": {}},
        {"server_capabilities": {}, "exchanges": ["synthetic-secret-value"]},
        {"server_capabilities": {}, "exchanges": [{"request": "synthetic-secret-value"}]},
        {"server_capabilities": {}, "exchanges": [{"request": {}, "response": None}]},
    ],
)
def test_cli_rejects_malformed_or_empty_transcripts_without_payloads(
    tmp_path: Path, transcript: object
) -> None:
    path = tmp_path / "capture.json"
    path.write_text(json.dumps(transcript), encoding="utf-8")
    result = _run(path)
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "Response could not be read or failed contract validation.\n"


@pytest.mark.parametrize("location", ["request", "response", "capability"])
def test_cli_sanitizes_nested_invalid_values_without_partial_reports(
    tmp_path: Path, location: str
) -> None:
    transcript: dict[str, Any] = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    # The first valid exchange must not be printed if any later exchange fails.
    invalid = deepcopy(transcript["exchanges"][-1])
    invalid["name"] = "synthetic-secret-value"
    if location == "request":
        invalid["request"]["params"]["taskId"] = {"nested": ["synthetic-secret-value"]}
    elif location == "response":
        invalid["response"]["result"]["status"] = {"nested": ["synthetic-secret-value"]}
    else:
        transcript["server_capabilities"]["extensions"]["io.modelcontextprotocol/tasks"] = {
            "secret": "synthetic-secret-value"
        }
    transcript["exchanges"].append(invalid)
    path = tmp_path / "capture.json"
    path.write_text(json.dumps(transcript), encoding="utf-8")
    result = _run(path)
    assert result.returncode == 1
    assert result.stdout == ""
    assert "synthetic-secret-value" not in result.stderr
    assert "Traceback" not in result.stderr


def test_cli_success_reports_do_not_echo_valid_capture_values(tmp_path: Path) -> None:
    transcript = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    transcript["exchanges"][0]["name"] = "synthetic-secret-value"
    transcript["exchanges"][0]["request"]["params"]["arguments"]["sensitive"] = {
        "nested": ["synthetic-secret-value"]
    }
    path = tmp_path / "capture.json"
    path.write_text(json.dumps(transcript), encoding="utf-8")
    result = _run(path)
    assert result.returncode == 0, result.stderr
    assert "synthetic-secret-value" not in result.stdout
    assert "synthetic-secret-value" not in result.stderr
