# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from heartwood.cli._launch import (
    LaunchOptions,
    _ensure_setup,
    _resolve_vllm,
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


def test_direct_launch_supervises_runtime_and_opens_existing_chat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _model(tmp_path / "model")
    executable = tmp_path / "vllm"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "setup.json").write_text("{}", encoding="utf-8")
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
    monkeypatch.setattr("heartwood.cli._launch._wait_for_runtime", lambda _runtime: True)
    monkeypatch.setattr(
        "heartwood.cli._launch.subprocess.run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0),
    )

    assert run_launch(_options(tmp_path), env={"HEARTWOOD_PLATFORM": "generic"}) == 0
    assert observed[0][0] == str(executable)
    assert "--enable-auto-tool-choice" in observed[0]
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
    assert runtime_env["PATH"] == "/usr/bin"
    assert runtime_env["CUDA_VISIBLE_DEVICES"] == "0"
    assert runtime_env["HEARTWOOD_AGENT_BACKEND"] == "openhands-sdk"
    assert _resolve_vllm(_options(tmp_path, vllm_executable=None, environment_root=None), env) == (
        tmp_path / "runtimes" / "v1" / "vllm" / "bin" / "vllm"
    )


def test_runtime_readiness_stops_when_process_exits() -> None:
    class ExitedProcess:
        def poll(self) -> int:
            return 1

    assert not _wait_for_runtime(ExitedProcess(), timeout=0.1)  # type: ignore[arg-type]


def test_setup_helper_invokes_non_interactive_local_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: list[str] = []

    def completed(
        command: Sequence[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[Sequence[str]]:
        observed.extend(command)
        return subprocess.CompletedProcess(command, 7)

    monkeypatch.setattr("heartwood.cli._launch.subprocess.run", completed)
    assert _ensure_setup(_options(tmp_path), {"OPENAI_API_KEY": "secret"}) == 7
    assert "--non-interactive" in observed
    assert "--model-source" in observed

    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "setup.json").write_text("{}", encoding="utf-8")
    observed.clear()
    assert _ensure_setup(_options(tmp_path)) == 0
    assert not observed


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

    assert run_launch(_options(tmp_path), env={"HEARTWOOD_PLATFORM": "generic"}) == 66
    assert "Staged model verification failed: copy changed" in capsys.readouterr().out
    assert not any((tmp_path / "state" / "scratch").iterdir())
