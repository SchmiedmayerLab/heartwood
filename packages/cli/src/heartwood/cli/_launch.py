# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Environment-aware native runtime launch orchestration."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from heartwood.adapters.platform import select_platform_adapter
from heartwood.cli._model_snapshot import verify_snapshot

InputFunction = Callable[[str], str]
RunFunction = Callable[[Sequence[str]], int]


@dataclass(frozen=True, slots=True)
class LaunchOptions:
    """Validated launch inputs shared by platform implementations."""

    workspace: Path
    session_id: str
    model_root: Path | None
    state_root: Path
    environment_root: Path | None
    vllm_executable: Path | None
    model_id: str
    partition: str
    gpus: int
    cpus: int
    memory: str
    time_limit: str
    dry_run: bool
    no_allocate: bool
    yes_request_allocation: bool
    inside_allocation: bool
    plain: bool


@dataclass(frozen=True, slots=True)
class LaunchPlan:
    """Human-reviewable compute and runtime launch proposal."""

    platform_id: str
    allocation_required: bool
    allocation_command: tuple[str, ...]
    model_root: Path | None
    state_root: Path

    def format(self) -> str:
        """Render the launch proposal without secrets."""
        compute = "Slurm allocation required" if self.allocation_required else "already provisioned"
        lines = [
            "Heartwood launch plan",
            "",
            f"Platform: {self.platform_id}",
            f"Compute: {compute}",
            f"Model: {self.model_root if self.model_root is not None else 'not selected'}",
            f"State: {self.state_root}",
        ]
        if self.allocation_command:
            lines.append(f"Request: {shlex.join(self.allocation_command)}")
        return "\n".join(lines)


def build_launch_plan(options: LaunchOptions, env: Mapping[str, str]) -> LaunchPlan:
    """Build a platform-specific launch plan without changing external state."""
    platform_id = select_platform_adapter(env).adapter_id
    allocation_required = platform_id == "carina" and not env.get("SLURM_JOB_ID")
    command: tuple[str, ...] = ()
    if allocation_required:
        command = (
            "srun",
            "--pty",
            f"--partition={options.partition}",
            f"--gres=gpu:{options.gpus}",
            f"--cpus-per-task={options.cpus}",
            f"--mem={options.memory}",
            f"--time={options.time_limit}",
            *_reentry_command(options),
        )
    return LaunchPlan(
        platform_id, allocation_required, command, options.model_root, options.state_root
    )


def run_launch(
    options: LaunchOptions,
    *,
    env: Mapping[str, str] | None = None,
    input_fn: InputFunction = input,
    run_fn: RunFunction | None = None,
) -> int:
    """Execute a reviewed allocation request or the in-allocation runtime."""
    active_env = dict(os.environ if env is None else env)
    plan = build_launch_plan(options, active_env)
    print(plan.format())
    if options.dry_run:
        return 0
    if plan.allocation_required:
        if options.no_allocate:
            print("\nA GPU allocation is required; rerun without --no-allocate.")
            return 1
        if not options.yes_request_allocation:
            try:
                approved = input_fn("\nRequest this GPU allocation? [y/N]: ").strip().lower() == "y"
            except EOFError:
                approved = False
            if not approved:
                print("Allocation cancelled.")
                return 1
        runner = run_fn or _run_command
        return runner(plan.allocation_command)
    return _run_runtime(options, active_env)


def _reentry_command(options: LaunchOptions) -> tuple[str, ...]:
    command = [
        sys.executable,
        "-m",
        "heartwood.cli",
        "--workspace",
        str(options.workspace),
        "--session-id",
        options.session_id,
        "launch",
        "--inside-allocation",
        "--state-root",
        str(options.state_root),
        "--model-id",
        options.model_id,
    ]
    if options.model_root is not None:
        command.extend(("--model-root", str(options.model_root)))
    if options.environment_root is not None:
        command.extend(("--environment-root", str(options.environment_root)))
    if options.vllm_executable is not None:
        command.extend(("--vllm-executable", str(options.vllm_executable)))
    if options.plain:
        command.append("--plain")
    return tuple(command)


def _run_command(command: Sequence[str]) -> int:
    return subprocess.run(command, check=False).returncode


