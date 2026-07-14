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
from heartwood.gateway import ProjectConfig, ProjectConfigStore, ProjectContext

InputFunction = Callable[[str], str]
RunFunction = Callable[[Sequence[str]], int]

_SLURM_EXPORTED_ENVIRONMENT = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "LD_LIBRARY_PATH",
    "PYTHONPATH",
    "VLLM_USE_FLASHINFER_SAMPLER",
)


class LaunchConfigurationError(ValueError):
    """Raised when a launch cannot be planned without guessing."""


@dataclass(frozen=True, slots=True)
class LaunchOptions:
    """Validated launch inputs shared by platform implementations."""

    project: ProjectContext
    session_id: str
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
    web: bool
    web_host: str
    web_port: int
    startup_timeout: int


@dataclass(frozen=True, slots=True)
class LaunchPlan:
    """Human-reviewable compute and runtime launch proposal."""

    platform_id: str
    allocation_required: bool
    allocation_command: tuple[str, ...]
    model_root: Path | None
    state_root: Path
    project_root: Path
    runtime: str | None
    model_id: str | None
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
            f"Runtime: {self.runtime if self.runtime is not None else 'not selected'}",
            f"State: {self.state_root}",
            f"Project: {self.project_root}",
        ]
        if self.partition is not None:
            lines.append(f"GPU partition: {self.partition}")
        if self.allocation_command:
            lines.append(f"Request: {shlex.join(self.allocation_command)}")
        return "\n".join(lines)


def build_launch_plan(options: LaunchOptions, env: Mapping[str, str]) -> LaunchPlan:
    """Build a platform-specific launch plan without changing external state."""
    platform_id = select_platform_adapter(env).adapter_id
    selection = _local_model_selection(options.project, env)
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
            f"--chdir={options.project.root}",
            _slurm_export_argument(env),
            *_reentry_command(options),
        )
    return LaunchPlan(
        platform_id,
        allocation_required,
        command,
        selection[0] if selection is not None else None,
        options.project.state_root,
        options.project.root,
        selection[1] if selection is not None else None,
        selection[2] if selection is not None else None,
        partition=partition,
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
        "--session-id",
        options.session_id,
        "launch",
        "--inside-allocation",
    ]
    if options.plain:
        command.append("--plain")
    if options.web:
        command.extend(("--web", "--host", options.web_host, "--port", str(options.web_port)))
    command.extend(("--startup-timeout", str(options.startup_timeout)))
    return tuple(command)


def _run_command(command: Sequence[str]) -> int:
    return subprocess.run(command, check=False).returncode


def _run_runtime(options: LaunchOptions, env: Mapping[str, str]) -> int:
    started = time.monotonic()
    selection = _local_model_selection(options.project, env)
    _stage(1, 6, "Verify the selected local model")
    if selection is None:
        print(
            "No local model is selected. Run `heartwood models artifacts`, then "
            "`heartwood models download <model-id>`."
        )
        return 64
    model_root, runtime_kind, model_id = selection
    if not model_root.exists():
        print(f"Selected local model is unavailable: {model_root}")
        return 66
    if runtime_kind == "vllm":
        try:
            verify_snapshot(model_root)
        except (OSError, UnicodeError, ValueError) as error:
            print(f"Model verification failed: {error}")
            return 66
    runtime_executable = _resolve_runtime_executable(runtime_kind)
    if not runtime_executable.is_file() or not os.access(runtime_executable, os.X_OK):
        print(f"{_runtime_label(runtime_kind)} executable is unavailable: {runtime_executable}")
        return 69
    runtime_env = _runtime_environment(env, project=options.project)
    _stage(2, 6, "Validate the local inference runtime")
    if runtime_kind == "vllm":
        preflight_error = _preflight_vllm(runtime_executable, runtime_env)
        if preflight_error is not None:
            print(f"vLLM preflight failed: {preflight_error}")
            return 69

    staged_model: Path | None = None
    staged_source = model_root
    scratch_value = env.get("LOCAL_SCRATCH_JOB")
    if env.get("HEARTWOOD_PLATFORM") == "carina":
        if not scratch_value:
            print("Carina allocation does not expose LOCAL_SCRATCH_JOB; no model was staged.")
            return 72
        scratch_parent = Path(scratch_value)
        if not scratch_parent.is_dir() or not os.access(scratch_parent, os.W_OK):
            print(f"Job-local scratch is unavailable or not writable: {scratch_parent}")
            return 72
        model_size = _model_size(model_root)
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
        if staged_model is None:
            _stage(3, 6, "Use the verified project-local model")
        else:
            _stage(3, 6, f"Stage the model on compute-local storage ({staged_model.parent})")
            try:
                staged_source = _stage_model(model_root, staged_model)
            except OSError as error:
                print(f"Model staging failed: {error}")
                return 74
            try:
                if runtime_kind == "vllm":
                    verify_snapshot(staged_source)
            except (OSError, UnicodeError, ValueError) as error:
                print(f"Staged model verification failed: {error}")
                return 66
        log_path = options.project.logs_dir / "local-model.log"
        log_file = log_path.open("w", encoding="utf-8")
        try:
            _stage(
                4,
                6,
                f"Start {_runtime_label(runtime_kind)}; first startup may take several minutes",
            )
            runtime = subprocess.Popen(
                _runtime_command(
                    runtime_kind,
                    runtime_executable,
                    staged_source,
                    model_id,
                ),
                stdout=log_file,
                stderr=subprocess.STDOUT,
                text=True,
                env=runtime_env,
            )
        finally:
            log_file.close()
        if not _wait_for_runtime(
            runtime,
            model_id=model_id,
            timeout=options.startup_timeout,
        ):
            print(
                f"{_runtime_label(runtime_kind)} did not become ready after "
                f"{options.startup_timeout} seconds."
            )
            _print_runtime_failure(log_path, runtime.poll())
            return 70
        print(
            f"{_runtime_label(runtime_kind)} is ready after "
            f"{int(time.monotonic() - started)} seconds."
        )
        runtime_env["HEARTWOOD_LOCAL_RUNTIME_ACTIVE"] = "1"
        _stage(5, 6, "Validate the shared Heartwood setup")
        setup_code = _ensure_setup(options, runtime_env)
        if setup_code != 0:
            return setup_code
        interaction, label = _interaction_command(options)
        _stage(6, 6, label)
        print(f"Project: {options.project.root}")
        return subprocess.run(
            interaction,
            check=False,
            env=runtime_env,
            cwd=options.project.root,
        ).returncode
    finally:
        if runtime is not None and runtime.poll() is None:
            runtime.terminate()
            try:
                runtime.wait(timeout=10)
            except subprocess.TimeoutExpired:
                runtime.kill()
                runtime.wait()
        if staged_model is not None:
            shutil.rmtree(staged_model, ignore_errors=True)


