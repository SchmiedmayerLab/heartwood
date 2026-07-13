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

import pytest

from heartwood.cli._launch import (
    LaunchOptions,
    _discover_slurm_gpu_partitions,
    _ensure_setup,
    _format_bytes,
    _preflight_vllm,
    _print_runtime_failure,
    _reentry_command,
    _resolve_vllm,
    _resolve_vllm_python,
    _run_command,
    _runtime_environment,
    _wait_for_runtime,
    build_launch_plan,
    run_launch,
)
from heartwood.cli._model_snapshot import verify_snapshot


def _options(tmp_path: Path, **overrides: object) -> LaunchOptions:
    values: dict[str, object] = {
        "workspace": tmp_path / "state" / "sessions",
        "session_id": "launch-test",
        "model_root": tmp_path / "model",
        "state_root": tmp_path / "state",
        "environment_root": tmp_path / "environment",
        "vllm_executable": tmp_path / "vllm",
        "model_id": "test-model",
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
        "startup_timeout": 600,
    }
    values.update(overrides)
    return LaunchOptions(**values)  # type: ignore[arg-type]


def _model(root: Path) -> None:
    root.mkdir()
    weights = root / "weights.safetensors"
    weights.write_bytes(b"synthetic")
    digest = hashlib.sha256(weights.read_bytes()).hexdigest()
    (root / "SHA256SUMS").write_text(f"{digest}  weights.safetensors\n", encoding="utf-8")


def test_carina_launch_plan_requests_reviewable_gpu_allocation(tmp_path: Path) -> None:
    options = _options(tmp_path)
    plan = build_launch_plan(options, {"HEARTWOOD_PLATFORM": "carina"})

    assert plan.platform_id == "carina"
    assert plan.allocation_required
    assert plan.allocation_command[:3] == ("srun", "--pty", "--partition=gpu")
    assert "--gres=gpu:1" in plan.allocation_command
    assert "--inside-allocation" in plan.allocation_command
    assert "--export=ALL,HEARTWOOD_PLATFORM=carina" in plan.allocation_command
    assert "Slurm allocation required" in plan.format()


def test_carina_launch_requires_explicit_allocation_consent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    called = False

    def runner(_command: object) -> int:
        nonlocal called
        called = True
        return 0

    code = run_launch(
        _options(tmp_path),
        env={"HEARTWOOD_PLATFORM": "carina"},
        input_fn=lambda _prompt: "n",
        run_fn=runner,
    )

    assert code == 1
    assert not called
    assert "Allocation cancelled" in capsys.readouterr().out


def test_carina_launch_defaults_to_no_when_consent_input_closes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def closed(_prompt: str) -> str:
        raise EOFError

    assert (
        run_launch(
            _options(tmp_path),
            env={"HEARTWOOD_PLATFORM": "carina"},
            input_fn=closed,
        )
        == 1
    )
    assert "Allocation cancelled" in capsys.readouterr().out


def test_carina_launch_submits_exact_plan_after_consent(tmp_path: Path) -> None:
    observed: list[str] = []

    def runner(command: object) -> int:
        observed.extend(command)  # type: ignore[arg-type]
        return 23

    code = run_launch(
        _options(tmp_path, yes_request_allocation=True),
        env={"HEARTWOOD_PLATFORM": "carina"},
        run_fn=runner,
    )

    assert code == 23
    assert observed[0] == "srun"
    assert "--cpus-per-task=8" in observed


def test_launch_dry_run_and_no_allocate_never_submit(tmp_path: Path) -> None:
    def unexpected(_command: object) -> int:
        pytest.fail("scheduler must not be called")

    assert (
        run_launch(
            _options(tmp_path, dry_run=True),
            env={"HEARTWOOD_PLATFORM": "carina"},
            run_fn=unexpected,
        )
        == 0
    )
    assert (
        run_launch(
            _options(tmp_path, no_allocate=True),
            env={"HEARTWOOD_PLATFORM": "carina"},
            run_fn=unexpected,
        )
        == 1
    )

    terra = build_launch_plan(_options(tmp_path), {"GOOGLE_PROJECT": "synthetic-project"})
    assert terra.platform_id == "terra"
    assert not terra.allocation_required


