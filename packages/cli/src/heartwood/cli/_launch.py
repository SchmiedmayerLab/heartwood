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
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from packaging.version import InvalidVersion, Version

from heartwood.adapters.platform import select_platform_adapter
from heartwood.cli._model_snapshot import verify_snapshot
from heartwood.gateway import (
    LocalContextPlan,
    ProjectConfig,
    ProjectConfigStore,
    ProjectContext,
    estimate_local_runtime_memory,
    managed_model_token_budgets,
    plan_local_context_window,
    verify_model_artifact,
)

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
    prompt: str | None
    prompt_file: Path | None


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
    context_window: int | None
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
            (
                f"Context capacity: up to {self.context_window:,} tokens"
                if self.context_window
                else "Context capacity: not selected"
            ),
            f"State: {self.state_root}",
            f"Project: {self.project_root}",
        ]
        if self.partition is not None:
            lines.append(f"GPU partition: {self.partition}")
        if self.allocation_command:
            lines.append(f"Request: {shlex.join(self.allocation_command)}")
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class LocalRuntimeSelection:
    """Persisted local-model selection normalized for runtime launch."""

    model_root: Path
    runtime: Literal["llama-cpp", "vllm"]
    model_id: str
    size_bytes: int | None
    artifact_sha256: str | None
    context_window: int


def build_launch_plan(options: LaunchOptions, env: Mapping[str, str]) -> LaunchPlan:
    """Build a platform-specific launch plan without changing external state."""
    if options.gpus != 1:
        raise LaunchConfigurationError(
            "Heartwood currently supports exactly one GPU per managed vLLM runtime"
        )
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
        selection.model_root if selection is not None else None,
        options.project.state_root,
        options.project.root,
        selection.runtime if selection is not None else None,
        selection.model_id if selection is not None else None,
        selection.context_window if selection is not None else None,
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
    owned_prompt_file: Path | None = None
    requested_interface = "web" if options.web else "terminal"
    capabilities = select_platform_adapter(active_env).capabilities()
    if requested_interface not in capabilities.interfaces:
        print(
            "Launch configuration error: "
            f"{capabilities.display_name} does not provide the {requested_interface} interface. "
            f"Use the {capabilities.interfaces[0]} interface in this environment."
        )
        return 64
    try:
        try:
            active_options, owned_prompt_file = _materialize_prompt(options)
            plan = build_launch_plan(active_options, active_env)
        except LaunchConfigurationError as error:
            print(f"Launch configuration error: {error}")
            return 64
        active_env["HEARTWOOD_PLATFORM"] = plan.platform_id
        print(plan.format())
        if active_options.dry_run:
            return 0
        if plan.allocation_required:
            if active_options.no_allocate:
                print("\nA GPU allocation is required; rerun without --no-allocate.")
                return 1
            if not active_options.yes_request_allocation:
                try:
                    approved = (
                        input_fn("\nRequest this GPU allocation? [y/N]: ").strip().lower() == "y"
                    )
                except EOFError:
                    approved = False
                if not approved:
                    print("Allocation cancelled.")
                    return 1
            runner = run_fn or _run_command
            return runner(plan.allocation_command)
        return _run_runtime(active_options, active_env)
    finally:
        if owned_prompt_file is not None:
            owned_prompt_file.unlink(missing_ok=True)


def _materialize_prompt(options: LaunchOptions) -> tuple[LaunchOptions, Path | None]:
    if options.prompt is None:
        return options, None
    options.project.initialize()
    descriptor, raw_path = tempfile.mkstemp(
        prefix="pending-prompt.",
        suffix=".txt",
        dir=options.project.runtime_dir,
        text=True,
    )
    path = Path(raw_path)
    try:
        os.fchmod(descriptor, 0o600)
    except OSError as error:
        os.close(descriptor)
        path.unlink(missing_ok=True)
        raise LaunchConfigurationError(f"unable to protect the pending task: {error}") from error
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(options.prompt)
    except OSError as error:
        path.unlink(missing_ok=True)
        raise LaunchConfigurationError(f"unable to store the pending task: {error}") from error
    return replace(options, prompt=None, prompt_file=path), path