def _interaction_command(options: LaunchOptions) -> tuple[list[str], str]:
    if options.web:
        return (
            [
                sys.executable,
                "-m",
                "heartwood.cli",
                "serve",
                "--host",
                options.web_host,
                "--port",
                str(options.web_port),
            ],
            f"Open the web interface on {options.web_host}:{options.web_port}",
        )
    command = [
        sys.executable,
        "-m",
        "heartwood.cli",
        "--session-id",
        options.session_id,
        "chat",
    ]
    if options.plain:
        command.append("--plain")
    return command, f"Open session {options.session_id}"


def _resolve_runtime_executable(runtime: str) -> Path:
    if runtime == "llama-cpp":
        installed = shutil.which("llama-server")
        return Path(installed) if installed else Path("/opt/llama.cpp/llama-server")
    version_root = _native_version_root()
    discovered_vllm = shutil.which("vllm")
    candidates: tuple[Path | None, ...] = (
        version_root / "vllm" / "bin" / "vllm" if version_root is not None else None,
        Path("/opt/heartwood-vllm/bin/vllm"),
        Path(discovered_vllm) if discovered_vllm is not None else None,
    )
    return next(
        (candidate for candidate in candidates if candidate and candidate.is_file()),
        Path("/opt/heartwood-vllm/bin/vllm"),
    )


def _runtime_command(
    runtime: str,
    executable: Path,
    model: Path,
    model_id: str,
) -> tuple[str, ...]:
    if runtime == "llama-cpp":
        model_file = _gguf_file(model)
        return (
            str(executable),
            "--model",
            str(model_file),
            "--alias",
            model_id,
            "--host",
            "127.0.0.1",
            "--port",
            "8765",
            "--ctx-size",
            "4096",
        )
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


def _local_model_selection(
    project: ProjectContext,
    env: Mapping[str, str],
) -> tuple[Path, str, str] | None:
    adapter = select_platform_adapter(env)
    store = ProjectConfigStore(
        project,
        ProjectConfig(
            platform_id=adapter.adapter_id,
            policy=adapter.default_policy_profile(),
        ),
    )
    try:
        selection = store.load().local_model
    except ValueError as error:
        raise LaunchConfigurationError(str(error)) from error
    if selection is None:
        return None
    model = selection.resolved_path(project)
    runtime = selection.runtime
    if runtime == "auto":
        runtime = "llama-cpp" if model.suffix.casefold() == ".gguf" else "vllm"
    return model, runtime, selection.model_id


def _runtime_label(runtime: str) -> str:
    return "llama.cpp" if runtime == "llama-cpp" else "vLLM"


def _gguf_file(model: Path) -> Path:
    if model.is_file() and model.suffix.casefold() == ".gguf":
        return model
    candidates = sorted(model.rglob("*.gguf")) if model.is_dir() else []
    if len(candidates) != 1:
        raise LaunchConfigurationError(
            "the selected llama.cpp artifact must contain exactly one GGUF file"
        )
    return candidates[0]


def _native_version_root() -> Path | None:
    executable = Path(sys.executable).absolute()
    parents = executable.parents
    if len(parents) >= 3 and parents[1].name == "heartwood":
        return parents[2]
    return None


