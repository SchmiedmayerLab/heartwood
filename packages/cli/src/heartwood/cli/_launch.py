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


class LaunchConfigurationError(ValueError):
    """Raised when a launch cannot be planned without guessing."""


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
    partition: str | None
    gpus: int
    cpus: int
    memory: str
    time_limit: str
    dry_run: bool
    no_allocate: bool
    yes_request_allocation: bool
    inside_allocation: bool
    plain: bool
    startup_timeout: int


@dataclass(frozen=True, slots=True)
class LaunchPlan:
    """Human-reviewable compute and runtime launch proposal."""

    platform_id: str
    allocation_required: bool
    allocation_command: tuple[str, ...]
    model_root: Path | None
    state_root: Path
    partition: str | None = None

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
            f"Agent workspace: {self.state_root / 'workspaces'}",
        ]
        if self.partition is not None:
            lines.append(f"GPU partition: {self.partition}")
        if self.allocation_command:
            lines.append(f"Request: {shlex.join(self.allocation_command)}")
        return "\n".join(lines)


def build_launch_plan(options: LaunchOptions, env: Mapping[str, str]) -> LaunchPlan:
    """Build a platform-specific launch plan without changing external state."""
    platform_id = select_platform_adapter(env).adapter_id
    allocation_required = platform_id == "carina" and not env.get("SLURM_JOB_ID")
    command: tuple[str, ...] = ()
    partition: str | None = None
    if allocation_required:
        partition = _resolve_slurm_partition(options.partition, env)
        command = (
            "srun",
            "--pty",
            f"--partition={partition}",
            f"--gres=gpu:{options.gpus}",
            f"--cpus-per-task={options.cpus}",
            f"--mem={options.memory}",
            f"--time={options.time_limit}",
            "--export=ALL,HEARTWOOD_PLATFORM=carina",
            *_reentry_command(options),
        )
    return LaunchPlan(
        platform_id,
        allocation_required,
        command,
        options.model_root,
        options.state_root,
        partition,
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
    try:
        plan = build_launch_plan(options, active_env)
    except LaunchConfigurationError as error:
        print(f"Launch configuration error: {error}")
        return 64
    active_env["HEARTWOOD_PLATFORM"] = plan.platform_id
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
    command.extend(("--startup-timeout", str(options.startup_timeout)))
    return tuple(command)


def _run_command(command: Sequence[str]) -> int:
    return subprocess.run(command, check=False).returncode


def _run_runtime(options: LaunchOptions, env: Mapping[str, str]) -> int:
    started = time.monotonic()
    _stage(1, 6, "Verify the selected model snapshot")
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
    runtime_env = _runtime_environment(env)
    _stage(2, 6, "Validate the local inference runtime")
    preflight_error = _preflight_vllm(options, vllm, runtime_env)
    if preflight_error is not None:
        print(f"vLLM preflight failed: {preflight_error}")
        return 69

    scratch_value = env.get("LOCAL_SCRATCH_JOB")
    if env.get("HEARTWOOD_PLATFORM") == "carina" and not scratch_value:
        print("Carina allocation does not expose LOCAL_SCRATCH_JOB; no model was staged.")
        return 72
    scratch_parent = Path(scratch_value or options.state_root / "scratch")
    if scratch_value:
        if not scratch_parent.is_dir() or not os.access(scratch_parent, os.W_OK):
            print(f"Job-local scratch is unavailable or not writable: {scratch_parent}")
            return 72
    else:
        scratch_parent.mkdir(parents=True, exist_ok=True)
    model_size = _snapshot_size(options.model_root)
    scratch_available = shutil.disk_usage(scratch_parent).free
    if scratch_available < model_size:
        print(
            f"Job-local scratch requires {_format_bytes(model_size)} for the model; "
            f"{_format_bytes(scratch_available)} is available under {scratch_parent}."
        )
        return 73
    staged_model = Path(tempfile.mkdtemp(prefix="heartwood-model.", dir=scratch_parent))
    runtime: subprocess.Popen[str] | None = None
    try:
        _stage(3, 6, f"Stage the model on compute-local storage ({scratch_parent})")
        _copy_snapshot(options.model_root, staged_model)
        try:
            verify_snapshot(staged_model)
        except (OSError, UnicodeError, ValueError) as error:
            print(f"Staged model verification failed: {error}")
            return 66
        runtime_log = options.state_root / "runtime"
        runtime_log.mkdir(parents=True, exist_ok=True)
        log_path = runtime_log / "vllm.log"
        log_file = log_path.open("w", encoding="utf-8")
        try:
            _stage(4, 6, "Start vLLM; first startup commonly takes several minutes")
            runtime = subprocess.Popen(
                _vllm_command(vllm, staged_model, options.model_id),
                stdout=log_file,
                stderr=subprocess.STDOUT,
                text=True,
                env=runtime_env,
            )
        finally:
            log_file.close()
        if not _wait_for_runtime(runtime, timeout=options.startup_timeout):
            print(f"vLLM did not become ready after {options.startup_timeout} seconds.")
            _print_runtime_failure(log_path, runtime.poll())
            return 70
        print(f"vLLM is ready after {int(time.monotonic() - started)} seconds.")
        _stage(5, 6, "Validate the shared Heartwood setup")
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
        _stage(6, 6, f"Open session {options.session_id}")
        print(f"Workspace: {options.state_root / 'workspaces' / options.session_id}")
        return subprocess.run(chat, check=False, env=runtime_env).returncode
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
        "HEARTWOOD_PLATFORM",
        "HEARTWOOD_INSTALL_ROOT",
        "HEARTWOOD_NATIVE_ROOT",
        "HEARTWOOD_NATIVE_VERSION",
        "HEARTWOOD_HOME",
        "LOCAL_SCRATCH_JOB",
        "SLURM_JOB_ID",
        "SLURM_JOB_PARTITION",
        "SLURM_CLUSTER_NAME",
    )
    result = {name: env[name] for name in allowed_names if name in env}
    native_root = env.get("HEARTWOOD_NATIVE_ROOT")
    native_version = env.get("HEARTWOOD_NATIVE_VERSION")
    if native_root and native_version:
        bootstrap = Path(native_root) / "runtimes" / native_version / "bootstrap"
        result["PATH"] = _prepend_path(bootstrap / "bin", result.get("PATH"))
        result["LD_LIBRARY_PATH"] = _prepend_path(bootstrap / "lib", result.get("LD_LIBRARY_PATH"))
    result.update(
        {
            "HEARTWOOD_AGENT_BACKEND": "openhands-sdk",
            "HEARTWOOD_LOCAL_RUNTIME_HOST": "127.0.0.1",
            "HEARTWOOD_LOCAL_RUNTIME_PORT": "8765",
            "VLLM_USE_FLASHINFER_SAMPLER": env.get("VLLM_USE_FLASHINFER_SAMPLER", "0"),
        }
    )
    return result


