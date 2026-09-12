# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from heartwood.cli import main
from heartwood.gateway import ProjectContext, SessionGateway
from heartwood.gateway.experiments import ExperimentRecorder
from heartwood.schemas.experiments import ExperimentCollection
from heartwood.schemas.python_environment import PythonEnvironmentSnapshot


@pytest.mark.parametrize("explicit", [False, True])
def test_environment_command_is_read_only_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    explicit: bool,
) -> None:
    monkeypatch.chdir(tmp_path)
    arguments = ["--python", sys.executable] if explicit else []
    assert main(["experiments", "environment", *arguments]) == 0
    captured = capsys.readouterr()
    value = PythonEnvironmentSnapshot.model_validate_json(captured.out)
    assert value.implementation == "CPython"
    assert captured.err == ""
    assert list(tmp_path.iterdir()) == []


def script(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str | None = None) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "analysis.py").write_text(
        text
        or "import json, sys\nfrom pathlib import Path\n"
        "values = [int(x) for x in Path('data.csv').read_text().splitlines()]\n"
        "result = {'mean':sum(values)/len(values), 'args':sys.argv[1:]}\n"
        "Path('result.json').write_text(json.dumps(result))\n"
    )
    (tmp_path / "data.csv").write_text("1\n2\n3\n")


def test_cli_command_uses_shared_records_and_literal_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    script(tmp_path, monkeypatch)
    identity = str(uuid4())
    command = [
        "experiments",
        "record",
        "--run-id",
        identity,
        "--input",
        "data.csv",
        "--output",
        "result.json",
        "analysis.py",
        "--seed",
        "42",
        "$(touch should-not-exist)",
        "private-argument",
    ]
    assert main(command) == 0
    result = json.loads((tmp_path / "result.json").read_text())
    assert result == {"mean": 2, "args": command[-4:]}
    assert not (tmp_path / "should-not-exist").exists()
    recorder = ExperimentRecorder(ProjectContext(tmp_path))
    (run,) = recorder.runs()
    assert str(run.run_id) == identity
    assert run.status == "succeeded"
    assert run.exit_code == 0
    assert run.definition.source == "shell"
    assert (
        run.outputs[0].sha256 == hashlib.sha256((tmp_path / "result.json").read_bytes()).hexdigest()
    )
    assert b"private-argument" not in recorder.export()
    assert main(command) == 64
    assert len(recorder.runs()) == 1
    capsys.readouterr()
    assert main(["experiments", "list", "--json"]) == 0
    collection = ExperimentCollection.model_validate_json(capsys.readouterr().out)
    gateway = SessionGateway(project=ProjectContext(tmp_path))
    try:
        assert collection == gateway.experiment_records()
        assert main(["experiments", "export"]) == 0
        assert capsys.readouterr().out == gateway.export_experiments().jsonl
        assert main(["experiments", "export"]) == 0
        assert capsys.readouterr().out.encode() == recorder.export()
    finally:
        gateway.stop()


def test_empty_experiment_inspection_does_not_create_project_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["experiments", "list"]) == 0
    assert "No experiments" in capsys.readouterr().out
    assert main(["experiments", "export"]) == 0
    assert capsys.readouterr().out == ""
    assert not (tmp_path / ".heartwood").exists()


@pytest.mark.parametrize("exit_code", [7, 127])
def test_command_failure_preserves_actual_exit_status_without_output_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exit_code: int
) -> None:
    script(
        tmp_path,
        monkeypatch,
        f"import sys\nprint('private-script-output')\nsys.exit({exit_code})\n",
    )
    assert main(["experiments", "record", "analysis.py"]) == exit_code
    recorder = ExperimentRecorder(ProjectContext(tmp_path))
    (run,) = recorder.runs()
    assert run.status == "failed"
    assert run.exit_code == exit_code
    assert b"private-script-output" not in recorder.export()