def _reentry_command(options: LaunchOptions) -> tuple[str, ...]:
    command = [
        sys.executable,
        "-m",
        "heartwood.cli",
        "--session-id",
        options.session_id,
    ]
    if options.plain:
        command.append("--plain")
    if options.web:
        command.extend(("--interface", "web", "--host", options.web_host))
    if options.prompt_file is not None:
        command.extend(("--prompt-file", str(options.prompt_file)))
    command.extend(
        (
            "--port",
            str(options.web_port),
            "runtime",
            "start",
            "--inside-allocation",
            "--startup-timeout",
            str(options.startup_timeout),
        )
    )
    return tuple(command)


def _run_command(command: Sequence[str]) -> int:
    return subprocess.run(command, check=False).returncode


def _run_runtime(options: LaunchOptions, env: Mapping[str, str]) -> int:
    started = time.monotonic()
    selection = _local_model_selection(options.project, env)
    _stage(1, 6, "Verify the selected Heartwood-managed model")
    if selection is None:
        print(
            "No Heartwood-managed model is selected. "
            "Run `heartwood setup` and choose Run with Heartwood."
        )
        return 64
    model_root = selection.model_root
    runtime_kind = selection.runtime
    model_id = selection.model_id
    if not model_root.exists():
        print(f"Selected Heartwood-managed model is unavailable: {model_root}")
        return 66
    try:
        _verify_local_model(selection)
    except (OSError, UnicodeError, ValueError) as error:
        print(f"Model verification failed: {error}")
        return 66
    runtime_executable = _resolve_runtime_executable(runtime_kind)
    if not runtime_executable.is_file() or not os.access(runtime_executable, os.X_OK):
        print(f"{_runtime_label(runtime_kind)} executable is unavailable: {runtime_executable}")
        return 69
    runtime_env = _runtime_environment(env, project=options.project)
    try:
        context_plan = _context_plan(selection, runtime_env)
    except ValueError as error:
        print(f"Heartwood-managed model cannot start an agent session: {error}")
        return 64
    selection = replace(selection, context_window=context_plan.effective_window)
    runtime_env["HEARTWOOD_LOCAL_MODEL_CONTEXT"] = str(selection.context_window)
    _print_resource_assessment(selection, runtime_env, context_plan=context_plan)
    _stage(2, 6, "Validate the Heartwood-managed inference runtime")
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
            print(f"Allocation scratch is unavailable or not writable: {scratch_parent}")
            return 72
        model_size = _model_size(model_root)
        scratch_available = shutil.disk_usage(scratch_parent).free
        if scratch_available < model_size:
            print(
                f"Allocation scratch requires {_format_bytes(model_size)} for the model; "
                f"{_format_bytes(scratch_available)} is available under {scratch_parent}."
            )
            return 73
        staged_model = Path(tempfile.mkdtemp(prefix="heartwood-model.", dir=scratch_parent))
    runtime: subprocess.Popen[str] | None = None
    try:
        if staged_model is None:
            _stage(3, 6, "Use the verified project model")
        else:
            _stage(3, 6, f"Stage the model in allocation scratch ({staged_model.parent})")
            try:
                staged_source = _stage_model(model_root, staged_model)
            except OSError as error:
                print(f"Model staging failed: {error}")
                return 74
            try:
                _verify_local_model(selection, model_root=staged_source)
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
                    selection.context_window,
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
            elapsed = int(time.monotonic() - started)
            exit_code = runtime.poll()
            if exit_code is None:
                print(
                    f"{_runtime_label(runtime_kind)} did not become ready after "
                    f"{options.startup_timeout} seconds."
                )
            else:
                print(
                    f"{_runtime_label(runtime_kind)} exited before becoming ready "
                    f"after {elapsed} seconds."
                )
            _print_runtime_failure(log_path, exit_code)
            return 70
        print(
            f"{_runtime_label(runtime_kind)} is ready after "
            f"{int(time.monotonic() - started)} seconds."
        )
        runtime_env["HEARTWOOD_LOCAL_RUNTIME_ACTIVE"] = "1"
        _stage(5, 6, "Validate the shared Heartwood setup")
        setup_code = _ensure_setup(
            options,
            runtime_env,
            context_window=selection.context_window,
        )
        if setup_code != 0:
            return setup_code
        interaction, label = _interaction_command(options)
        _stage(6, 6, label)
        print(f"Project: {options.project.root}")
        return _run_interaction(
            interaction,
            env=runtime_env,
            cwd=options.project.root,
        )
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


