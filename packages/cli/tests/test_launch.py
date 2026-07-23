# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import hashlib
import io
import json
import subprocess
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Literal, cast

import pytest

from heartwood.adapters.platform import select_platform_adapter
from heartwood.cli._launch import (
    LaunchConfigurationError,
    LaunchOptions,
    LaunchPlan,
    LocalRuntimeSelection,
    _allocation_resources,
    _available_gpu_memory_bytes,
    _available_system_memory_bytes,
    _catalog_contains_model,
    _context_plan,
    _ensure_setup,
    _format_bytes,
    _gguf_file,
    _interaction_command,
    _local_model_selection,
    _minimum_compute_capability,
    _model_size,
    _persist_effective_context,
    _preflight_vllm,
    _print_copy_progress,
    _print_resource_assessment,
    _print_runtime_failure,
    _reentry_command,
    _resolve_runtime_executable,
    _run_command,
    _run_interaction,
    _runtime_command,
    _runtime_environment,
    _stage_model,
    _use_eager_vllm,
    _verify_local_model,
    _wait_for_runtime,
    build_launch_plan,
    run_launch,
)
from heartwood.cli._model_snapshot import verify_snapshot
from heartwood.gateway import (
    GpuDevice,
    ModelSettings,
    ProjectConfig,
    ProjectConfigStore,
    ProjectContext,
    SessionGateway,
    SlurmGpuPartition,
    model_profile_from_preset,
)


def _options(
    tmp_path: Path,
    *,
    selected: bool = True,
    runtime: str = "vllm",
    context_window: int = 32_768,
    qualification: Literal["unvalidated", "qualified"] = "qualified",
    qualification_date: str | None = None,
    qualification_evidence: str | None = None,
    **overrides: object,
) -> LaunchOptions:
    tmp_path.mkdir(parents=True, exist_ok=True)
    project = ProjectContext(tmp_path)
    project.initialize()
    if selected:
        if qualification == "qualified":
            qualification_date = qualification_date or "2026-07-22"
            qualification_evidence = qualification_evidence or "https://example.test/qualification"
        adapter = select_platform_adapter({"HEARTWOOD_PLATFORM": "generic"})
        store = ProjectConfigStore(
            project,
            ProjectConfig(
                platform_id=adapter.adapter_id,
                policy=adapter.default_policy_profile(),
            ),
        )
        store.select_local_model(
            artifact_id="test-model",
            path=project.models_dir / ("model.gguf" if runtime == "llama-cpp" else "model"),
            runtime=runtime,
            model_id="test-model",
            display_name="Synthetic test model",
            source_repository="example/test-model",
            source_revision="1" * 40,
            size_bytes=1024,
            minimum_free_bytes=2048,
            license_posture="Synthetic test model.",
            license_id="Apache-2.0",
            context_window=context_window,
            maximum_context_window=context_window,
            minimum_resource_envelope="Synthetic minimum resources.",
            recommended_resource_envelope="Synthetic recommended resources.",
            precision="Synthetic",
            qualification=qualification,
            qualification_date=qualification_date,
            qualification_evidence=qualification_evidence,
            minimum_gpu_count=0 if runtime == "llama-cpp" else 1,
            minimum_gpu_memory_bytes=0 if runtime == "llama-cpp" else 1,
            recommended_cpu_count=16,
            recommended_ram_bytes=16 * 1024**3,
            recommended_disk_bytes=32 * 1024**3,
            tool_call_parser=None if runtime == "llama-cpp" else "hermes",
            catalog_source="user-selected",
        )
    values: dict[str, object] = {
        "project": project,
        "session_id": "launch-test",
        "partition": "gpu",
        "gpus": 1,
        "cpus": 8,
        "memory": "64G",
        "time_limit": "02:00:00",
        "task_profile": "auto",
        "dry_run": False,
        "no_allocate": False,
        "yes_request_allocation": False,
        "yes_download": False,
        "inside_allocation": False,
        "plain": True,
        "web": False,
        "web_host": "127.0.0.1",
        "web_port": 8767,
        "startup_timeout": 600,
        "prompt": None,
        "prompt_file": None,
    }
    values.update(overrides)
    return LaunchOptions(**values)  # type: ignore[arg-type]


def _partition(
    name: str = "gpu",
    *,
    default: bool = True,
    gpu_model: str = "nvidia_l40s",
    gpu_count: int = 8,
) -> SlurmGpuPartition:
    return SlurmGpuPartition(
        name=name,
        is_default=default,
        gpu_model=gpu_model,
        gpu_count=gpu_count,
        node_memory_bytes=512 * 1024**3,
        node_cpu_count=64,
        state="up",
    )


@pytest.fixture(autouse=True)
def _synthetic_visible_gpu(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "heartwood.gateway._gpu_environment.discover_visible_gpus",
        lambda _env: (
            GpuDevice(
                index=0,
                name="Tesla T4",
                total_memory_bytes=16_000_000_000,
                free_memory_bytes=15_000_000_000,
                driver_version="570.86.15",
                compute_capability=(7, 5),
            ),
        ),
    )


