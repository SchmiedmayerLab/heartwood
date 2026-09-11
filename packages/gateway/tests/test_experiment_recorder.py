# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import hashlib
import json
import runpy
import subprocess
import sys
from pathlib import Path
from typing import ClassVar
from uuid import uuid4

import pytest

import heartwood.gateway.experiments as recording
import heartwood.persistence._files as files
from heartwood.gateway import ProjectContext, WorkspaceInspectionError
from heartwood.gateway.experiments import ExperimentRecorder, experiment_digest
from heartwood.schemas.experiments import ExperimentDefinition, ExperimentEnvironment


def prepare(tmp_path: Path) -> tuple[ExperimentRecorder, ExperimentDefinition]:
    (tmp_path / "analysis.py").write_text(
        "import json\nfrom pathlib import Path\n"
        "values = [int(v) for v in Path('data.csv').read_text().splitlines()[1:]]\n"
        "Path('result.json').write_text(json.dumps({'mean': sum(values) / len(values)}))\n"
    )
    (tmp_path / "data.csv").write_text("value\n1\n2\n3\n")
    recorder = ExperimentRecorder(ProjectContext(tmp_path), max_file_bytes=4096)
    definition = recorder.describe(
        actor_ref="test-researcher",
        entry_point="analysis.py",
        inputs=("data.csv",),
        outputs=("result.json",),
        parameters={"seed": 42, "private-example": "not-in-export"},
        environment=ExperimentEnvironment(kind="declared", source="declared", sha256="0" * 64),
    )
    return recorder, definition


def test_python_analysis_outputs_trace_to_declared_inputs_and_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder, definition = prepare(tmp_path)
    assert recorder.runs() == ()
    assert recorder.export() == b""
    assert not (tmp_path / ".heartwood").exists()
    monkeypatch.chdir(tmp_path)
    with recorder.record(definition) as run_id:
        runpy.run_path("analysis.py", run_name="__main__")
    assert json.loads((tmp_path / "result.json").read_text()) == {"mean": 2}
    fresh = ExperimentRecorder(ProjectContext(tmp_path))
    (run,) = fresh.runs()
    assert run.run_id == run_id
    assert run.status == "succeeded"
    assert run.definition == definition
    assert (
        run.outputs[0].sha256 == hashlib.sha256((tmp_path / "result.json").read_bytes()).hexdigest()
    )
    assert recorder.export() == fresh.export()
    assert b"not-in-export" not in recorder.export()
    assert b"value\\n1" not in recorder.export()
    assert (
        run.definition.code[0].sha256
        == hashlib.sha256((tmp_path / "analysis.py").read_bytes()).hexdigest()
    )


@pytest.mark.parametrize("failure", ["exception", "cancel", "missing-output", "changed-input"])
def test_unsuccessful_python_runs_keep_identity_and_content_safe_outcome(
    tmp_path: Path, failure: str
) -> None:
    recorder, definition = prepare(tmp_path)
    identity = uuid4()
    expected_error = {
        "exception": RuntimeError,
        "cancel": KeyboardInterrupt,
        "missing-output": WorkspaceInspectionError,
        "changed-input": ValueError,
    }[failure]

    def execute() -> None:
        with recorder.record(definition, run_id=identity):
            if failure == "exception":
                raise RuntimeError("private exception must never be recorded")
            if failure == "cancel":
                raise KeyboardInterrupt("private cancellation text")
            if failure == "changed-input":
                (tmp_path / "data.csv").write_text("changed synthetic input")

    with pytest.raises(expected_error):
        execute()
    (run,) = recorder.runs()
    assert run.run_id == identity
    assert run.status == ("cancelled" if failure == "cancel" else "failed")
    assert run.exit_code is None
    assert not run.outputs
    assert b"private exception" not in recorder.export()
    assert b"private cancellation" not in recorder.export()


def test_same_identity_never_reenters_user_code(tmp_path: Path) -> None:
    recorder, definition = prepare(tmp_path)
    identity = uuid4()
    with recorder.record(definition, run_id=identity):
        (tmp_path / "result.json").write_text("{}")
    before = recorder.export()
    with (
        pytest.raises(ValueError, match="identity already exists"),
        recorder.record(definition, run_id=identity),
    ):
        pytest.fail("A retry must not execute user code")
    assert recorder.export() == before