def _runtime_environment(
    env: Mapping[str, str],
    *,
    project: ProjectContext | None = None,
) -> dict[str, str]:
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
        "HEARTWOOD_PLATFORM",
        "HEARTWOOD_LOCAL_RUNTIME_ACTIVE",
        "LOCAL_SCRATCH_JOB",
        "SLURM_JOB_ID",
        "SLURM_JOB_PARTITION",
        "SLURM_CLUSTER_NAME",
    )
    result = {name: env[name] for name in allowed_names if name in env}
    version_root = _native_version_root()
    if version_root is not None:
        bootstrap = version_root / "bootstrap"
        result["PATH"] = _prepend_path(bootstrap / "bin", result.get("PATH"))
        result["LD_LIBRARY_PATH"] = _prepend_path(bootstrap / "lib", result.get("LD_LIBRARY_PATH"))
    if project is not None:
        result.update(
            {
                "HF_HOME": str(project.cache_dir / "huggingface"),
                "TORCH_HOME": str(project.cache_dir / "torch"),
                "TRANSFORMERS_CACHE": str(project.cache_dir / "transformers"),
                "XDG_CACHE_HOME": str(project.cache_dir),
            }
        )
    result.update(
        {
            "VLLM_USE_FLASHINFER_SAMPLER": env.get("VLLM_USE_FLASHINFER_SAMPLER", "0"),
        }
    )
    return result


def _wait_for_runtime(
    runtime: subprocess.Popen[str],
    *,
    model_id: str,
    timeout: float = 600,
) -> bool:
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
                if response.status == 200 and _catalog_contains_model(payload, model_id):
                    return True
        except (OSError, ValueError):
            pass
        now = time.monotonic()
        if now >= next_update:
            elapsed = int(now - started)
            print(f"Still starting the local model server ({elapsed} seconds elapsed)...")
            next_update = now + 15
        time.sleep(1)
    return False


def _catalog_contains_model(payload: object, model_id: str) -> bool:
    if not isinstance(payload, dict):
        return False
    models = payload.get("data")
    return isinstance(models, list) and any(
        isinstance(model, dict) and model.get("id") == model_id for model in models
    )


def _ensure_setup(options: LaunchOptions, env: Mapping[str, str] | None = None) -> int:
    runtime_env = _runtime_environment(
        os.environ if env is None else env,
        project=options.project,
    )
    adapter = select_platform_adapter(runtime_env)
    store = ProjectConfigStore(
        options.project,
        ProjectConfig(
            platform_id=adapter.adapter_id,
            policy=adapter.default_policy_profile(),
        ),
    )
    configured = False
    if store.configured:
        config = store.load()
        if config.model_source == "local":
            try:
                profile = config.model_settings.profile()
            except ValueError:
                pass
            else:
                configured = profile.is_local
    if not configured:
        selection = _local_model_selection(options.project, runtime_env)
        if selection is None:
            print("No local model is selected for setup.")
            return 64
        setup_command = (
            sys.executable,
            "-m",
            "heartwood.cli",
            "setup",
            "--model-source",
            "local",
            "--model-id",
            selection[2],
            "--non-interactive",
            "--yes",
        )
        setup_code = subprocess.run(
            setup_command,
            check=False,
            env=runtime_env,
            cwd=options.project.root,
        ).returncode
        if setup_code != 0:
            return setup_code
    doctor_command = (
        sys.executable,
        "-m",
        "heartwood.cli",
        "doctor",
    )
    return subprocess.run(
        doctor_command,
        check=False,
        env=runtime_env,
        cwd=options.project.root,
    ).returncode


def _resolve_slurm_partition(requested: str | None, env: Mapping[str, str]) -> str:
    partitions = _discover_slurm_gpu_partitions(env)
    if requested:
        if partitions and requested not in {name for name, _is_default in partitions}:
            available = ", ".join(name for name, _is_default in partitions)
            raise LaunchConfigurationError(
                f"GPU partition {requested!r} is unavailable; choose one of: {available}"
            )
        return requested
    for name, is_default in partitions:
        if is_default:
            return name
    if len(partitions) == 1:
        return partitions[0][0]
    if partitions:
        available = ", ".join(name for name, _is_default in partitions)
        raise LaunchConfigurationError(
            f"no default GPU partition was detected; choose one of: {available}"
        )
    raise LaunchConfigurationError("no available GPU partition was detected; pass --partition")


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


def _slurm_export_argument(env: Mapping[str, str]) -> str:
    exported = [name for name in _SLURM_EXPORTED_ENVIRONMENT if name in env]
    exported.append("HEARTWOOD_PLATFORM=carina")
    return f"--export={','.join(exported)}"


def _preflight_vllm(executable: Path, env: Mapping[str, str]) -> str | None:
    python = executable.parent / "python"
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


def _stage_model(source: Path, destination: Path) -> Path:
    if source.is_file():
        target = destination / source.name
        shutil.copy2(source, target)
        return target
    _copy_snapshot(source, destination)
    return destination


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


def _model_size(root: Path) -> int:
    if root.is_file():
        return root.stat().st_size
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