def _snapshot(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    weights = root / "weights.safetensors"
    weights.write_bytes(b"synthetic")
    digest = hashlib.sha256(weights.read_bytes()).hexdigest()
    (root / "SHA256SUMS").write_text(f"{digest}  weights.safetensors\n", encoding="utf-8")


def test_carina_plan_preserves_project_and_exports_no_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("heartwood.cli._launch.discover_slurm_gpu_partitions", lambda _env: ())
    options = _options(tmp_path, cpus=None, memory=None)
    plan = build_launch_plan(
        options,
        {
            "HEARTWOOD_PLATFORM": "carina",
            "PATH": "/usr/bin",
            "HOME": "/home/researcher",
            "OPENAI_API_KEY": "secret",
        },
    )

    assert plan.platform_id == "carina"
    assert plan.allocation_required
    assert plan.context_window == 32_768
    assert "Context capacity: up to 32,768 tokens" in plan.format()
    assert "Qualification: Qualified" in plan.format()
    assert plan.allocation_command[:3] == ("srun", "--pty", "--partition=gpu")
    assert plan.cpus == 16
    assert "--cpus-per-task=16" in plan.allocation_command
    assert f"--chdir={tmp_path}" in plan.allocation_command
    export = next(item for item in plan.allocation_command if item.startswith("--export="))
    assert export == "--export=PATH,HOME,HEARTWOOD_PLATFORM=carina"
    assert "OPENAI_API_KEY" not in export
    assert "--workspace" not in plan.allocation_command
    assert "--model-root" not in plan.allocation_command


def test_user_selected_model_scales_default_cpus_with_gpu_count(tmp_path: Path) -> None:
    options = _options(tmp_path, cpus=None, gpus=4)
    selection = _local_model_selection(options.project, {"HEARTWOOD_PLATFORM": "generic"})
    assert selection is not None
    selection = replace(
        selection,
        catalog_source="user-selected",
        minimum_gpu_count=4,
        tensor_parallel_size=4,
        recommended_cpu_count=8,
    )

    gpus, cpus, _memory = _allocation_resources(options, selection)

    assert gpus == 4
    assert cpus == 32


def test_qualified_context_requires_catalog_memory_for_every_gpu(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = _options(tmp_path)
    selection = _local_model_selection(options.project, {"HEARTWOOD_PLATFORM": "generic"})
    assert selection is not None
    selection = replace(
        selection,
        catalog_source="catalog",
        minimum_gpu_memory_bytes=15_000_000_000,
        tensor_parallel_size=2,
    )

    def available_memory(_env: object, *, count: int) -> int:
        assert count == 2
        return 20_000_000_000

    monkeypatch.setattr(
        "heartwood.cli._launch._available_gpu_memory_bytes",
        available_memory,
    )

    with pytest.raises(ValueError, match=r"27\.9 GiB is required and 18\.6 GiB is available"):
        _context_plan(selection, {"PATH": str(tmp_path)})

    def unavailable_memory(_env: object, *, count: int) -> None:
        assert count == 2

    monkeypatch.setattr(
        "heartwood.cli._launch._available_gpu_memory_bytes",
        unavailable_memory,
    )
    plan = _context_plan(selection, {"PATH": str(tmp_path)})
    assert plan.effective_window == 32_768
    assert plan.estimated_required_bytes == 30_000_000_000
    assert "verified after allocation" in plan.reason


def test_launch_plan_rejects_gpu_count_outside_the_model_qualification(tmp_path: Path) -> None:
    with pytest.raises(LaunchConfigurationError, match="qualified with 1 GPU"):
        build_launch_plan(
            _options(tmp_path, gpus=2),
            {"HEARTWOOD_PLATFORM": "carina"},
        )


def test_launch_plan_labels_unvalidated_configuration(tmp_path: Path) -> None:
    plan = build_launch_plan(
        _options(tmp_path, qualification="unvalidated"),
        {"HEARTWOOD_PLATFORM": "terra", "PATH": "/usr/bin"},
    )

    assert "Qualification: Not tested" in plan.format()


def test_launch_plan_dates_a_qualified_platform_configuration(tmp_path: Path) -> None:
    plan = build_launch_plan(
        _options(
            tmp_path,
            qualification="qualified",
            qualification_date="2026-07-22",
            qualification_evidence="https://example.test/qualification",
        ),
        {"HEARTWOOD_PLATFORM": "carina", "PATH": "/usr/bin"},
    )

    assert "Qualification: Qualified (2026-07-22)" in plan.format()


@pytest.mark.parametrize(
    ("model_id", "precision", "expected"),
    [
        ("gpt-oss-120b-vllm", "MXFP4", (8, 0)),
        ("test-model", "FP8", (8, 9)),
        ("qwen3-coder-awq", "W4A16 AWQ", None),
    ],
)
def test_launch_preflight_infers_minimum_compute_capability(
    tmp_path: Path,
    model_id: str,
    precision: str,
    expected: tuple[int, int] | None,
) -> None:
    selection = _local_model_selection(_options(tmp_path).project, {})
    assert selection is not None
    selection = LocalRuntimeSelection(
        artifact_id=selection.artifact_id,
        model_root=selection.model_root,
        runtime=selection.runtime,
        model_id=model_id,
        size_bytes=selection.size_bytes,
        artifact_sha256=selection.artifact_sha256,
        context_window=selection.context_window,
        maximum_context_window=selection.maximum_context_window,
        tier=selection.tier,
        precision=precision,
        qualification=selection.qualification,
        minimum_gpu_count=selection.minimum_gpu_count,
        minimum_gpu_memory_bytes=selection.minimum_gpu_memory_bytes,
        recommended_ram_bytes=selection.recommended_ram_bytes,
        recommended_disk_bytes=selection.recommended_disk_bytes,
        tool_call_parser=selection.tool_call_parser,
        tensor_parallel_size=selection.tensor_parallel_size,
        startup_seconds_min=selection.startup_seconds_min,
        startup_seconds_max=selection.startup_seconds_max,
        catalog_source=selection.catalog_source,
    )

    assert _minimum_compute_capability(selection) == expected


def test_launch_requires_consent_and_honors_dry_run_and_no_allocate(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "heartwood.cli._launch.discover_slurm_gpu_partitions",
        lambda _env: (_partition(),),
    )
    called = False

    def runner(_command: object) -> int:
        nonlocal called
        called = True
        return 0

    assert (
        run_launch(
            _options(tmp_path),
            env={"HEARTWOOD_PLATFORM": "carina"},
            input_fn=lambda _prompt: "n",
            run_fn=runner,
        )
        == 1
    )
    assert not called
    assert "Allocation cancelled" in capsys.readouterr().out
    assert (
        run_launch(
            _options(tmp_path, dry_run=True),
            env={"HEARTWOOD_PLATFORM": "carina"},
            run_fn=runner,
        )
        == 0
    )
    assert (
        run_launch(
            _options(tmp_path, no_allocate=True),
            env={"HEARTWOOD_PLATFORM": "carina"},
            run_fn=runner,
        )
        == 1
    )


def test_launch_handles_closed_consent_and_submits_approved_plan(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "heartwood.cli._launch.discover_slurm_gpu_partitions",
        lambda _env: (_partition(),),
    )
    submitted: list[tuple[str, ...]] = []

    def closed(_prompt: str) -> str:
        raise EOFError

    def run_successfully(command: Sequence[str]) -> int:
        submitted.append(tuple(command))
        return 0

    def run_with_failure(command: Sequence[str]) -> int:
        submitted.append(tuple(command))
        return 23

    assert (
        run_launch(
            _options(tmp_path / "closed"),
            env={"HEARTWOOD_PLATFORM": "carina"},
            input_fn=closed,
            run_fn=run_successfully,
        )
        == 1
    )
    assert submitted == []
    assert "Allocation cancelled" in capsys.readouterr().out

    assert (
        run_launch(
            _options(tmp_path / "approved"),
            env={"HEARTWOOD_PLATFORM": "carina"},
            input_fn=lambda _prompt: "Y",
            run_fn=run_with_failure,
        )
        == 23
    )
    assert submitted[0][:3] == ("srun", "--pty", "--partition=gpu")


def test_launch_reports_missing_selection_artifact_and_runtime(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = {"HEARTWOOD_PLATFORM": "generic"}
    assert run_launch(_options(tmp_path, selected=False), env=env) == 64
    assert "No qualified Heartwood-managed model" in capsys.readouterr().out
    assert run_launch(_options(tmp_path), env=env) == 66
    _snapshot(tmp_path / ".heartwood" / "models" / "model")
    monkeypatch.setattr(
        "heartwood.cli._launch._resolve_runtime_executable",
        lambda _runtime: tmp_path / "missing-vllm",
    )
    assert run_launch(_options(tmp_path), env=env) == 69
    assert "vLLM executable is unavailable" in capsys.readouterr().out


def test_recommended_model_cannot_download_or_allocate_before_setup(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = _options(tmp_path, selected=False, yes_download=True, yes_request_allocation=True)
    runner_called = False

    def runner(_command: Sequence[str]) -> int:
        nonlocal runner_called
        runner_called = True
        return 0

    plan = LaunchPlan(
        platform_id="carina",
        allocation_required=True,
        allocation_command=("srun", "--pty", "heartwood"),
        model_root=options.project.models_dir / "recommended-model",
        state_root=options.project.state_root,
        project_root=options.project.root,
        runtime="vllm",
        model_id="heartwood-managed-model",
        artifact_id="recommended-model",
        context_window=32_768,
        download_required=True,
        model_selected=False,
    )
    monkeypatch.setattr("heartwood.cli._launch.build_launch_plan", lambda *_args: plan)

    assert run_launch(options, env={"HEARTWOOD_PLATFORM": "carina"}, run_fn=runner) == 64
    output = capsys.readouterr().out
    assert "Model status: recommendation only" in output
    assert "Run `heartwood` to choose Run with Heartwood" in output
    assert not runner_called
    assert plan.model_root is not None
    assert not plan.model_root.exists()


def test_launch_rejects_short_context_before_starting_runtime(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = _options(tmp_path, runtime="vllm", context_window=4_096)
    _snapshot(options.project.models_dir / "model")
    executable = tmp_path / "vllm"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setattr(
        "heartwood.cli._launch._resolve_runtime_executable", lambda _runtime: executable
    )

    assert run_launch(options, env={"HEARTWOOD_PLATFORM": "generic"}) == 64
    assert "at least 18,432 tokens" in capsys.readouterr().out


def test_explicit_runtime_start_rejects_unsupported_terra_web_interface(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    options = _options(tmp_path, web=True, dry_run=True)

    assert run_launch(options, env={"HEARTWOOD_PLATFORM": "terra"}) == 64
    output = capsys.readouterr().out
    assert "Terra does not provide the web interface" in output
    assert "Use the terminal interface" in output


def test_launch_scrubs_resource_and_runtime_preflight_environments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = _options(tmp_path)
    _snapshot(tmp_path / ".heartwood" / "models" / "model")
    executable = tmp_path / "heartwood-vllm"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    observed: list[dict[str, str]] = []

    monkeypatch.setattr(
        "heartwood.cli._launch._resolve_runtime_executable",
        lambda _runtime: executable,
    )
    monkeypatch.setattr(
        "heartwood.cli._launch._print_resource_assessment",
        lambda _selection, env, **_kwargs: observed.append(dict(env)),
    )

    def stop_after_preflight(_executable: Path, env: dict[str, str]) -> str:
        observed.append(dict(env))
        return "synthetic stop"

    monkeypatch.setattr("heartwood.cli._launch._preflight_vllm", stop_after_preflight)

    assert (
        run_launch(
            options,
            env={
                "HEARTWOOD_PLATFORM": "generic",
                "PATH": "/usr/bin",
                "OPENAI_API_KEY": "secret",
            },
        )
        == 69
    )
    assert len(observed) == 2
    assert all("OPENAI_API_KEY" not in env for env in observed)


def test_launch_checks_scratch_capacity_and_carina_scratch_requirement(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = _options(tmp_path)
    _snapshot(tmp_path / ".heartwood" / "models" / "model")
    executable = tmp_path / "vllm"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setattr(
        "heartwood.cli._launch._resolve_runtime_executable", lambda _kind: executable
    )
    monkeypatch.setattr("heartwood.cli._launch._preflight_vllm", lambda *_args: None)

    assert (
        run_launch(
            options,
            env={"HEARTWOOD_PLATFORM": "carina", "SLURM_JOB_ID": "synthetic"},
        )
        == 72
    )
    assert "does not expose LOCAL_SCRATCH_JOB" in capsys.readouterr().out

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(
        "heartwood.cli._launch.shutil.disk_usage",
        lambda _path: SimpleNamespace(total=1, used=1, free=1),
    )
    assert (
        run_launch(
            options,
            env={
                "HEARTWOOD_PLATFORM": "carina",
                "SLURM_JOB_ID": "synthetic",
                "LOCAL_SCRATCH_JOB": str(scratch),
            },
        )
        == 73
    )


def test_launch_rejects_unavailable_scratch_and_staging_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = _options(tmp_path)
    _snapshot(options.project.models_dir / "model")
    executable = tmp_path / "vllm"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setattr(
        "heartwood.cli._launch._resolve_runtime_executable", lambda _kind: executable
    )
    monkeypatch.setattr("heartwood.cli._launch._preflight_vllm", lambda *_args: None)

    assert (
        run_launch(
            options,
            env={
                "HEARTWOOD_PLATFORM": "carina",
                "SLURM_JOB_ID": "synthetic",
                "LOCAL_SCRATCH_JOB": str(tmp_path / "missing"),
            },
        )
        == 72
    )
    assert "unavailable or not writable" in capsys.readouterr().out

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(
        "heartwood.cli._launch._stage_model",
        lambda *_args: (_ for _ in ()).throw(OSError("synthetic scratch failure")),
    )
    assert (
        run_launch(
            options,
            env={
                "HEARTWOOD_PLATFORM": "carina",
                "SLURM_JOB_ID": "synthetic",
                "LOCAL_SCRATCH_JOB": str(scratch),
            },
        )
        == 74
    )
    assert "Model staging failed: synthetic scratch failure" in capsys.readouterr().out


def test_launch_rejects_staged_snapshot_that_changes_during_copy(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = _options(tmp_path)
    _snapshot(options.project.models_dir / "model")
    executable = tmp_path / "vllm"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    calls = 0

    def verifier(_root: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ValueError("synthetic copy changed")

    monkeypatch.setattr(
        "heartwood.cli._launch._resolve_runtime_executable", lambda _kind: executable
    )
    monkeypatch.setattr("heartwood.cli._launch._preflight_vllm", lambda *_args: None)
    monkeypatch.setattr("heartwood.cli._launch.verify_snapshot", verifier)

    assert (
        run_launch(
            options,
            env={
                "HEARTWOOD_PLATFORM": "carina",
                "SLURM_JOB_ID": "synthetic",
                "LOCAL_SCRATCH_JOB": str(scratch),
            },
        )
        == 66
    )
    assert "Staged model verification failed: synthetic copy changed" in capsys.readouterr().out
    assert not tuple(scratch.iterdir())


def test_launch_supervises_selected_vllm_and_opens_project_chat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = _options(tmp_path, context_window=131_072)
    model = tmp_path / ".heartwood" / "models" / "model"
    _snapshot(model)
    executable = tmp_path / "vllm"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    observed_processes: list[tuple[str, ...]] = []
    observed_runs: list[tuple[tuple[str, ...], Path | None]] = []
    observed_environments: list[dict[str, str]] = []

    class FakeProcess:
        def __init__(self, command: object, **_kwargs: object) -> None:
            observed_processes.append(tuple(command))  # type: ignore[arg-type]
            self.running = True

        def poll(self) -> int | None:
            return None if self.running else 0

        def terminate(self) -> None:
            self.running = False

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            return 0

        def kill(self) -> None:
            self.running = False

    def completed(
        command: Sequence[str], **kwargs: object
    ) -> subprocess.CompletedProcess[Sequence[str]]:
        observed_runs.append((tuple(command), cast(Path | None, kwargs.get("cwd"))))
        observed_environments.append(dict(cast(dict[str, str], kwargs.get("env", {}))))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(
        "heartwood.cli._launch._resolve_runtime_executable", lambda _kind: executable
    )
    monkeypatch.setattr("heartwood.cli._launch._preflight_vllm", lambda *_args: None)
    monkeypatch.setattr(
        "heartwood.cli._launch._available_gpu_memory_bytes",
        lambda _env, **_kwargs: 48 * 1024**3,
    )
    monkeypatch.setattr("heartwood.cli._launch.subprocess.Popen", FakeProcess)
    monkeypatch.setattr("heartwood.cli._launch._wait_for_runtime", lambda *_args, **_kwargs: True)
    setup_options: list[dict[str, object]] = []

    def ensure_setup(*_args: object, **kwargs: object) -> int:
        setup_options.append(kwargs)
        return 0

    monkeypatch.setattr(
        "heartwood.cli._launch._ensure_setup",
        ensure_setup,
    )
    monkeypatch.setattr("heartwood.cli._launch.subprocess.run", completed)

    assert run_launch(options, env={"HEARTWOOD_PLATFORM": "generic"}) == 0
    assert observed_processes[0][0] == str(executable)
    assert "--enable-auto-tool-choice" in observed_processes[0]
    context_index = observed_processes[0].index("--max-model-len")
    assert observed_processes[0][context_index + 1] == "131072"
    assert setup_options == [{"context_window": 131_072}]
    assert observed_runs[-1][1] == tmp_path
    assert observed_environments[-1]["HEARTWOOD_LOCAL_RUNTIME_ACTIVE"] == "1"
    assert observed_environments[-1]["HEARTWOOD_LOCAL_RUNTIME_ARTIFACT_ID"] == "test-model"
    assert "--workspace" not in observed_runs[-1][0]
    assert (tmp_path / ".heartwood" / "logs" / "local-model.log").is_file()
    assert not (tmp_path / ".heartwood" / "runtime" / "scratch").exists()


def test_launch_reports_runtime_timeout_and_forces_cleanup(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = _options(tmp_path, startup_timeout=7)
    _snapshot(options.project.models_dir / "model")
    executable = tmp_path / "vllm"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    lifecycle: list[str] = []

    class StubbornProcess:
        def __init__(self, _command: object, **_kwargs: object) -> None:
            self.running = True

        def poll(self) -> int | None:
            return None if self.running else -9

        def terminate(self) -> None:
            lifecycle.append("terminate")

        def wait(self, timeout: float | None = None) -> int:
            if timeout is not None and self.running:
                raise subprocess.TimeoutExpired("vllm", timeout)
            return -9

        def kill(self) -> None:
            lifecycle.append("kill")
            self.running = False

    monkeypatch.setattr(
        "heartwood.cli._launch._resolve_runtime_executable", lambda _kind: executable
    )
    monkeypatch.setattr("heartwood.cli._launch._preflight_vllm", lambda *_args: None)
    monkeypatch.setattr("heartwood.cli._launch.subprocess.Popen", StubbornProcess)
    monkeypatch.setattr("heartwood.cli._launch._wait_for_runtime", lambda *_args, **_kwargs: False)

    assert run_launch(options, env={"HEARTWOOD_PLATFORM": "generic"}) == 70
    assert lifecycle == ["terminate", "kill"]
    output = capsys.readouterr().out
    assert "did not become ready after 7 seconds" in output
    assert "Runtime log:" in output


def test_launch_reports_early_runtime_exit_without_claiming_timeout(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = _options(tmp_path, startup_timeout=600)
    _snapshot(options.project.models_dir / "model")
    executable = tmp_path / "vllm"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)

    class FailedProcess:
        def __init__(self, _command: object, **_kwargs: object) -> None:
            pass

        def poll(self) -> int:
            return 42

        def terminate(self) -> None:
            pytest.fail("an exited process must not be terminated")

    monkeypatch.setattr(
        "heartwood.cli._launch._resolve_runtime_executable", lambda _kind: executable
    )
    monkeypatch.setattr("heartwood.cli._launch._preflight_vllm", lambda *_args: None)
    monkeypatch.setattr("heartwood.cli._launch.subprocess.Popen", FailedProcess)
    monkeypatch.setattr("heartwood.cli._launch._wait_for_runtime", lambda *_args, **_kwargs: False)

    assert run_launch(options, env={"HEARTWOOD_PLATFORM": "generic"}) == 70
    output = capsys.readouterr().out
    assert "vLLM exited before becoming ready" in output
    assert "after 600 seconds" not in output
    assert "vLLM exited with status 42" in output


def test_llama_cpp_command_uses_the_selected_gguf(tmp_path: Path) -> None:
    model = tmp_path / "model.gguf"
    model.write_bytes(b"synthetic")
    selection = LocalRuntimeSelection(
        artifact_id="test-model",
        model_root=model,
        runtime="llama-cpp",
        model_id="heartwood-managed-model",
        size_bytes=model.stat().st_size,
        artifact_sha256=hashlib.sha256(model.read_bytes()).hexdigest(),
        context_window=32_768,
        maximum_context_window=32_768,
        tier="standard",
        precision="GGUF synthetic",
        qualification="qualified",
        minimum_gpu_count=0,
        minimum_gpu_memory_bytes=0,
        recommended_ram_bytes=16 * 1024**3,
        recommended_disk_bytes=32 * 1024**3,
        tool_call_parser=None,
        tensor_parallel_size=1,
        startup_seconds_min=1,
        startup_seconds_max=2,
        catalog_source="user-selected",
    )
    command = _runtime_command(
        Path("/opt/llama.cpp/llama-server"),
        model,
        selection,
    )
    assert command[:3] == (
        "/opt/llama.cpp/llama-server",
        "--model",
        str(model),
    )
    assert "--alias" in command
    assert "--jinja" in command
    context_index = command.index("--ctx-size")
    assert command[context_index + 1] == "32768"


def test_resource_assessment_reports_context_and_memory_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    selection = LocalRuntimeSelection(
        artifact_id="test-model",
        model_root=tmp_path / "model",
        runtime="vllm",
        model_id="test-model",
        size_bytes=10 * 1024**3,
        artifact_sha256=None,
        context_window=32_768,
        maximum_context_window=32_768,
        tier="standard",
        precision="Synthetic",
        qualification="qualified",
        minimum_gpu_count=1,
        minimum_gpu_memory_bytes=1,
        recommended_ram_bytes=16 * 1024**3,
        recommended_disk_bytes=32 * 1024**3,
        tool_call_parser="hermes",
        tensor_parallel_size=1,
        startup_seconds_min=1,
        startup_seconds_max=2,
        catalog_source="user-selected",
    )
    monkeypatch.setattr(
        "heartwood.cli._launch._available_system_memory_bytes",
        lambda: 16 * 1024**3,
    )
    monkeypatch.setattr(
        "heartwood.cli._launch._available_gpu_memory_bytes",
        lambda _env, **_kwargs: 24 * 1024**3,
    )

    _print_resource_assessment(selection, {"PATH": "/usr/bin"})

    output = capsys.readouterr().out
    assert "Context window: 32,768 tokens selected automatically" in output
    assert "model capacity: 32,768" in output
    assert "Warning: RAM may be insufficient" in output
    assert "GPU memory available; estimated minimum" in output
    assert "Runtime mode: eager execution selected" not in output


def test_constrained_vllm_gpu_uses_eager_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    selection = LocalRuntimeSelection(
        artifact_id="test-model",
        model_root=tmp_path / "model",
        runtime="vllm",
        model_id="test-model",
        size_bytes=5 * 1024**3,
        artifact_sha256=None,
        context_window=18_432,
        maximum_context_window=32_768,
        tier="standard",
        precision="AWQ int4",
        qualification="unvalidated",
        minimum_gpu_count=1,
        minimum_gpu_memory_bytes=15_000_000_000,
        recommended_ram_bytes=32 * 1024**3,
        recommended_disk_bytes=16 * 1024**3,
        tool_call_parser="hermes",
        tensor_parallel_size=1,
        startup_seconds_min=120,
        startup_seconds_max=480,
        catalog_source="catalog",
    )
    monkeypatch.setattr(
        "heartwood.cli._launch._available_gpu_memory_bytes",
        lambda _env, **_kwargs: 16 * 1024**3,
    )

    assert _use_eager_vllm(selection, {})
    _print_resource_assessment(selection, {})
    assert "Runtime mode: eager execution selected" in capsys.readouterr().out
    assert (
        _runtime_command(
            Path("/opt/heartwood-vllm/bin/heartwood-vllm"),
            selection.model_root,
            selection,
            enforce_eager=True,
        )[-1]
        == "--enforce-eager"
    )

    monkeypatch.setattr(
        "heartwood.cli._launch._available_gpu_memory_bytes",
        lambda _env, **_kwargs: 48 * 1024**3,
    )
    assert not _use_eager_vllm(selection, {})


def test_available_system_memory_honors_cgroup_v1_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = {
        "/proc/meminfo": "MemAvailable: 67108864 kB\n",
        "/sys/fs/cgroup/memory/memory.limit_in_bytes": str(32 * 1024**3),
        "/sys/fs/cgroup/memory/memory.usage_in_bytes": str(8 * 1024**3),
    }

    def read_text(path: Path, **_kwargs: object) -> str:
        try:
            return content[str(path)]
        except KeyError as error:
            raise OSError from error

    monkeypatch.setattr(Path, "read_text", read_text)

    assert _available_system_memory_bytes() == 24 * 1024**3


def test_available_gpu_memory_uses_least_available_visible_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "heartwood.cli._launch.shutil.which",
        lambda name, **_kwargs: f"/usr/bin/{name}",
    )
    monkeypatch.setattr(
        "heartwood.cli._launch.subprocess.run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout="16384\n8192\n",
        ),
    )

    assert _available_gpu_memory_bytes({"PATH": "/usr/bin"}) == 16 * 1024**3


def test_runtime_resolution_and_gguf_directory_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llama = tmp_path / "llama-server"
    llama.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        "heartwood.cli._launch.shutil.which",
        lambda name: str(llama) if name == "llama-server" else None,
    )
    assert _resolve_runtime_executable("llama-cpp") == llama

    version_root = tmp_path / "runtime"
    vllm = version_root / "vllm" / "bin" / "heartwood-vllm"
    vllm.parent.mkdir(parents=True)
    vllm.write_text("", encoding="utf-8")
    monkeypatch.setattr("heartwood.cli._launch._native_version_root", lambda: version_root)
    assert _resolve_runtime_executable("vllm") == vllm

    model_dir = tmp_path / "gguf"
    model_dir.mkdir()
    model = model_dir / "model.gguf"
    model.write_bytes(b"synthetic")
    assert _gguf_file(model_dir) == model
    (model_dir / "second.gguf").write_bytes(b"synthetic")
    with pytest.raises(ValueError, match="exactly one GGUF"):
        _gguf_file(model_dir)
    with pytest.raises(ValueError, match="exactly one GGUF"):
        _gguf_file(tmp_path / "missing")


def test_snapshot_verification_rejects_unsafe_modified_and_unlisted_content(
    tmp_path: Path,
) -> None:
    model = tmp_path / "model"
    model.mkdir()
    (model / "SHA256SUMS").write_text("not-a-manifest\n", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid SHA256SUMS"):
        verify_snapshot(model)
    digest = hashlib.sha256(b"expected").hexdigest()
    (model / "SHA256SUMS").write_text(f"{digest}  ../weights\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unsafe SHA256SUMS"):
        verify_snapshot(model)
    (model / "weights").write_bytes(b"modified")
    (model / "SHA256SUMS").write_text(f"{digest}  weights\n", encoding="utf-8")
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        verify_snapshot(model)
    _snapshot(tmp_path / "valid")
    (tmp_path / "valid" / "unlisted").write_text("value", encoding="utf-8")
    with pytest.raises(ValueError, match="unlisted"):
        verify_snapshot(tmp_path / "valid")


def test_runtime_environment_scrubs_credentials_and_legacy_path_controls() -> None:
    runtime_env = _runtime_environment(
        {
            "PATH": "/usr/bin",
            "CUDA_VISIBLE_DEVICES": "0",
            "GOOGLE_PROJECT": "terra-project",
            "CLUSTER_NAME": "saturn-runtime",
            "HEARTWOOD_GPU_RUNTIME": "vllm",
            "HEARTWOOD_PLATFORM_HOME": "/home/jupyter",
            "OPENAI_API_KEY": "secret",
            "STANFORD_AI_API_KEY": "secret",
            "HEARTWOOD_HOME": "/legacy",
            "HEARTWOOD_WORKSPACE": "/legacy/sessions",
            "HEARTWOOD_VLLM_EXECUTABLE": "/unreviewed/vllm",
        }
    )
    assert runtime_env["CUDA_VISIBLE_DEVICES"] == "0"
    assert runtime_env["GOOGLE_PROJECT"] == "terra-project"
    assert runtime_env["CLUSTER_NAME"] == "saturn-runtime"
    assert runtime_env["HEARTWOOD_GPU_RUNTIME"] == "vllm"
    assert runtime_env["HEARTWOOD_PLATFORM_HOME"] == "/home/jupyter"
    assert "VLLM_USE_FLASHINFER_SAMPLER" not in runtime_env
    assert not any("API_KEY" in name for name in runtime_env)
    assert "HEARTWOOD_HOME" not in runtime_env
    assert "HEARTWOOD_WORKSPACE" not in runtime_env
    assert "HEARTWOOD_VLLM_EXECUTABLE" not in runtime_env


def test_reentry_command_contains_no_storage_or_runtime_paths(tmp_path: Path) -> None:
    command = _reentry_command(_options(tmp_path, plain=False))
    assert "--inside-allocation" in command
    assert "--plain" not in command
    assert "--web" not in command
    assert "--workspace" not in command
    assert "--state-root" not in command
    assert "--model-root" not in command
    assert "--vllm-executable" not in command


def test_task_handoff_uses_a_private_project_file_without_exposing_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = _options(tmp_path, prompt="synthetic task with private details")
    submitted: list[tuple[str, ...]] = []

    def run_successfully(command: Sequence[str]) -> int:
        submitted.append(tuple(command))
        prompt_index = command.index("--prompt-file")
        prompt_file = Path(command[prompt_index + 1])
        assert prompt_file.parent == tmp_path / ".heartwood" / "runtime"
        assert prompt_file.read_text(encoding="utf-8") == options.prompt
        assert prompt_file.stat().st_mode & 0o777 == 0o600
        return 0

    monkeypatch.setattr(
        "heartwood.cli._launch.discover_slurm_gpu_partitions",
        lambda _env: (_partition(),),
    )

    assert (
        run_launch(
            options,
            env={"HEARTWOOD_PLATFORM": "carina"},
            input_fn=lambda _prompt: "y",
            run_fn=run_successfully,
        )
        == 0
    )
    assert len(submitted) == 1
    rendered = " ".join(submitted[0])
    assert "synthetic task with private details" not in rendered
    assert not tuple((tmp_path / ".heartwood" / "runtime").glob("pending-prompt.*"))


def test_interaction_interrupt_exits_without_a_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def interrupted(*_args: object, **_kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("heartwood.cli._launch.subprocess.run", interrupted)

    assert _run_interaction(("heartwood",), env={}, cwd=tmp_path) == 130
    assert capsys.readouterr().out == "\nHeartwood stopped.\n"


def test_web_reentry_preserves_only_interface_options(tmp_path: Path) -> None:
    command = _reentry_command(
        _options(
            tmp_path,
            web=True,
            web_host="0.0.0.0",
            web_port=9876,
        )
    )

    assert command[-11:] == (
        "--interface",
        "web",
        "--host",
        "0.0.0.0",
        "--port",
        "9876",
        "runtime",
        "start",
        "--inside-allocation",
        "--startup-timeout",
        "600",
    )

    interaction, label = _interaction_command(
        _options(tmp_path, web=True, web_host="0.0.0.0", web_port=9876)
    )
    assert interaction[-6:] == [
        "gateway",
        "serve",
        "--host",
        "0.0.0.0",
        "--port",
        "9876",
    ]
    assert label == "Open the web interface on 0.0.0.0:9876"


def test_runtime_readiness_accepts_requested_model_and_stops_after_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ExitedProcess:
        def poll(self) -> int:
            return 1

    assert not _wait_for_runtime(
        cast(subprocess.Popen[str], ExitedProcess()),
        model_id="test-model",
        timeout=0.1,
    )

    class RunningProcess:
        def poll(self) -> None:
            return None

    class Response(io.BytesIO):
        status = 200

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            self.close()

    class Opener:
        def open(self, *_args: object, **_kwargs: object) -> Response:
            return Response(json.dumps({"data": [{"id": "test-model"}]}).encode())

    monkeypatch.setattr(
        "heartwood.cli._launch.urllib.request.build_opener",
        lambda _handler: Opener(),
    )
    assert _wait_for_runtime(
        cast(subprocess.Popen[str], RunningProcess()),
        model_id="test-model",
        timeout=0.1,
    )


def test_runtime_readiness_reports_progress_and_rejects_malformed_catalog(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class RunningProcess:
        def poll(self) -> None:
            return None

    class FailingOpener:
        def open(self, *_args: object, **_kwargs: object) -> object:
            raise OSError("not ready")

    times = iter((0.0, 0.0, 0.0, 16.0, 31.0))
    monkeypatch.setattr("heartwood.cli._launch.time.monotonic", lambda: next(times))
    monkeypatch.setattr("heartwood.cli._launch.time.sleep", lambda _seconds: None)
    monkeypatch.setattr(
        "heartwood.cli._launch.urllib.request.build_opener",
        lambda _handler: FailingOpener(),
    )

    assert not _wait_for_runtime(
        cast(subprocess.Popen[str], RunningProcess()),
        model_id="test-model",
        timeout=30,
    )
    assert (
        "Still starting the Heartwood-managed model (16 seconds elapsed)" in capsys.readouterr().out
    )
    assert not _catalog_contains_model([], "test-model")
    assert not _catalog_contains_model({"data": "invalid"}, "test-model")
    assert not _catalog_contains_model({"data": ["invalid"]}, "test-model")


def test_setup_helper_runs_inside_project_without_path_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[tuple[str, ...], Path | None, dict[str, str]]] = []

    def completed(
        command: Sequence[str], **kwargs: object
    ) -> subprocess.CompletedProcess[Sequence[str]]:
        observed.append(
            (
                tuple(command),
                cast(Path | None, kwargs.get("cwd")),
                dict(cast(dict[str, str], kwargs.get("env"))),
            )
        )
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("heartwood.cli._launch.subprocess.run", completed)
    assert _ensure_setup(_options(tmp_path), {"HEARTWOOD_PLATFORM": "terra"}) == 0
    assert any("setup" in command for command, _cwd, _env in observed)
    assert any("doctor" in command for command, _cwd, _env in observed)
    assert all(cwd == tmp_path for _command, cwd, _env in observed)
    assert all("--workspace" not in command for command, _cwd, _env in observed)
    assert all(env["HEARTWOOD_PLATFORM"] == "terra" for _command, _cwd, env in observed)
    assert all(
        select_platform_adapter(env).adapter_id == "terra" for _command, _cwd, env in observed
    )


def test_setup_helper_propagates_setup_failure_and_missing_selection(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _ensure_setup(_options(tmp_path / "missing", selected=False), {}) == 64
    assert "No Heartwood-managed model is selected for setup" in capsys.readouterr().out

    observed: list[tuple[str, ...]] = []

    def failed(command: Sequence[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        observed.append(tuple(command))
        return subprocess.CompletedProcess(command, 7)

    monkeypatch.setattr("heartwood.cli._launch.subprocess.run", failed)
    assert _ensure_setup(_options(tmp_path / "failure"), {}) == 7
    assert len(observed) == 1
    assert "setup" in observed[0]


def test_effective_context_is_persisted_in_the_shared_local_profile(tmp_path: Path) -> None:
    options = _options(tmp_path)
    adapter = select_platform_adapter({"HEARTWOOD_PLATFORM": "generic"})
    store = ProjectConfigStore(
        options.project,
        ProjectConfig(
            platform_id=adapter.adapter_id,
            policy=adapter.default_policy_profile(),
        ),
    )
    profile = model_profile_from_preset("heartwood-managed", "heartwood-managed-model")
    settings = ModelSettings().with_profile(profile).selecting(profile.profile_id)
    store.select_model_source("heartwood", settings)

    _persist_effective_context(store, 65_536)

    persisted = store.load().model_settings.profile()
    assert persisted.max_input_tokens == 61_440
    assert persisted.max_output_tokens == 4_096


def test_partition_discovery_and_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "heartwood.cli._launch.discover_slurm_gpu_partitions",
        lambda _env: (
            _partition("dev", default=True),
            _partition("long", default=False),
        ),
    )
    plan = build_launch_plan(
        _options(tmp_path, partition=None),
        {"HEARTWOOD_PLATFORM": "carina"},
    )
    assert "--partition=dev" in plan.allocation_command

    monkeypatch.setattr(
        "heartwood.cli._launch.discover_slurm_gpu_partitions",
        lambda _env: (
            _partition("dev", default=True),
            _partition("normal", default=False),
        ),
    )
    assert (
        run_launch(
            _options(
                tmp_path,
                partition="gpu",
                dry_run=True,
                prompt="private task that must be removed",
            ),
            env={"HEARTWOOD_PLATFORM": "carina"},
        )
        == 64
    )
    assert "choose one of: dev, normal" in capsys.readouterr().out
    assert not tuple((tmp_path / ".heartwood" / "runtime").glob("pending-prompt.*"))


def test_partition_discovery_handles_ambiguous_and_unavailable_schedulers(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "heartwood.cli._launch.discover_slurm_gpu_partitions",
        lambda _env: (
            _partition("dev", default=False),
            _partition("normal", default=False),
        ),
    )
    assert (
        run_launch(
            _options(tmp_path / "ambiguous", partition=None, dry_run=True),
            env={"HEARTWOOD_PLATFORM": "carina"},
        )
        == 64
    )
    assert "no default GPU partition" in capsys.readouterr().out


def test_vllm_preflight_and_output_helpers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    executable = tmp_path / "bin" / "heartwood-vllm"
    python = executable.with_name("python")
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\n", encoding="utf-8")
    python.chmod(0o755)
    observed: list[tuple[str, ...]] = []

    def completed(command: Sequence[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        observed.append(tuple(command))
        if command == (str(executable), "__heartwood_verify_runtime__"):
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=(
                    "Transformers 5.5.0 integration and "
                    "vLLM GHSA-8fr4-5q9j-m8gm and "
                    "xgrammar GHSA-7rgv-gqhr-fxg3 fixes verified\n"
                ),
            )
        return subprocess.CompletedProcess(command, 0, stdout="0.25.1+cu129 2.11.0+cu129 12.9\n")

    monkeypatch.setattr("heartwood.cli._launch.subprocess.run", completed)
    assert _preflight_vllm(executable, {"PATH": "/usr/bin"}) is None
    assert "import torch, vllm" in observed[0][-1]
    assert "torch.cuda.init()" in observed[0][-1]
    assert observed[1] == (str(executable), "__heartwood_verify_runtime__")
    assert _format_bytes(1024) == "1.0 KiB"

    log = tmp_path / "runtime.log"
    log.write_text("RuntimeError: missing kernel\n", encoding="utf-8")
    _print_runtime_failure(log, 17)
    output = capsys.readouterr().out
    assert "status 17" in output
    assert "RuntimeError: missing kernel" in output

    monkeypatch.setattr(
        "heartwood.cli._launch.subprocess.run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 23),
    )
    assert _run_command(("synthetic-command",)) == 23


def test_vllm_preflight_reports_missing_and_failed_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "bin" / "vllm"
    assert "runtime Python is unavailable" in str(_preflight_vllm(executable, {}))

    python = executable.with_name("python")
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\n", encoding="utf-8")
    python.chmod(0o755)
    monkeypatch.setattr(
        "heartwood.cli._launch.subprocess.run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("cannot execute")),
    )
    assert _preflight_vllm(executable, {}) == "cannot execute"

    monkeypatch.setattr(
        "heartwood.cli._launch.subprocess.run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr="first line\nmissing runtime library\n",
        ),
    )
    assert _preflight_vllm(executable, {}) == "missing runtime library"

    monkeypatch.setattr(
        "heartwood.cli._launch.subprocess.run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr=(
                "Traceback (most recent call last):\n"
                "AssertionError: CUDA is unavailable to PyTorch 2.11.0 (built for CUDA 12.9)\n"
            ),
        ),
    )
    assert _preflight_vllm(executable, {}) == (
        "AssertionError: CUDA is unavailable to PyTorch 2.11.0 (built for CUDA 12.9)"
    )

    monkeypatch.setattr(
        "heartwood.cli._launch.subprocess.run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command, 0, stdout="0.10.1.1+cu118 2.7.1+cu118 11.8\n"
        ),
    )
    assert "must be Heartwood's secured launcher" in str(_preflight_vllm(executable, {}))

    monkeypatch.setattr(
        "heartwood.cli._launch.subprocess.run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command, 0, stdout="0.11.1 2.9.0 12.8\n"
        ),
    )
    assert _preflight_vllm(executable, {}) is None

    monkeypatch.setattr(
        "heartwood.cli._launch.subprocess.run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command, 0, stdout="unknown-version 2.9.0 12.8\n"
        ),
    )
    assert "must be Heartwood's secured launcher" in str(_preflight_vllm(executable, {}))

    wrapper = executable.with_name("heartwood-vllm")

    def failed_compatibility(
        command: Sequence[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if command == (str(wrapper), "__heartwood_verify_runtime__"):
            return subprocess.CompletedProcess(
                command,
                1,
                stdout="",
                stderr="first line\nincompatible model configuration\n",
            )
        return subprocess.CompletedProcess(command, 0, stdout="0.25.1+cu129 2.11.0+cu129 12.9\n")

    monkeypatch.setattr("heartwood.cli._launch.subprocess.run", failed_compatibility)
    assert _preflight_vllm(wrapper, {}) == "incompatible model configuration"

    def timed_out_compatibility(
        command: Sequence[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if command == (str(wrapper), "__heartwood_verify_runtime__"):
            raise subprocess.TimeoutExpired(command, 60)
        return subprocess.CompletedProcess(command, 0, stdout="0.25.1+cu129 2.11.0+cu129 12.9\n")

    monkeypatch.setattr("heartwood.cli._launch.subprocess.run", timed_out_compatibility)
    assert "timed out after 60 seconds" in str(_preflight_vllm(wrapper, {}))


def test_model_staging_helpers_cover_files_directories_and_sizes(tmp_path: Path) -> None:
    source_file = tmp_path / "model.gguf"
    source_file.write_bytes(b"1234")
    selection = LocalRuntimeSelection(
        artifact_id="test-model",
        model_root=source_file,
        runtime="llama-cpp",
        model_id="test-model",
        size_bytes=4,
        artifact_sha256=hashlib.sha256(b"1234").hexdigest(),
        context_window=32_768,
        maximum_context_window=32_768,
        tier="standard",
        precision="GGUF synthetic",
        qualification="qualified",
        minimum_gpu_count=0,
        minimum_gpu_memory_bytes=0,
        recommended_ram_bytes=16 * 1024**3,
        recommended_disk_bytes=32 * 1024**3,
        tool_call_parser=None,
        tensor_parallel_size=1,
        startup_seconds_min=1,
        startup_seconds_max=2,
        catalog_source="user-selected",
    )
    _verify_local_model(selection)
    file_destination = tmp_path / "file-stage"
    file_destination.mkdir()
    staged_file = _stage_model(source_file, file_destination)
    assert staged_file.read_bytes() == b"1234"
    assert _model_size(source_file) == 4
    staged_file.write_bytes(b"5678")
    with pytest.raises(ValueError, match="checksum"):
        _verify_local_model(selection, model_root=staged_file)

    source_directory = tmp_path / "snapshot"
    _snapshot(source_directory)
    directory_destination = tmp_path / "directory-stage"
    directory_destination.mkdir()
    assert _stage_model(source_directory, directory_destination) == directory_destination
    assert (directory_destination / "weights.safetensors").read_bytes() == b"synthetic"
    assert _model_size(source_directory) > 0


def test_imported_vllm_snapshot_passes_the_launch_integrity_gate(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    snapshot.joinpath("config.json").write_text(
        json.dumps({"architectures": ["SyntheticForCausalLM"]}),
        encoding="utf-8",
    )
    snapshot.joinpath("model.safetensors").write_bytes(b"synthetic")
    project = ProjectContext(project_root)
    gateway = SessionGateway(project=project, env={})

    gateway.import_local_model(
        snapshot,
        source_repository="Qwen/Qwen2.5-Coder-7B-Instruct",
        source_revision="1" * 40,
        license_posture="Apache-2.0",
        context_window=32_768,
    )
    selection = _local_model_selection(project, {})

    assert selection is not None
    assert selection.runtime == "vllm"
    _verify_local_model(selection)


def test_model_staging_progress_waits_for_a_stable_eta(
    capsys: pytest.CaptureFixture[str],
) -> None:
    total = 14 * 1024 * 1024 * 1024

    _print_copy_progress(1536, total, 0.001)
    _print_copy_progress(128 * 1024 * 1024, total, 8)
    _print_copy_progress(total, total, 20)

    lines = capsys.readouterr().out.splitlines()
    assert "calculating remaining time" in lines[0]
    assert "seconds remaining" not in lines[0]
    assert "seconds remaining" in lines[1]
    assert lines[2].endswith("complete)")