def test_direct_launch_reports_model_and_runtime_failures(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env = {"HEARTWOOD_PLATFORM": "generic"}
    assert run_launch(_options(tmp_path, model_root=None), env=env) == 64
    assert run_launch(_options(tmp_path), env=env) == 66
    _model(tmp_path / "model")
    assert run_launch(_options(tmp_path), env=env) == 69
    assert "vLLM executable is unavailable" in capsys.readouterr().out


def test_direct_launch_reports_runtime_preflight_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _model(tmp_path / "model")
    executable = tmp_path / "vllm"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setattr(
        "heartwood.cli._launch._preflight_vllm",
        lambda *_args: "TorchCodec could not load FFmpeg",
    )

    assert run_launch(_options(tmp_path), env={"HEARTWOOD_PLATFORM": "generic"}) == 69
    assert "TorchCodec could not load FFmpeg" in capsys.readouterr().out


def test_launch_rejects_an_unavailable_configured_scratch_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _model(tmp_path / "model")
    executable = tmp_path / "vllm"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setattr("heartwood.cli._launch._preflight_vllm", lambda *_args: None)

    assert (
        run_launch(
            _options(tmp_path),
            env={
                "HEARTWOOD_PLATFORM": "generic",
                "LOCAL_SCRATCH_JOB": str(tmp_path / "missing-scratch"),
            },
        )
        == 72
    )
    assert "unavailable or not writable" in capsys.readouterr().out


def test_carina_runtime_requires_job_local_scratch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _model(tmp_path / "model")
    executable = tmp_path / "vllm"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    python = tmp_path / "python"
    python.write_text("#!/bin/sh\n", encoding="utf-8")
    python.chmod(0o755)
    monkeypatch.setattr("heartwood.cli._launch._preflight_vllm", lambda *_args: None)

    code = run_launch(
        _options(tmp_path),
        env={"HEARTWOOD_PLATFORM": "carina", "SLURM_JOB_ID": "synthetic-job"},
    )

    assert code == 72
    assert "does not expose LOCAL_SCRATCH_JOB" in capsys.readouterr().out


def test_launch_rejects_job_scratch_without_model_capacity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _model(tmp_path / "model")
    executable = tmp_path / "vllm"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    python = tmp_path / "python"
    python.write_text("#!/bin/sh\n", encoding="utf-8")
    python.chmod(0o755)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr("heartwood.cli._launch._preflight_vllm", lambda *_args: None)
    monkeypatch.setattr(
        "heartwood.cli._launch.shutil.disk_usage",
        lambda _path: SimpleNamespace(total=1, used=1, free=1),
    )

    code = run_launch(
        _options(tmp_path),
        env={"HEARTWOOD_PLATFORM": "generic", "LOCAL_SCRATCH_JOB": str(scratch)},
    )

    assert code == 73
    assert "Job-local scratch requires" in capsys.readouterr().out


def test_direct_launch_supervises_runtime_and_opens_existing_chat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _model(tmp_path / "model")
    executable = tmp_path / "vllm"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    python = tmp_path / "python"
    python.write_text("#!/bin/sh\n", encoding="utf-8")
    python.chmod(0o755)
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "setup.json").write_text("{}", encoding="utf-8")
    (tmp_path / "state" / "runtime").mkdir()
    runtime_log = tmp_path / "state" / "runtime" / "vllm.log"
    runtime_log.write_text("stale error from a prior launch\n", encoding="utf-8")
    observed: list[tuple[str, ...]] = []

    class FakeProcess:
        def __init__(self, command: object, **_kwargs: object) -> None:
            observed.append(tuple(command))  # type: ignore[arg-type]
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

    monkeypatch.setattr("heartwood.cli._launch.subprocess.Popen", FakeProcess)
    monkeypatch.setattr("heartwood.cli._launch._preflight_vllm", lambda *_args: None)
    monkeypatch.setattr(
        "heartwood.cli._launch._wait_for_runtime", lambda _runtime, *, timeout: timeout == 600
    )
    monkeypatch.setattr(
        "heartwood.cli._launch.subprocess.run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0),
    )

    assert run_launch(_options(tmp_path), env={"HEARTWOOD_PLATFORM": "generic"}) == 0
    assert observed[0][0] == str(executable)
    assert "--enable-auto-tool-choice" in observed[0]
    assert runtime_log.read_text(encoding="utf-8") == ""
    assert not any((tmp_path / "state" / "scratch").iterdir())


