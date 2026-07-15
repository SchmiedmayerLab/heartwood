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
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from heartwood.adapters.platform import select_platform_adapter
from heartwood.cli._launch import (
    LaunchOptions,
    LocalRuntimeSelection,
    _available_system_memory_bytes,
    _catalog_contains_model,
    _discover_slurm_gpu_partitions,
    _ensure_setup,
    _format_bytes,
    _gguf_file,
    _interaction_command,
    _model_size,
    _preflight_vllm,
    _print_resource_assessment,
    _print_runtime_failure,
    _reentry_command,
    _resolve_runtime_executable,
    _run_command,
    _runtime_command,
    _runtime_environment,
    _stage_model,
    _verify_local_model,
    _wait_for_runtime,
    build_launch_plan,
    run_launch,
)
from heartwood.cli._model_snapshot import verify_snapshot
from heartwood.gateway import ProjectConfig, ProjectConfigStore, ProjectContext


def _options(
    tmp_path: Path,
    *,
    selected: bool = True,
    runtime: str = "vllm",
    **overrides: object,
) -> LaunchOptions:
    tmp_path.mkdir(parents=True, exist_ok=True)
    project = ProjectContext(tmp_path)
    project.initialize()
    if selected:
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
        )
    values: dict[str, object] = {
        "project": project,
        "session_id": "launch-test",
        "partition": "gpu",
        "gpus": 1,
        "cpus": 8,
        "memory": "64G",
        "time_limit": "02:00:00",
        "dry_run": False,
        "no_allocate": False,
        "yes_request_allocation": False,
        "inside_allocation": False,
        "plain": True,
        "web": False,
        "web_host": "127.0.0.1",
        "web_port": 8767,
        "startup_timeout": 600,
    }
    values.update(overrides)
    return LaunchOptions(**values)  # type: ignore[arg-type]


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
    monkeypatch.setattr("heartwood.cli._launch._discover_slurm_gpu_partitions", lambda _env: ())
    options = _options(tmp_path)
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
    assert plan.context_window == 16_384
    assert "Context: 16,384 tokens" in plan.format()
    assert plan.allocation_command[:3] == ("srun", "--pty", "--partition=gpu")
    assert f"--chdir={tmp_path}" in plan.allocation_command
    export = next(item for item in plan.allocation_command if item.startswith("--export="))
    assert export == "--export=PATH,HOME,HEARTWOOD_PLATFORM=carina"
    assert "OPENAI_API_KEY" not in export
    assert "--workspace" not in plan.allocation_command
    assert "--model-root" not in plan.allocation_command


def test_launch_requires_consent_and_honors_dry_run_and_no_allocate(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "heartwood.cli._launch._discover_slurm_gpu_partitions",
        lambda _env: (("gpu", True),),
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
        "heartwood.cli._launch._discover_slurm_gpu_partitions",
        lambda _env: (("gpu", True),),
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
    assert "No local model is selected" in capsys.readouterr().out
    assert run_launch(_options(tmp_path), env=env) == 66
    _snapshot(tmp_path / ".heartwood" / "models" / "model")
    monkeypatch.setattr(
        "heartwood.cli._launch._resolve_runtime_executable",
        lambda _runtime: tmp_path / "missing-vllm",
    )
    assert run_launch(_options(tmp_path), env=env) == 69
    assert "vLLM executable is unavailable" in capsys.readouterr().out


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
    options = _options(tmp_path)
    model = tmp_path / ".heartwood" / "models" / "model"
    _snapshot(model)
    executable = tmp_path / "vllm"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    observed_processes: list[tuple[str, ...]] = []
    observed_runs: list[tuple[tuple[str, ...], Path | None]] = []

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
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(
        "heartwood.cli._launch._resolve_runtime_executable", lambda _kind: executable
    )
    monkeypatch.setattr("heartwood.cli._launch._preflight_vllm", lambda *_args: None)
    monkeypatch.setattr("heartwood.cli._launch.subprocess.Popen", FakeProcess)
    monkeypatch.setattr("heartwood.cli._launch._wait_for_runtime", lambda *_args, **_kwargs: True)
    monkeypatch.setattr("heartwood.cli._launch._ensure_setup", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr("heartwood.cli._launch.subprocess.run", completed)

    assert run_launch(options, env={"HEARTWOOD_PLATFORM": "generic"}) == 0
    assert observed_processes[0][0] == str(executable)
    assert "--enable-auto-tool-choice" in observed_processes[0]
    assert observed_runs[-1][1] == tmp_path
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


def test_llama_cpp_command_uses_the_selected_gguf(tmp_path: Path) -> None:
    model = tmp_path / "model.gguf"
    model.write_bytes(b"synthetic")
    command = _runtime_command(
        "llama-cpp",
        Path("/opt/llama.cpp/llama-server"),
        model,
        "heartwood-local-model",
        32_768,
    )
    assert command[:3] == (
        "/opt/llama.cpp/llama-server",
        "--model",
        str(model),
    )
    assert "--alias" in command
    context_index = command.index("--ctx-size")
    assert command[context_index + 1] == "32768"


def test_resource_assessment_reports_context_and_memory_warnings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    selection = LocalRuntimeSelection(
        model_root=tmp_path / "model",
        runtime="vllm",
        model_id="test-model",
        size_bytes=10 * 1024**3,
        artifact_sha256=None,
        context_window=32_768,
    )
    monkeypatch.setattr(
        "heartwood.cli._launch._available_system_memory_bytes",
        lambda: 16 * 1024**3,
    )
    monkeypatch.setattr(
        "heartwood.cli._launch._available_gpu_memory_bytes",
        lambda _env: 12 * 1024**3,
    )

    _print_resource_assessment(selection, {"PATH": "/usr/bin"})

    output = capsys.readouterr().out
    assert "Context window: 32,768 tokens" in output
    assert "Warning: RAM may be insufficient" in output
    assert "Warning: GPU memory may be insufficient" in output


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
    vllm = version_root / "vllm" / "bin" / "vllm"
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
            "OPENAI_API_KEY": "secret",
            "STANFORD_AI_API_KEY": "secret",
            "HEARTWOOD_HOME": "/legacy",
            "HEARTWOOD_WORKSPACE": "/legacy/sessions",
            "HEARTWOOD_VLLM_EXECUTABLE": "/unreviewed/vllm",
        }
    )
    assert runtime_env["CUDA_VISIBLE_DEVICES"] == "0"
    assert runtime_env["VLLM_USE_FLASHINFER_SAMPLER"] == "0"
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