def _run_interaction(
    command: Sequence[str],
    *,
    env: Mapping[str, str],
    cwd: Path,
) -> int:
    try:
        return subprocess.run(command, check=False, env=env, cwd=cwd).returncode
    except KeyboardInterrupt:
        print("\nHeartwood stopped.")
        return 130


def _interaction_command(
    options: LaunchOptions,
) -> tuple[list[str], str]:
    if options.web:
        return (
            [
                sys.executable,
                "-m",
                "heartwood.cli",
                "gateway",
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
    ]
    if options.plain:
        command.append("--plain")
    if options.prompt_file is not None:
        command.extend(("--prompt-file", str(options.prompt_file)))
    return command, f"Open session {options.session_id}"


def _resolve_runtime_executable(runtime: str) -> Path:
    if runtime == "llama-cpp":
        installed = shutil.which("llama-server")
        return Path(installed) if installed else Path("/opt/llama.cpp/llama-server")
    version_root = _native_version_root()
    candidates: tuple[Path | None, ...] = (
        version_root / "vllm" / "bin" / "heartwood-vllm" if version_root is not None else None,
        Path("/opt/heartwood-vllm/bin/heartwood-vllm"),
    )
    return next(
        (candidate for candidate in candidates if candidate and candidate.is_file()),
        Path("/opt/heartwood-vllm/bin/heartwood-vllm"),
    )


def _runtime_command(
    runtime: str,
    executable: Path,
    model: Path,
    model_id: str,
    context_window: int,
) -> tuple[str, ...]:
    if runtime == "llama-cpp":
        model_file = _gguf_file(model)
        return (
            str(executable),
            "--model",
            str(model_file),
            "--jinja",
            "--alias",
            model_id,
            "--host",
            "127.0.0.1",
            "--port",
            "8765",
            "--ctx-size",
            str(context_window),
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
        str(context_window),
        "--enable-auto-tool-choice",
        "--tool-call-parser",
        "hermes",
    )


def _local_model_selection(
    project: ProjectContext,
    env: Mapping[str, str],
) -> LocalRuntimeSelection | None:
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
    runtime: Literal["llama-cpp", "vllm"]
    if selection.runtime == "auto":
        runtime = "llama-cpp" if model.suffix.casefold() == ".gguf" else "vllm"
    elif selection.runtime == "llama-cpp":
        runtime = "llama-cpp"
    else:
        runtime = "vllm"
    return LocalRuntimeSelection(
        model_root=model,
        runtime=runtime,
        model_id=selection.model_id,
        size_bytes=selection.size_bytes,
        artifact_sha256=selection.artifact_sha256,
        context_window=selection.context_window,
    )


def _print_resource_assessment(
    selection: LocalRuntimeSelection,
    env: Mapping[str, str],
    *,
    context_plan: LocalContextPlan | None = None,
) -> None:
    """Report the selected context and a conservative memory preflight."""
    plan = _context_plan(selection, env) if context_plan is None else context_plan
    model_bytes = selection.size_bytes
    if model_bytes is None and selection.model_root.exists():
        model_bytes = _model_size(selection.model_root)
    print(
        f"Context window: {plan.effective_window:,} tokens selected automatically "
        f"(model capacity: {plan.model_limit:,})."
    )
    print(f"Context selection: {plan.reason}")
    if model_bytes is None:
        print("Resource check: model size is unavailable; memory could not be estimated.")
        return

    system_required = max(
        8 * 1024**3,
        estimate_local_runtime_memory(
            context_window=plan.effective_window,
            model_size_bytes=model_bytes,
            runtime="llama-cpp",
        ),
    )
    system_available = _available_system_memory_bytes()
    _print_memory_result("RAM", system_required, system_available)

    if selection.runtime == "vllm":
        gpu_required = estimate_local_runtime_memory(
            context_window=plan.effective_window,
            model_size_bytes=model_bytes,
            runtime="vllm",
        )
        gpu_available = _available_gpu_memory_bytes(env)
        _print_memory_result("GPU memory", gpu_required, gpu_available)


def _context_plan(
    selection: LocalRuntimeSelection,
    env: Mapping[str, str],
) -> LocalContextPlan:
    model_bytes = selection.size_bytes
    if model_bytes is None and selection.model_root.exists():
        model_bytes = _model_size(selection.model_root)
    available = (
        _available_gpu_memory_bytes(env)
        if selection.runtime == "vllm"
        else _available_system_memory_bytes()
    )
    return plan_local_context_window(
        model_limit=selection.context_window,
        model_size_bytes=model_bytes,
        runtime=selection.runtime,
        available_memory_bytes=available,
    )


def _print_memory_result(label: str, required: int, available: int | None) -> None:
    estimate = _format_bytes(required)
    if available is None:
        print(
            f"Resource check: {label} availability could not be verified; "
            f"estimated minimum {estimate}."
        )
        return
    observed = _format_bytes(available)
    if available < required:
        print(
            f"Warning: {label} may be insufficient for this model and context; "
            f"{observed} available, estimated minimum {estimate}."
        )
        return
    print(f"Resource check: {observed} {label} available; estimated minimum {estimate}.")


def _available_system_memory_bytes() -> int | None:
    candidates: list[int] = []
    meminfo_available: int | None = None
    try:
        values: dict[str, int] = {}
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            name, separator, raw = line.partition(":")
            if separator and raw.strip().endswith("kB"):
                values[name] = int(raw.strip().split()[0]) * 1024
        meminfo_available = values.get("MemAvailable")
    except (OSError, ValueError):
        meminfo_available = None
    if meminfo_available:
        candidates.append(meminfo_available)

    for maximum_path, current_path in (
        (Path("/sys/fs/cgroup/memory.max"), Path("/sys/fs/cgroup/memory.current")),
        (
            Path("/sys/fs/cgroup/memory/memory.limit_in_bytes"),
            Path("/sys/fs/cgroup/memory/memory.usage_in_bytes"),
        ),
    ):
        try:
            maximum_text = maximum_path.read_text(encoding="utf-8").strip()
            current_text = current_path.read_text(encoding="utf-8").strip()
            if maximum_text == "max":
                continue
            maximum = int(maximum_text)
            if maximum < 2**60:
                candidates.append(max(0, maximum - int(current_text)))
        except (OSError, ValueError):
            continue
    return min(candidates) if candidates else None


def _available_gpu_memory_bytes(env: Mapping[str, str]) -> int | None:
    executable = shutil.which("nvidia-smi", path=env.get("PATH"))
    if executable is None:
        return None
    try:
        result = subprocess.run(
            (
                executable,
                "--query-gpu=memory.free",
                "--format=csv,noheader,nounits",
            ),
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            env=dict(env),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        values = [
            int(line.strip()) * 1024**2 for line in result.stdout.splitlines() if line.strip()
        ]
    except ValueError:
        return None
    return min(values) if values else None


def _verify_local_model(
    selection: LocalRuntimeSelection,
    *,
    model_root: Path | None = None,
) -> None:
    root = selection.model_root if model_root is None else model_root
    if selection.runtime == "vllm":
        verify_snapshot(root)
        return
    if selection.size_bytes is None or selection.artifact_sha256 is None:
        raise LaunchConfigurationError(
            "the selected llama.cpp artifact is missing persisted size or checksum metadata"
        )
    verify_model_artifact(
        _gguf_file(root),
        expected_size_bytes=selection.size_bytes,
        expected_sha256=selection.artifact_sha256,
    )


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
        "GOOGLE_PROJECT",
        "CLUSTER_NAME",
        "JUPYTERHUB_SERVICE_PREFIX",
        "HEARTWOOD_GPU_RUNTIME",
        "HEARTWOOD_IMAGE_FLAVOR",
        "HEARTWOOD_PLATFORM",
        "HEARTWOOD_PLATFORM_HOME",
        "HEARTWOOD_LOCAL_MODEL_CONTEXT",
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
            print(f"Still starting the Heartwood-managed model ({elapsed} seconds elapsed)...")
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


def _ensure_setup(
    options: LaunchOptions,
    env: Mapping[str, str] | None = None,
    *,
    context_window: int | None = None,
) -> int:
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
        if config.model_source == "heartwood":
            try:
                profile = config.model_settings.profile()
            except ValueError:
                pass
            else:
                configured = profile.is_local
    if not configured:
        selection = _local_model_selection(options.project, runtime_env)
        if selection is None:
            print("No Heartwood-managed model is selected for setup.")
            return 64
        setup_command = (
            sys.executable,
            "-m",
            "heartwood.cli",
            "setup",
            "--model-source",
            "heartwood",
            "--model-id",
            selection.model_id,
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
    if context_window is not None:
        _persist_effective_context(store, context_window)
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


def _persist_effective_context(store: ProjectConfigStore, context_window: int) -> None:
    """Keep the active managed OpenHands profile aligned with the launched runtime."""
    input_capacity, output_budget = managed_model_token_budgets(context_window)

    def update(config: ProjectConfig) -> ProjectConfig:
        profile = config.model_settings.profile()
        if not profile.is_local:
            raise LaunchConfigurationError("the active model profile is not Heartwood-managed")
        updated = replace(
            profile,
            max_input_tokens=input_capacity,
            max_output_tokens=output_budget,
        )
        return config.with_model_settings(config.model_settings.with_profile(updated))

    store.update(update)


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
                    "import torch, vllm; from importlib.metadata import version; "
                    "cuda = torch.version.cuda or 'none'; "
                    "available = torch.cuda.is_available(); "
                    "assert available, "
                    "f'CUDA is unavailable to PyTorch {torch.__version__} "
                    "(built for CUDA {cuda})'; "
                    "torch.cuda.init(); "
                    "print(version('vllm'), torch.__version__, cuda)"
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
        if executable.name == "heartwood-vllm":
            try:
                compatibility = subprocess.run(
                    (str(executable), "__heartwood_verify_runtime__"),
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    env=dict(env),
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                return str(error)
            if compatibility.returncode != 0:
                detail = (
                    compatibility.stderr.strip()
                    or compatibility.stdout.strip()
                    or "runtime compatibility check failed"
                )
                return detail.splitlines()[-1]
        else:
            installed_version = completed.stdout.split(maxsplit=1)[0]
            try:
                secured_upstream = Version(installed_version) >= Version("0.11.1")
            except InvalidVersion:
                secured_upstream = False
            if not secured_upstream:
                return (
                    "the selected vLLM executable must be Heartwood's secured launcher "
                    "or vLLM 0.11.1 or newer"
                )
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
    if copied >= total:
        status = "complete"
    elif elapsed >= 5 and copied >= 64 * 1024 * 1024 and rate > 0:
        status = f"about {int(remaining)} seconds remaining"
    else:
        status = "calculating remaining time"
    print(
        f"Model staging: {percent:5.1f}% "
        f"({_format_bytes(copied)} of {_format_bytes(total)}, "
        f"{status})"
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