def test_model_snapshot_rejects_malformed_unsafe_and_modified_content(tmp_path: Path) -> None:
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
    (model / "SHA256SUMS").write_text(f"{digest}  weights\n{digest}  weights\n", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate SHA256SUMS"):
        verify_snapshot(model)

    (model / "SHA256SUMS").write_text(f"{digest}  weights\n", encoding="utf-8")
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        verify_snapshot(model)


def test_model_snapshot_rejects_unlisted_and_linked_files(tmp_path: Path) -> None:
    model = tmp_path / "model"
    _model(model)
    (model / "unlisted").write_text("value", encoding="utf-8")
    with pytest.raises(ValueError, match="unlisted"):
        verify_snapshot(model)
    (model / "unlisted").unlink()
    (model / "link").symlink_to(model / "weights.safetensors")
    with pytest.raises(ValueError, match="symbolic link"):
        verify_snapshot(model)


def test_launch_runtime_helpers_scrub_credentials_and_resolve_native_vllm(tmp_path: Path) -> None:
    env = {
        "OPENAI_API_KEY": "secret",
        "GH_TOKEN": "secret",
        "STANFORD_AI_API_KEY": "secret",
        "AWS_SECRET_ACCESS_KEY": "secret",
        "CUSTOM_PROVIDER_TOKEN": "secret",
        "PATH": "/usr/bin",
        "CUDA_VISIBLE_DEVICES": "0",
        "HEARTWOOD_NATIVE_ROOT": str(tmp_path),
        "HEARTWOOD_NATIVE_VERSION": "v1",
    }
    runtime_env = _runtime_environment(env)
    assert "OPENAI_API_KEY" not in runtime_env
    assert "GH_TOKEN" not in runtime_env
    assert "STANFORD_AI_API_KEY" not in runtime_env
    assert "AWS_SECRET_ACCESS_KEY" not in runtime_env
    assert "CUSTOM_PROVIDER_TOKEN" not in runtime_env
    assert runtime_env["CUDA_VISIBLE_DEVICES"] == "0"
    assert runtime_env["HEARTWOOD_AGENT_BACKEND"] == "openhands-sdk"
    assert runtime_env["HEARTWOOD_NATIVE_ROOT"] == str(tmp_path)
    assert runtime_env["PATH"].startswith(str(tmp_path / "runtimes" / "v1" / "bootstrap" / "bin"))
    assert runtime_env["VLLM_USE_FLASHINFER_SAMPLER"] == "0"
    assert _resolve_vllm(_options(tmp_path, vllm_executable=None, environment_root=None), env) == (
        tmp_path / "runtimes" / "v1" / "vllm" / "bin" / "vllm"
    )


def test_launch_runtime_helpers_cover_explicit_and_default_paths(tmp_path: Path) -> None:
    options = _options(tmp_path, vllm_executable=None, environment_root=None)
    assert _resolve_vllm(options, {"HEARTWOOD_VLLM_EXECUTABLE": "/runtime/vllm"}) == Path(
        "/runtime/vllm"
    )
    assert _resolve_vllm(options, {}) == Path("/opt/heartwood-vllm/bin/vllm")
    assert _resolve_vllm_python(options, tmp_path / "bin" / "vllm", {}) == (
        tmp_path / "bin" / "python"
    )
    assert _resolve_vllm_python(
        options,
        tmp_path / "bin" / "vllm",
        {"HEARTWOOD_VLLM_PYTHON": "/runtime/python"},
    ) == Path("/runtime/python")
    assert _resolve_vllm_python(
        options,
        tmp_path / "bin" / "vllm",
        {"HEARTWOOD_NATIVE_ROOT": "/native", "HEARTWOOD_NATIVE_VERSION": "1"},
    ) == Path("/native/runtimes/1/vllm/bin/python")

    command = _reentry_command(
        _options(
            tmp_path,
            model_root=None,
            environment_root=None,
            vllm_executable=None,
            plain=False,
        )
    )
    assert "--model-root" not in command
    assert "--environment-root" not in command
    assert "--vllm-executable" not in command
    assert "--plain" not in command


def test_runtime_readiness_stops_when_process_exits() -> None:
    class ExitedProcess:
        def poll(self) -> int:
            return 1

    assert not _wait_for_runtime(ExitedProcess(), timeout=0.1)  # type: ignore[arg-type]


def test_runtime_readiness_accepts_a_nonempty_local_model_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
            return Response(json.dumps({"data": [{"id": "model"}]}).encode())

    def build_opener(handler: object) -> Opener:
        assert vars(handler).get("proxies") == {}
        return Opener()

    monkeypatch.setattr(
        "heartwood.cli._launch.urllib.request.build_opener",
        build_opener,
    )

    assert _wait_for_runtime(RunningProcess(), timeout=0.1)  # type: ignore[arg-type]


def test_setup_helper_invokes_non_interactive_local_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: list[tuple[str, ...]] = []

    def failed_setup(
        command: Sequence[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[Sequence[str]]:
        observed.append(tuple(command))
        return subprocess.CompletedProcess(command, 7)

    monkeypatch.setattr("heartwood.cli._launch.subprocess.run", failed_setup)
    assert _ensure_setup(_options(tmp_path), {"OPENAI_API_KEY": "secret"}) == 7
    assert "--non-interactive" in observed[0]
    assert "--model-source" in observed[0]

    def completed(
        command: Sequence[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[Sequence[str]]:
        observed.append(tuple(command))
        return subprocess.CompletedProcess(command, 0)

    observed.clear()
    monkeypatch.setattr("heartwood.cli._launch.subprocess.run", completed)
    assert _ensure_setup(_options(tmp_path), {"OPENAI_API_KEY": "secret"}) == 0
    assert any("setup" in command for command in observed)
    assert any("doctor" in command for command in observed)

    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "setup.json").write_text("{}", encoding="utf-8")
    observed.clear()
    assert _ensure_setup(_options(tmp_path)) == 0
    assert len(observed) == 1
    assert "doctor" in observed[0]


def test_direct_launch_reports_staged_verification_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _model(tmp_path / "model")
    executable = tmp_path / "vllm"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    calls = 0

    def verifier(_root: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ValueError("copy changed")

    monkeypatch.setattr("heartwood.cli._launch.verify_snapshot", verifier)
    monkeypatch.setattr("heartwood.cli._launch._preflight_vllm", lambda *_args: None)

    assert run_launch(_options(tmp_path), env={"HEARTWOOD_PLATFORM": "generic"}) == 66
    assert "Staged model verification failed: copy changed" in capsys.readouterr().out
    assert not any((tmp_path / "state" / "scratch").iterdir())


def test_carina_discovers_default_gpu_partition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def completed(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            (), 0, stdout="dev*|gpu:nvidia_l40s:8|up\nlong|gpu:nvidia_l40s:8|up\n"
        )

    monkeypatch.setattr("heartwood.cli._launch.subprocess.run", completed)

    assert _discover_slurm_gpu_partitions({"PATH": "/usr/bin"}) == (
        ("dev", True),
        ("long", False),
    )
    plan = build_launch_plan(_options(tmp_path, partition=None), {"HEARTWOOD_PLATFORM": "carina"})
    assert "--partition=dev" in plan.allocation_command


def test_carina_reports_invalid_explicit_partition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "heartwood.cli._launch._discover_slurm_gpu_partitions",
        lambda _env: (("dev", True), ("normal", False)),
    )

    code = run_launch(
        _options(tmp_path, partition="gpu", dry_run=True),
        env={"HEARTWOOD_PLATFORM": "carina"},
    )

    assert code == 64
    assert "choose one of: dev, normal" in capsys.readouterr().out


def test_partition_discovery_handles_scheduler_errors_and_irrelevant_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess((), 1, stdout="")

    monkeypatch.setattr("heartwood.cli._launch.subprocess.run", unavailable)
    assert _discover_slurm_gpu_partitions({}) == ()

    def malformed(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            (),
            0,
            stdout=(
                "missing-fields\n"
                "cpu|cpu:64|up\n"
                "down|gpu:nvidia_l40s:8|down\n"
                "dev*|gpu:nvidia_l40s:8|idle\n"
                "dev*|gpu:nvidia_l40s:8|idle\n"
            ),
        )

    monkeypatch.setattr("heartwood.cli._launch.subprocess.run", malformed)
    assert _discover_slurm_gpu_partitions({}) == (("dev", True),)

    monkeypatch.setattr(
        "heartwood.cli._launch.subprocess.run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("missing sinfo")),
    )
    assert _discover_slurm_gpu_partitions({}) == ()


def test_vllm_preflight_imports_real_runtime_modules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "environment" / "vllm" / "bin" / "vllm"
    python = executable.with_name("python")
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\n", encoding="utf-8")
    python.chmod(0o755)
    observed: list[str] = []

    def completed(command: Sequence[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        observed.extend(command)
        return subprocess.CompletedProcess(command, 0, stdout="0.14.0 0.25.0\n")

    monkeypatch.setattr("heartwood.cli._launch.subprocess.run", completed)

    assert _preflight_vllm(_options(tmp_path), executable, {"PATH": "/usr/bin"}) is None
    assert "import torchcodec, vllm" in observed[-1]


def test_vllm_preflight_reports_missing_python_process_errors_and_import_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "vllm"
    options = _options(tmp_path, environment_root=None, vllm_executable=executable)
    assert "runtime Python is unavailable" in str(_preflight_vllm(options, executable, {}))

    python = tmp_path / "python"
    python.write_text("#!/bin/sh\n", encoding="utf-8")
    python.chmod(0o755)
    monkeypatch.setattr(
        "heartwood.cli._launch.subprocess.run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("cannot execute")),
    )
    assert _preflight_vllm(options, executable, {}) == "cannot execute"

    monkeypatch.setattr(
        "heartwood.cli._launch.subprocess.run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command, 1, stdout="", stderr="first line\nmissing FFmpeg\n"
        ),
    )
    assert _preflight_vllm(options, executable, {}) == "missing FFmpeg"


def test_launch_output_helpers_report_units_process_status_and_log_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _format_bytes(1) == "1.0 B"
    assert _format_bytes(1024) == "1.0 KiB"
    assert _format_bytes(1024**5) == "1024.0 TiB"

    log = tmp_path / "vllm.log"
    log.write_text("starting\nRuntimeError: missing kernel\n", encoding="utf-8")
    _print_runtime_failure(log, 17)
    output = capsys.readouterr().out
    assert "status 17" in output
    assert "RuntimeError: missing kernel" in output

    _print_runtime_failure(tmp_path / "missing.log", None)
    assert "Runtime log:" in capsys.readouterr().out

    monkeypatch.setattr(
        "heartwood.cli._launch.subprocess.run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 23),
    )
    assert _run_command(("synthetic-command",)) == 23