def _wait_for_runtime(runtime: subprocess.Popen[str], timeout: float = 600) -> bool:
    started = time.monotonic()
    deadline = time.monotonic() + timeout
    next_update = started + 15
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while time.monotonic() < deadline:
        if runtime.poll() is not None:
            return False
        try:
            with opener.open("http://127.0.0.1:8765/v1/models", timeout=2) as response:
                payload = json.load(response)
                if response.status == 200 and payload.get("data"):
                    return True
        except (OSError, ValueError):
            now = time.monotonic()
            if now >= next_update:
                print(f"Still starting vLLM ({int(now - started)} seconds elapsed)...")
                next_update = now + 15
            time.sleep(1)
    return False


def _ensure_setup(options: LaunchOptions, env: Mapping[str, str] | None = None) -> int:
    runtime_env = _runtime_environment(os.environ if env is None else env)
    if not (options.state_root / "setup.json").is_file():
        setup_command = (
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
        setup_code = subprocess.run(setup_command, check=False, env=runtime_env).returncode
        if setup_code != 0:
            return setup_code
    doctor_command = (
        sys.executable,
        "-m",
        "heartwood.cli",
        "--workspace",
        str(options.workspace),
        "doctor",
    )
    return subprocess.run(doctor_command, check=False, env=runtime_env).returncode


def _resolve_slurm_partition(requested: str | None, env: Mapping[str, str]) -> str:
    configured = requested or env.get("HEARTWOOD_SLURM_PARTITION")
    partitions = _discover_slurm_gpu_partitions(env)
    if configured:
        if partitions and configured not in {name for name, _is_default in partitions}:
            available = ", ".join(name for name, _is_default in partitions)
            raise LaunchConfigurationError(
                f"GPU partition {configured!r} is unavailable; choose one of: {available}"
            )
        return configured
    for name, is_default in partitions:
        if is_default:
            return name
    if partitions:
        return partitions[0][0]
    raise LaunchConfigurationError(
        "no available GPU partition was detected; pass --partition or set HEARTWOOD_SLURM_PARTITION"
    )


def _discover_slurm_gpu_partitions(env: Mapping[str, str]) -> tuple[tuple[str, bool], ...]:
    try:
        completed = subprocess.run(
            ("sinfo", "--noheader", "--format=%P|%G|%a"),
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
            env=_scheduler_environment(env),
        )
    except (OSError, subprocess.TimeoutExpired):
        return ()
    if completed.returncode != 0:
        return ()
    partitions: list[tuple[str, bool]] = []
    for line in completed.stdout.splitlines():
        fields = line.strip().split("|")
        if len(fields) != 3:
            continue
        raw_name, resources, state = fields
        if "gpu" not in resources.lower() or state.lower() not in {"up", "idle", "mix", "alloc"}:
            continue
        name = raw_name.rstrip("*")
        entry = (name, raw_name.endswith("*"))
        if name and entry not in partitions:
            partitions.append(entry)
    return tuple(partitions)


def _scheduler_environment(env: Mapping[str, str]) -> dict[str, str]:
    return {
        name: env[name]
        for name in ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE")
        if name in env
    }


def _preflight_vllm(options: LaunchOptions, executable: Path, env: Mapping[str, str]) -> str | None:
    python = _resolve_vllm_python(options, executable, env)
    if not python.is_file() or not os.access(python, os.X_OK):
        return f"runtime Python is unavailable: {python}"
    try:
        completed = subprocess.run(
            (
                str(python),
                "-c",
                (
                    "import torchcodec, vllm; from importlib.metadata import version; "
                    "print(version('torchcodec'), version('vllm'))"
                ),
            ),
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
            env=dict(env),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return str(error)
    if completed.returncode == 0:
        versions = completed.stdout.strip()
        if versions:
            print(f"Runtime modules: {versions}")
        return None
    detail = completed.stderr.strip() or completed.stdout.strip() or "runtime import failed"
    return detail.splitlines()[-1]


def _resolve_vllm_python(options: LaunchOptions, executable: Path, env: Mapping[str, str]) -> Path:
    configured = env.get("HEARTWOOD_VLLM_PYTHON")
    if configured:
        return Path(configured)
    if options.environment_root is not None:
        return options.environment_root / "vllm" / "bin" / "python"
    native_root = env.get("HEARTWOOD_NATIVE_ROOT")
    native_version = env.get("HEARTWOOD_NATIVE_VERSION")
    if native_root and native_version:
        return Path(native_root) / "runtimes" / native_version / "vllm" / "bin" / "python"
    return executable.parent / "python"


def _copy_snapshot(source: Path, destination: Path) -> None:
    files = sorted(path for path in source.rglob("*") if path.is_file())
    total = sum(path.stat().st_size for path in files)
    copied = 0
    started = time.monotonic()
    next_update = started
    for source_file in files:
        relative = source_file.relative_to(source)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with source_file.open("rb") as source_stream, target.open("wb") as target_stream:
            while chunk := source_stream.read(16 * 1024 * 1024):
                target_stream.write(chunk)
                copied += len(chunk)
                now = time.monotonic()
                if now >= next_update:
                    _print_copy_progress(copied, total, now - started)
                    next_update = now + 5
        shutil.copystat(source_file, target, follow_symlinks=False)
    _print_copy_progress(copied, total, time.monotonic() - started)


def _snapshot_size(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def _print_copy_progress(copied: int, total: int, elapsed: float) -> None:
    percent = (copied / total * 100) if total else 100
    rate = copied / elapsed if elapsed > 0 else 0
    remaining = (total - copied) / rate if rate > 0 else 0
    print(
        f"Model staging: {percent:5.1f}% "
        f"({_format_bytes(copied)} of {_format_bytes(total)}, "
        f"about {int(remaining)} seconds remaining)"
    )


def _format_bytes(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} TiB"


def _print_runtime_failure(log_path: Path, return_code: int | None) -> None:
    if return_code is not None:
        print(f"vLLM exited with status {return_code}.")
    print(f"Runtime log: {log_path}")
    try:
        lines = [line.strip() for line in log_path.read_text(encoding="utf-8").splitlines()]
    except (OSError, UnicodeError):
        return
    relevant = [
        line
        for line in lines
        if line and any(marker in line.lower() for marker in ("error", "exception", "failed"))
    ]
    if relevant:
        print(f"Last reported error: {relevant[-1]}")


def _prepend_path(path: Path, existing: str | None) -> str:
    return f"{path}{os.pathsep}{existing}" if existing else str(path)


def _stage(number: int, count: int, message: str) -> None:
    print(f"\n[{number}/{count}] {message}")