def _run_runtime(options: LaunchOptions, env: Mapping[str, str]) -> int:
    if options.model_root is None:
        print("A verified model snapshot is required; pass --model-root.")
        return 64
    try:
        verify_snapshot(options.model_root)
    except (OSError, UnicodeError, ValueError) as error:
        print(f"Model verification failed: {error}")
        return 66
    vllm = _resolve_vllm(options, env)
    if not vllm.is_file() or not os.access(vllm, os.X_OK):
        print(f"vLLM executable is unavailable: {vllm}")
        return 69

    scratch_parent = Path(env.get("LOCAL_SCRATCH_JOB", str(options.state_root / "scratch")))
    scratch_parent.mkdir(parents=True, exist_ok=True)
    staged_model = Path(tempfile.mkdtemp(prefix="heartwood-model.", dir=scratch_parent))
    runtime: subprocess.Popen[str] | None = None
    try:
        shutil.copytree(options.model_root, staged_model, dirs_exist_ok=True, symlinks=False)
        try:
            verify_snapshot(staged_model)
        except (OSError, UnicodeError, ValueError) as error:
            print(f"Staged model verification failed: {error}")
            return 66
        runtime_log = options.state_root / "runtime"
        runtime_log.mkdir(parents=True, exist_ok=True)
        log_file = (runtime_log / "vllm.log").open("a", encoding="utf-8")
        try:
            runtime = subprocess.Popen(
                _vllm_command(vllm, staged_model, options.model_id),
                stdout=log_file,
                stderr=subprocess.STDOUT,
                text=True,
                env=_runtime_environment(env),
            )
        finally:
            log_file.close()
        if not _wait_for_runtime(runtime):
            print(f"vLLM did not become ready; inspect {runtime_log / 'vllm.log'}")
            return 70
        setup_code = _ensure_setup(options, env)
        if setup_code != 0:
            return setup_code
        chat = [
            sys.executable,
            "-m",
            "heartwood.cli",
            "--workspace",
            str(options.workspace),
            "--session-id",
            options.session_id,
            "chat",
        ]
        if options.plain:
            chat.append("--plain")
        return subprocess.run(chat, check=False, env=_runtime_environment(env)).returncode
    finally:
        if runtime is not None and runtime.poll() is None:
            runtime.terminate()
            try:
                runtime.wait(timeout=10)
            except subprocess.TimeoutExpired:
                runtime.kill()
                runtime.wait()
        shutil.rmtree(staged_model, ignore_errors=True)


def _resolve_vllm(options: LaunchOptions, env: Mapping[str, str]) -> Path:
    if options.vllm_executable is not None:
        return options.vllm_executable
    configured = env.get("HEARTWOOD_VLLM_EXECUTABLE")
    if configured:
        return Path(configured)
    if options.environment_root is not None:
        return options.environment_root / "vllm" / "bin" / "vllm"
    native_root = env.get("HEARTWOOD_NATIVE_ROOT")
    native_version = env.get("HEARTWOOD_NATIVE_VERSION")
    if native_root and native_version:
        return Path(native_root) / "runtimes" / native_version / "vllm" / "bin" / "vllm"
    return Path("/opt/heartwood-vllm/bin/vllm")


def _vllm_command(executable: Path, model: Path, model_id: str) -> tuple[str, ...]:
    return (
        str(executable),
        "serve",
        str(model),
        "--host",
        "127.0.0.1",
        "--port",
        "8765",
        "--served-model-name",
        model_id,
        "--max-model-len",
        "8192",
        "--enable-auto-tool-choice",
        "--tool-call-parser",
        "hermes",
    )


def _runtime_environment(env: Mapping[str, str]) -> dict[str, str]:
    allowed_names = (
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TMPDIR",
        "TMP",
        "TEMP",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TERM",
        "VIRTUAL_ENV",
        "PYTHONPATH",
        "LD_LIBRARY_PATH",
        "DYLD_LIBRARY_PATH",
        "CUDA_VISIBLE_DEVICES",
        "NVIDIA_VISIBLE_DEVICES",
        "NVIDIA_DRIVER_CAPABILITIES",
        "XDG_CACHE_HOME",
        "HF_HOME",
        "TRANSFORMERS_CACHE",
        "TORCH_HOME",
    )
    result = {name: env[name] for name in allowed_names if name in env}
    result.update(
        {
            "HEARTWOOD_AGENT_BACKEND": "openhands-sdk",
            "HEARTWOOD_LOCAL_RUNTIME_HOST": "127.0.0.1",
            "HEARTWOOD_LOCAL_RUNTIME_PORT": "8765",
        }
    )
    return result


def _wait_for_runtime(runtime: subprocess.Popen[str], timeout: float = 300) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if runtime.poll() is not None:
            return False
        try:
            with urllib.request.urlopen("http://127.0.0.1:8765/v1/models", timeout=2) as response:
                payload = json.load(response)
                if response.status == 200 and payload.get("data"):
                    return True
        except (OSError, ValueError):
            time.sleep(1)
    return False


def _ensure_setup(options: LaunchOptions, env: Mapping[str, str] | None = None) -> int:
    if (options.state_root / "setup.json").is_file():
        return 0
    command = (
        sys.executable,
        "-m",
        "heartwood.cli",
        "--workspace",
        str(options.workspace),
        "setup",
        "--model-source",
        "local",
        "--model-id",
        options.model_id,
        "--non-interactive",
        "--yes",
    )
    return subprocess.run(
        command,
        check=False,
        env=_runtime_environment(os.environ if env is None else env),
    ).returncode
