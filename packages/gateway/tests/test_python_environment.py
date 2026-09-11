# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Environment evidence uses a confined probe, never an agent's unverified report."""

import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from heartwood.core_adapter.research_checks import compare_python_environment
from heartwood.core_adapter.research_workflows import workflow_reproduction_spec
from heartwood.gateway import ProjectContext, RestGateway, RestRequest, SessionGateway
from heartwood.gateway._environment_probe import capture_environment, require_environment
from heartwood.gateway._research_evaluation import ResearchStageEvaluator
from heartwood.gateway.experiments import experiment_digest
from heartwood.gateway.python_environment import (
    inspect_verification_environment,
    observe_python_environment,
)
from heartwood.schemas.python_environment import PythonEnvironmentSnapshot


def snapshot(**changes: object) -> PythonEnvironmentSnapshot:
    return PythonEnvironmentSnapshot.model_validate(
        {
            "implementation": "CPython",
            "python": "3.12.13",
            "system": "Linux",
            "machine": "x86_64",
            "packages": (("example-package", "1.2.0"),),
            **changes,
        }
    )


def test_required_versions_normalize_names_and_allow_unverified_additional_packages() -> None:
    expected = snapshot()
    actual = snapshot(packages=(("Example_Package", "1.2"), ("another", "2.0")))
    assert expected.differences(actual) == ()
    assert actual.differences(expected) == ("package:another",)
    assert expected.differences(snapshot(packages=())) == ("package:example-package",)
    assert expected.differences(snapshot(python="3.13", machine="arm64")) == ("machine", "python")


@pytest.mark.parametrize(
    "packages",
    [
        (("name", "1"), ("Name", "1")),
        (("../path", "1"),),
        (("name", "invalid"),),
        (("name", "https://secret@example.invalid"),),
    ],
)
def test_ambiguous_or_invalid_package_metadata_is_rejected(packages: object) -> None:
    with pytest.raises(ValidationError):
        snapshot(packages=packages)


def test_fingerprint_preserves_existing_experiment_representation() -> None:
    value = snapshot()
    assert value.fingerprint == experiment_digest(
        {
            "implementation": "CPython",
            "python": "3.12.13",
            "system": "Linux",
            "machine": "x86_64",
            "packages": [("example-package", "1.2.0")],
        }
    )
    assert compare_python_environment(
        {
            "environment": value.model_dump_json(),
            "environment-check": value.model_dump_json(),
        }
    )
    assert not compare_python_environment({"environment": "{}", "environment-check": "{}"})


def test_fixed_probe_observes_isolated_python_without_reading_project_code(tmp_path: Path) -> None:
    (tmp_path / "platform.py").write_text("raise RuntimeError('project shadow module')")
    (tmp_path / "results").mkdir()
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-m",
            "heartwood.gateway._environment_probe",
            "--output-dir",
            "results/environment",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        timeout=30,
    )
    assert result.stdout == b""
    recorded = PythonEnvironmentSnapshot.model_validate_json(
        (tmp_path / "results/environment/environment.json").read_bytes()
    )
    assert recorded == inspect_verification_environment()
    assert not (tmp_path / ".heartwood").exists()
    with pytest.raises(FileExistsError):
        capture_environment(ProjectContext(tmp_path), "results/environment")


@pytest.mark.parametrize("path", ["../outside", ".heartwood/environment", "linked/environment"])
def test_probe_rejects_private_or_escaping_output(tmp_path: Path, path: str) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    (project / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises((ValueError, OSError)):
        capture_environment(ProjectContext(project), path)
    assert list(outside.iterdir()) == []


def test_gateway_and_rest_share_read_only_environment_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = snapshot()
    monkeypatch.setattr(
        "heartwood.gateway.python_environment.inspect_verification_environment", lambda: value
    )
    gateway = SessionGateway(project=ProjectContext(tmp_path), env={})
    try:
        assert gateway.verification_environment() == value
        response = RestGateway(gateway).handle(RestRequest("GET", "/research/environment"))
        assert response.status_code == 200
        assert response.body == value.model_dump(mode="json")
        assert list(tmp_path.iterdir()) == []
    finally:
        gateway.stop()


def test_environment_guard_rejects_changed_dependencies_before_analysis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = snapshot()
    (tmp_path / "environment.json").write_text(expected.model_dump_json())
    monkeypatch.setattr(
        "heartwood.gateway._environment_probe.observe_python_environment", lambda: expected
    )
    require_environment(ProjectContext(tmp_path), "environment.json")
    monkeypatch.setattr(
        "heartwood.gateway._environment_probe.observe_python_environment",
        lambda: snapshot(packages=()),
    )
    with pytest.raises(ValueError, match="Python environment changed"):
        require_environment(ProjectContext(tmp_path), "environment.json")
    with pytest.raises(ValueError, match="unavailable"):
        require_environment(ProjectContext(tmp_path), "missing.json")


def test_matching_agent_written_environment_does_not_establish_execution(tmp_path: Path) -> None:
    gateway = SessionGateway(project=ProjectContext(tmp_path), env={})
    try:
        definition = next(
            entry.definition
            for entry in gateway.research_workflows().workflows
            if entry.definition.workflow_id == "result-verification"
        )
        inputs = {item.input_id: f"{item.input_id}.txt" for item in definition.inputs}
        for path in inputs.values():
            (tmp_path / path).write_text("synthetic")
        value = observe_python_environment().model_dump_json()
        (tmp_path / inputs["environment"]).write_text(value)
        binding = gateway.prepare_research_workflow(
            "result-verification", inputs=inputs, output_directory="results"
        )
        assert binding.python_executable == sys.executable
        probe = workflow_reproduction_spec(binding, "environment")
        rerun = workflow_reproduction_spec(binding, "reproduce")
        assert probe is not None
        assert rerun is not None
        assert probe.python_executable == rerun.python_executable == sys.executable
        output = tmp_path / binding.artifact_path("environment-check")
        output.parent.mkdir(parents=True)
        output.write_text(value)
        result = ResearchStageEvaluator(gateway.workspace_inspector).evaluate(
            binding, "environment", model_status="success"
        )
        assert result.checks[0].status == "not_run"
    finally:
        gateway.stop()


def test_inspection_failure_does_not_disclose_subprocess_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise subprocess.CalledProcessError(1, ["synthetic"], stderr=b"private-installation-path")

    monkeypatch.setattr("heartwood.gateway.python_environment.subprocess.run", fail)
    with pytest.raises(ValueError, match="could not be inspected") as error:
        inspect_verification_environment()
    assert "private-installation-path" not in str(error.value)
    gateway = SessionGateway(project=ProjectContext(tmp_path), env={})
    try:
        response = RestGateway(gateway).handle(RestRequest("GET", "/research/environment"))
        assert response.status_code == 503
        assert "private-installation-path" not in str(response.body)
        assert list(tmp_path.iterdir()) == []
    finally:
        gateway.stop()