@pytest.mark.parametrize("path", ["data.csv", "analysis.py"])
def test_changed_preconditions_never_enter_user_code(tmp_path: Path, path: str) -> None:
    recorder, definition = prepare(tmp_path)
    (tmp_path / path).write_text("changed synthetic file")
    with pytest.raises(ValueError, match="code or inputs changed"), recorder.record(definition):
        pytest.fail("Changed execution inputs must not be admitted")
    assert recorder.runs() == ()


def test_preexisting_output_cannot_be_claimed_by_noop(tmp_path: Path) -> None:
    recorder, definition = prepare(tmp_path)
    (tmp_path / "result.json").write_text("{}")
    with pytest.raises(ValueError, match="unused paths"), recorder.record(definition):
        pytest.fail("A stale output must not be counted as produced")
    assert recorder.runs() == ()


@pytest.mark.parametrize("phase", ["start", "finish"])
def test_durable_interruption_never_repeats_a_python_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    recorder, definition = prepare(tmp_path)
    identity = uuid4()
    calls = 0
    executed = 0
    original = files.append_private_bytes

    def interrupt(path: Path, content: bytes) -> None:
        nonlocal calls
        calls += 1
        if calls == (1 if phase == "start" else 2):
            raise OSError("synthetic process interruption")
        original(path, content)

    def execute() -> None:
        nonlocal executed
        with recorder.record(definition, run_id=identity):
            executed += 1
            (tmp_path / "result.json").write_text("{}")

    with monkeypatch.context() as patched:
        patched.setattr(files, "append_private_bytes", interrupt)
        with pytest.raises(OSError, match="synthetic process interruption"):
            execute()
    fresh = ExperimentRecorder(ProjectContext(tmp_path))
    assert fresh.runs()[0].status == ("started" if phase == "start" else "succeeded")
    assert executed == (0 if phase == "start" else 1)
    with (
        pytest.raises(ValueError, match="identity already exists"),
        fresh.record(definition, run_id=identity),
    ):
        pytest.fail("An interrupted execution cannot silently run again")


def test_environment_digest_does_not_capture_paths_urls_or_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Distribution:
        metadata: ClassVar = {"Name": "synthetic-package", "Home-page": "https://private.example"}
        version = "1.2.3"

    monkeypatch.setattr(recording, "distributions", lambda: iter([Distribution()]))
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-secret-do-not-export")
    environment = recording.observed_python_environment()
    assert environment.source == "observed"
    assert len(environment.sha256) == 64
    assert "private.example" not in environment.model_dump_json()
    assert "synthetic-secret" not in environment.model_dump_json()
    assert recording.observed_python_environment() == environment
    Distribution.version = "2.0.0"
    assert recording.observed_python_environment().sha256 != environment.sha256


@pytest.mark.parametrize("value", [float("inf"), float("nan"), object()])
def test_parameter_hash_rejects_non_json_values_without_echoing_them(value: object) -> None:
    with pytest.raises(ValueError, match="finite JSON values"):
        experiment_digest(value)


@pytest.mark.parametrize("budget", [-1, 0, True])
def test_capture_budget_is_explicit_and_positive(tmp_path: Path, budget: int) -> None:
    with pytest.raises(ValueError, match="budget must be positive"):
        ExperimentRecorder(ProjectContext(tmp_path), max_file_bytes=budget)


def test_real_process_loss_preserves_uncertain_outcome_without_reexecution(tmp_path: Path) -> None:
    recorder, definition = prepare(tmp_path)
    identity = uuid4()
    script = """
import os, sys
from pathlib import Path
from uuid import UUID
from heartwood.gateway import ProjectContext
from heartwood.gateway.experiments import ExperimentRecorder
from heartwood.schemas.experiments import ExperimentDefinition
recorder = ExperimentRecorder(ProjectContext(Path(sys.argv[1])))
definition = ExperimentDefinition.model_validate_json(sys.argv[2])
with recorder.record(definition, run_id=UUID(sys.argv[3])):
    Path(sys.argv[1], 'result.json').write_text('{}')
    os._exit(23)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), definition.model_dump_json(), str(identity)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 23, result.stderr
    assert recorder.runs()[0].status == "started"
    assert (tmp_path / "result.json").read_text() == "{}"
    with (
        pytest.raises(ValueError, match="identity already exists"),
        recorder.record(definition, run_id=identity),
    ):
        pytest.fail("Process loss must not repeat possibly completed work")