def test_web_reentry_preserves_only_interface_options(tmp_path: Path) -> None:
    command = _reentry_command(
        _options(
            tmp_path,
            web=True,
            web_host="0.0.0.0",
            web_port=9876,
        )
    )

    assert command[-7:] == (
        "--web",
        "--host",
        "0.0.0.0",
        "--port",
        "9876",
        "--startup-timeout",
        "600",
    )

    interaction, label = _interaction_command(
        _options(tmp_path, web=True, web_host="0.0.0.0", web_port=9876)
    )
    assert interaction[-5:] == ["serve", "--host", "0.0.0.0", "--port", "9876"]
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
    assert "Still starting the local model server (16 seconds elapsed)" in capsys.readouterr().out
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
    assert "No local model is selected for setup" in capsys.readouterr().out

    observed: list[tuple[str, ...]] = []

    def failed(command: Sequence[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        observed.append(tuple(command))
        return subprocess.CompletedProcess(command, 7)

    monkeypatch.setattr("heartwood.cli._launch.subprocess.run", failed)
    assert _ensure_setup(_options(tmp_path / "failure"), {}) == 7
    assert len(observed) == 1
    assert "setup" in observed[0]


def test_partition_discovery_and_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "heartwood.cli._launch.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            (), 0, stdout="dev*|gpu:nvidia_l40s:8|up\nlong|gpu:nvidia_l40s:8|up\n"
        ),
    )
    assert _discover_slurm_gpu_partitions({"PATH": "/usr/bin"}) == (
        ("dev", True),
        ("long", False),
    )
    plan = build_launch_plan(
        _options(tmp_path, partition=None),
        {"HEARTWOOD_PLATFORM": "carina"},
    )
    assert "--partition=dev" in plan.allocation_command

    monkeypatch.setattr(
        "heartwood.cli._launch._discover_slurm_gpu_partitions",
        lambda _env: (("dev", True), ("normal", False)),
    )
    assert (
        run_launch(
            _options(tmp_path, partition="gpu", dry_run=True),
            env={"HEARTWOOD_PLATFORM": "carina"},
        )
        == 64
    )
    assert "choose one of: dev, normal" in capsys.readouterr().out


def test_partition_discovery_handles_ambiguous_and_unavailable_schedulers(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "heartwood.cli._launch._discover_slurm_gpu_partitions",
        lambda _env: (("dev", False), ("normal", False)),
    )
    assert (
        run_launch(
            _options(tmp_path / "ambiguous", partition=None, dry_run=True),
            env={"HEARTWOOD_PLATFORM": "carina"},
        )
        == 64
    )
    assert "no default GPU partition" in capsys.readouterr().out

    monkeypatch.setattr(
        "heartwood.cli._launch.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess((), 1, stdout=""),
    )
    assert _discover_slurm_gpu_partitions({}) == ()
    monkeypatch.setattr(
        "heartwood.cli._launch.subprocess.run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("missing sinfo")),
    )
    assert _discover_slurm_gpu_partitions({}) == ()


def test_vllm_preflight_and_output_helpers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    executable = tmp_path / "bin" / "vllm"
    python = executable.with_name("python")
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\n", encoding="utf-8")
    python.chmod(0o755)
    observed: list[str] = []

    def completed(command: Sequence[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        observed.extend(command)
        return subprocess.CompletedProcess(command, 0, stdout="0.10.1.1 2.7.1 11.8\n")

    monkeypatch.setattr("heartwood.cli._launch.subprocess.run", completed)
    assert _preflight_vllm(executable, {"PATH": "/usr/bin"}) is None
    assert "import torch, vllm" in observed[-1]
    assert "torch.cuda.init()" in observed[-1]
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
                "AssertionError: CUDA is unavailable to PyTorch 2.7.1 (built for CUDA 11.8)\n"
            ),
        ),
    )
    assert _preflight_vllm(executable, {}) == (
        "AssertionError: CUDA is unavailable to PyTorch 2.7.1 (built for CUDA 11.8)"
    )


def test_model_staging_helpers_cover_files_directories_and_sizes(tmp_path: Path) -> None:
    source_file = tmp_path / "model.gguf"
    source_file.write_bytes(b"1234")
    selection = LocalRuntimeSelection(
        model_root=source_file,
        runtime="llama-cpp",
        model_id="test-model",
        size_bytes=4,
        artifact_sha256=hashlib.sha256(b"1234").hexdigest(),
        context_window=32_768,
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