@pytest.mark.parametrize("change", ["missing-output", "changed-input"])
def test_zero_command_exit_does_not_override_provenance_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    content = (
        "pass\n"
        if change == "missing-output"
        else "from pathlib import Path\nPath('data.csv').write_text('changed')\n"
    )
    script(tmp_path, monkeypatch, content)
    assert (
        main(
            [
                "experiments",
                "record",
                "--input",
                "data.csv",
                "--output",
                "result.json",
                "analysis.py",
            ]
        )
        == 64
    )
    (run,) = ExperimentRecorder(ProjectContext(tmp_path)).runs()
    assert run.status == "failed"
    assert run.exit_code is None


def test_external_interpreter_requires_explicit_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    script(tmp_path, monkeypatch)
    (tmp_path / "analysis.sh").write_text("printf '%s\\n' synthetic-shell-result > result.txt\n")
    command = ["experiments", "record", "--runner", "sh", "--output", "result.txt"]
    assert main([*command, "analysis.sh"]) == 64
    assert "declared environment" in capsys.readouterr().err
    assert not (tmp_path / ".heartwood").exists()
    assert main([*command, "--environment-sha256", "a" * 64, "analysis.sh"]) == 0
    (run,) = ExperimentRecorder(ProjectContext(tmp_path)).runs()
    assert run.definition.environment.source == "declared"
    assert run.definition.environment.sha256 == "a" * 64
    assert (tmp_path / "result.txt").read_text() == "synthetic-shell-result\n"


@pytest.mark.parametrize("path", ["../analysis.py", ".heartwood/analysis.py", "missing.py"])
def test_unsafe_or_unavailable_script_never_executes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["experiments", "record", path]) == 64
    assert not (tmp_path / ".heartwood").exists()


@pytest.mark.parametrize("mode", ["interrupt", "missing-runner", "signal"])
def test_interrupted_or_failed_spawn_has_a_content_safe_terminal_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    script(tmp_path, monkeypatch)

    def fail(*_args: object, **_kwargs: object) -> None:
        if mode == "interrupt":
            raise KeyboardInterrupt
        if mode == "signal":
            raise subprocess.CalledProcessError(-15, "private-command")
        raise OSError("private-spawn-details")

    monkeypatch.setattr(subprocess, "run", fail)
    assert (
        main(["experiments", "record", "analysis.py"])
        == {"interrupt": 130, "missing-runner": 74, "signal": 143}[mode]
    )
    recorder = ExperimentRecorder(ProjectContext(tmp_path))
    (run,) = recorder.runs()
    assert run.status == ("cancelled" if mode == "interrupt" else "failed")
    assert run.exit_code == (-15 if mode == "signal" else None)
    assert b"private-" not in recorder.export()


def test_abrupt_command_loss_can_be_recovered_and_cancelled_without_rerun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    script(tmp_path, monkeypatch)
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            "\n".join(
                (
                    "import os",
                    "from heartwood.gateway import ProjectContext",
                    "from heartwood.gateway.experiments import ExperimentRecorder",
                    "r=ExperimentRecorder(ProjectContext.current())",
                    "d=r.describe(actor_ref='test',entry_point='analysis.py',"
                    "inputs=(),outputs=(),parameters={})",
                    "with r.record(d): os._exit(23)",
                )
            ),
        ],
        cwd=tmp_path,
        check=False,
    )
    assert child.returncode == 23
    recorder = ExperimentRecorder(ProjectContext(tmp_path))
    (run,) = recorder.runs()
    identity = str(run.run_id)
    assert main(["experiments", "recover", identity]) == 0
    assert recorder.runs()[0].status == "interrupted"
    assert main(["experiments", "cancel", identity]) == 0
    assert recorder.runs()[0].status == "cancelled"
    assert not (tmp_path / "result.json").exists()
    assert main(["experiments", "list"]) == 0
    assert f"{identity}  cancelled" in capsys.readouterr().out
