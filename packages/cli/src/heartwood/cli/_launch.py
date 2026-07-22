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
from typing import Literal, cast

from packaging.version import InvalidVersion, Version

from heartwood.adapters.platform import select_platform_adapter
from heartwood.cli._model_snapshot import verify_snapshot
from heartwood.gateway import (
    LocalContextPlan,
    ModelSnapshot,
    ProjectConfig,
    ProjectConfigStore,
    ProjectContext,
    SessionGateway,
    automatic_model_tier,
    discover_slurm_gpu_partitions,
    estimate_local_runtime_memory,
    inspect_gpu_environment,
    managed_model_token_budgets,
    minimum_compute_capability_for_model,
    plan_local_context_window,
    verify_model_artifact,
)

InputFunction = Callable[[str], str]
RunFunction = Callable[[Sequence[str]], int]

_VLLM_EAGER_MEMORY_PER_GPU = 20 * 1024**3
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
)


class LaunchConfigurationError(ValueError):
    """Raised when a launch cannot be planned without guessing."""


@dataclass(frozen=True, slots=True)
class LaunchOptions:
    """Validated launch inputs shared by platform implementations."""

    project: ProjectContext
    session_id: str
    partition: str | None
    gpus: int | None
    cpus: int | None
    memory: str | None
    time_limit: str
    task_profile: Literal["auto", "standard", "powerful", "maximum"]
    dry_run: bool
    no_allocate: bool
    yes_request_allocation: bool
    yes_download: bool
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
    artifact_id: str | None
    context_window: int | None
    partition: str | None = None
    gpus: int | None = None
    cpus: int | None = None
    memory: str | None = None
    model_size_bytes: int | None = None
    model_tier: str | None = None
    model_precision: str | None = None
    model_qualification: str | None = None
    startup_seconds_min: int | None = None
    startup_seconds_max: int | None = None
    environment_notes: tuple[str, ...] = ()
    download_required: bool = False

    def format(self) -> str:
        """Render the launch proposal without secrets."""
        compute = "Slurm allocation required" if self.allocation_required else "already provisioned"
        lines = [
            "Heartwood launch plan",
            "",
            f"Platform: {self.platform_id}",
            f"Compute: {compute}",
            f"Model: {self.model_root if self.model_root is not None else 'not selected'}",
            (
                "Catalog model: "
                f"{self.artifact_id if self.artifact_id is not None else 'not selected'}"
            ),
            f"Runtime: {self.runtime if self.runtime is not None else 'not selected'}",
            (
                f"Context capacity: up to {self.context_window:,} tokens"
                if self.context_window
                else "Context capacity: not selected"
            ),
            f"State: {self.state_root}",
            f"Project: {self.project_root}",
        ]
        if self.model_tier is not None:
            lines.append(f"Capability: {self.model_tier.title()}")
        if self.model_precision is not None:
            lines.append(f"Precision: {self.model_precision}")
        if self.model_qualification is not None:
            label = (
                "Qualified" if self.model_qualification == "qualified" else "Evaluation candidate"
            )
            lines.append(f"Qualification: {label}")
        if self.model_size_bytes is not None:
            lines.append(f"Model download: {_format_bytes(self.model_size_bytes)}")
        if self.startup_seconds_min is not None and self.startup_seconds_max is not None:
            lines.append(
                "Expected startup: "
                f"{_format_duration(self.startup_seconds_min)} to "
                f"{_format_duration(self.startup_seconds_max)}"
            )
        if self.partition is not None:
            lines.append(f"GPU partition: {self.partition}")
        if self.gpus is not None:
            lines.append(f"GPU allocation: {self.gpus}")
        if self.cpus is not None:
            lines.append(f"CPU allocation: {self.cpus}")
        if self.memory is not None:
            lines.append(f"RAM allocation: {self.memory}")
        if self.allocation_command:
            lines.append(f"Request: {shlex.join(self.allocation_command)}")
        lines.extend(f"Resource check: {note}" for note in self.environment_notes)
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class LocalRuntimeSelection:
    """Persisted local-model selection normalized for runtime launch."""

    artifact_id: str
    model_root: Path
    runtime: Literal["llama-cpp", "vllm"]
    model_id: str
    size_bytes: int | None
    artifact_sha256: str | None
    context_window: int
    maximum_context_window: int
    tier: Literal["standard", "powerful", "maximum"]
    precision: str
    qualification: Literal["candidate", "qualified"]
    minimum_gpu_count: int
    minimum_gpu_memory_bytes: int
    recommended_ram_bytes: int | None
    recommended_disk_bytes: int | None
    tool_call_parser: Literal["hermes", "openai", "qwen3_coder"] | None
    tensor_parallel_size: int
    startup_seconds_min: int
    startup_seconds_max: int
    catalog_source: Literal["catalog", "user-selected"]


def _allocation_resources(
    options: LaunchOptions,
    selection: LocalRuntimeSelection | None,
) -> tuple[int, int, str]:
    if selection is None or selection.runtime == "llama-cpp":
        gpus = options.gpus or 1
    else:
        gpus = options.gpus or selection.tensor_parallel_size
        if gpus != selection.tensor_parallel_size:
            raise LaunchConfigurationError(
                f"{selection.model_id} was qualified with "
                f"{selection.tensor_parallel_size} GPU(s); choose a catalog model configured "
                f"for {gpus} GPU(s) instead"
            )
        if gpus < selection.minimum_gpu_count:
            raise LaunchConfigurationError(
                f"{selection.model_id} requires at least {selection.minimum_gpu_count} GPU(s)"
            )
    cpus = options.cpus or max(8, gpus * 8)
    recommended_memory = selection.recommended_ram_bytes if selection is not None else None
    memory = options.memory or (
        f"{_ceil_gib(recommended_memory)}G" if recommended_memory is not None else "64G"
    )
    return gpus, cpus, memory


def _validate_gpu_environment(
    platform_id: str,
    selection: LocalRuntimeSelection | None,
    env: Mapping[str, str],
    *,
    allocation_required: bool,
) -> tuple[str, ...]:
    if selection is None or selection.runtime != "vllm":
        return ()
    environment = inspect_gpu_environment(platform_id, env)
    available, reason = environment.assess(
        gpu_count=selection.tensor_parallel_size,
        gpu_memory_bytes=selection.minimum_gpu_memory_bytes,
        minimum_compute_capability=_minimum_compute_capability(selection),
    )
    if not available:
        raise LaunchConfigurationError(reason)
    notes = [reason]
    if allocation_required:
        notes.append("The exact devices and NVIDIA driver will be checked inside the allocation.")
    elif environment.visible_devices:
        drivers = ", ".join(
            sorted({device.driver_version for device in environment.visible_devices})
        )
        notes.append(f"NVIDIA driver: {drivers}; Heartwood runtime ABI: CUDA 12.9")
    return tuple(notes)


def _minimum_compute_capability(
    selection: LocalRuntimeSelection,
) -> tuple[int, int] | None:
    """Return the minimum GPU generation required by reviewed quantization paths."""
    if selection.runtime != "vllm":
        return None
    return minimum_compute_capability_for_model(
        model_id=selection.model_id,
        precision=selection.precision,
    )


def _recommend_model(
    options: LaunchOptions,
    env: Mapping[str, str],
    *,
    platform_id: str,
) -> ModelSnapshot | None:
    task_profile = options.task_profile
    if task_profile == "auto":
        task_profile = automatic_model_tier(platform_id)
    gateway = SessionGateway(project=options.project, env=env)
    try:
        gpu_environment = gateway.gpu_environment()
        maximum_gpu_count = max(
            (capacity.gpu_count for capacity in gpu_environment.capacities),
            default=0,
        )
        if options.gpus is not None and options.gpus > maximum_gpu_count:
            raise LaunchConfigurationError(
                f"{options.gpus} GPU(s) were requested, but only {maximum_gpu_count} compatible "
                "GPU(s) were detected"
            )
        return gateway.recommend_managed_model(
            maximum_tier=task_profile,
            requested_gpus=options.gpus,
            gpu_environment=gpu_environment,
        )
    finally:
        gateway.stop()


def _selection_from_snapshot(
    snapshot: ModelSnapshot,
    project: ProjectContext,
) -> LocalRuntimeSelection:
    return LocalRuntimeSelection(
        artifact_id=snapshot.snapshot_id,
        model_root=project.models_dir / snapshot.snapshot_id,
        runtime="vllm",
        model_id="heartwood-managed-model",
        size_bytes=snapshot.expected_size_bytes,
        artifact_sha256=None,
        context_window=snapshot.context_window,
        maximum_context_window=snapshot.maximum_context_window,
        tier=snapshot.tier,
        precision=snapshot.precision,
        qualification=snapshot.qualification,
        minimum_gpu_count=snapshot.minimum_gpu_count,
        minimum_gpu_memory_bytes=snapshot.minimum_gpu_memory_bytes,
        recommended_ram_bytes=snapshot.recommended_ram_bytes,
        recommended_disk_bytes=snapshot.recommended_disk_bytes,
        tool_call_parser=snapshot.tool_call_parser,
        tensor_parallel_size=snapshot.tensor_parallel_size,
        startup_seconds_min=snapshot.startup_seconds_min,
        startup_seconds_max=snapshot.startup_seconds_max,
        catalog_source="catalog",
    )


def build_launch_plan(options: LaunchOptions, env: Mapping[str, str]) -> LaunchPlan:
    """Build a platform-specific launch plan without changing external state."""
    platform_id = select_platform_adapter(env).adapter_id
    selection = _local_model_selection(options.project, env)
    if selection is None:
        recommendation = _recommend_model(options, env, platform_id=platform_id)
        if recommendation is not None:
            selection = _selection_from_snapshot(recommendation, options.project)
    gpus, cpus, memory = _allocation_resources(options, selection)
    allocation_required = platform_id == "carina" and not env.get("SLURM_JOB_ID")
    environment_notes = _validate_gpu_environment(
        platform_id,
        selection,
        env,
        allocation_required=allocation_required,
    )
    command: tuple[str, ...] = ()
    partition: str | None = None
    if allocation_required:
        partition = _resolve_slurm_partition(
            options.partition,
            env,
            required_gpus=gpus,
            required_gpu_memory_bytes=(
                selection.minimum_gpu_memory_bytes if selection is not None else 0
            ),
        )
        command = (
            "srun",
            "--pty",
            f"--partition={partition}",
            f"--gres=gpu:{gpus}",
            f"--cpus-per-task={cpus}",
            f"--mem={memory}",
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
        selection.artifact_id if selection is not None else None,
        selection.context_window if selection is not None else None,
        partition=partition,
        gpus=gpus if allocation_required else None,
        cpus=cpus if allocation_required else None,
        memory=memory if allocation_required else None,
        model_size_bytes=selection.size_bytes if selection is not None else None,
        model_tier=selection.tier if selection is not None else None,
        model_precision=selection.precision if selection is not None else None,
        model_qualification=(selection.qualification if selection is not None else None),
        startup_seconds_min=(selection.startup_seconds_min if selection is not None else None),
        startup_seconds_max=(selection.startup_seconds_max if selection is not None else None),
        environment_notes=environment_notes,
        download_required=(
            selection is not None
            and selection.catalog_source == "catalog"
            and not selection.model_root.exists()
        ),
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
        if plan.artifact_id is None:
            print(
                "\nNo qualified Heartwood-managed model matches the detected resources. "
                "Run `heartwood models managed` for lower-resource and advanced options."
            )
            return 64
        if plan.download_required:
            if not active_options.yes_download:
                try:
                    approved = (
                        input_fn("\nDownload this pinned model into .heartwood/models? [y/N]: ")
                        .strip()
                        .lower()
                        == "y"
                    )
                except EOFError:
                    approved = False
                if not approved:
                    print("Model download cancelled; no allocation was requested.")
                    return 1
            print("\nDownloading the reviewed model snapshot. This can take several minutes.")
            gateway = SessionGateway(project=active_options.project, env=active_env)
            try:
                gateway.download_local_model_now(
                    plan.artifact_id,
                    progress_callback=_progress_reporter("Model download"),
                )
            except (OSError, ValueError) as error:
                print(f"Model download failed: {error}")
                return 74
            finally:
                gateway.stop()
            try:
                plan = build_launch_plan(active_options, active_env)
            except LaunchConfigurationError as error:
                print(f"Launch configuration error after model download: {error}")
                return 64
            print("\nModel ready. Updated launch plan:\n")
            print(plan.format())
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
                    runtime_executable,
                    staged_source,
                    selection,
                    enforce_eager=_use_eager_vllm(selection, runtime_env),
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
        runtime_env["HEARTWOOD_LOCAL_RUNTIME_ARTIFACT_ID"] = selection.artifact_id
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
    executable: Path,
    model: Path,
    selection: LocalRuntimeSelection,
    *,
    enforce_eager: bool = False,
) -> tuple[str, ...]:
    if selection.runtime == "llama-cpp":
        model_file = _gguf_file(model)
        return (
            str(executable),
            "--model",
            str(model_file),
            "--jinja",
            "--alias",
            selection.model_id,
            "--host",
            "127.0.0.1",
            "--port",
            "8765",
            "--ctx-size",
            str(selection.context_window),
        )
    if selection.tool_call_parser is None:  # pragma: no cover - persisted invariant
        raise LaunchConfigurationError("the selected vLLM model has no tool-call parser")
    command = (
        str(executable),
        "serve",
        str(model),
        "--host",
        "127.0.0.1",
        "--port",
        "8765",
        "--served-model-name",
        selection.model_id,
        "--max-model-len",
        str(selection.context_window),
        "--tensor-parallel-size",
        str(selection.tensor_parallel_size),
        "--enable-auto-tool-choice",
        "--tool-call-parser",
        selection.tool_call_parser,
    )
    return (*command, "--enforce-eager") if enforce_eager else command


def _use_eager_vllm(
    selection: LocalRuntimeSelection,
    env: Mapping[str, str],
) -> bool:
    """Avoid CUDA graph capture overhead on constrained NVIDIA GPUs."""
    if selection.runtime != "vllm":
        return False
    available = _available_gpu_memory_bytes(env, count=selection.tensor_parallel_size)
    if available is None:
        return False
    per_device_available = available // selection.tensor_parallel_size
    return per_device_available <= _VLLM_EAGER_MEMORY_PER_GPU


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
        artifact_id=selection.artifact_id,
        model_root=model,
        runtime=runtime,
        model_id=selection.model_id,
        size_bytes=selection.size_bytes,
        artifact_sha256=selection.artifact_sha256,
        context_window=selection.context_window,
        maximum_context_window=selection.maximum_context_window,
        tier=cast(Literal["standard", "powerful", "maximum"], selection.tier),
        precision=selection.precision or "Unspecified",
        qualification=cast(Literal["candidate", "qualified"], selection.qualification),
        minimum_gpu_count=selection.minimum_gpu_count,
        minimum_gpu_memory_bytes=selection.minimum_gpu_memory_bytes,
        recommended_ram_bytes=selection.recommended_ram_bytes,
        recommended_disk_bytes=selection.recommended_disk_bytes,
        tool_call_parser=cast(
            Literal["hermes", "openai", "qwen3_coder"] | None,
            selection.tool_call_parser,
        ),
        tensor_parallel_size=selection.tensor_parallel_size,
        startup_seconds_min=selection.startup_seconds_min,
        startup_seconds_max=selection.startup_seconds_max,
        catalog_source=cast(
            Literal["catalog", "user-selected"],
            selection.catalog_source,
        ),
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
        gpu_available = _available_gpu_memory_bytes(
            env,
            count=selection.tensor_parallel_size,
        )
        _print_memory_result("GPU memory", gpu_required, gpu_available)
        if _use_eager_vllm(selection, env):
            print(
                "Runtime mode: eager execution selected to avoid CUDA graph capture "
                "overhead on the available GPU memory."
            )


def _context_plan(
    selection: LocalRuntimeSelection,
    env: Mapping[str, str],
) -> LocalContextPlan:
    model_bytes = selection.size_bytes
    if model_bytes is None and selection.model_root.exists():
        model_bytes = _model_size(selection.model_root)
    available = (
        _available_gpu_memory_bytes(env, count=selection.tensor_parallel_size)
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


def _available_gpu_memory_bytes(
    env: Mapping[str, str],
    *,
    count: int = 1,
) -> int | None:
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
    if len(values) < count:
        return sum(values) if values else None
    return sum(sorted(values, reverse=True)[:count])


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
        "HEARTWOOD_LOCAL_RUNTIME_ARTIFACT_ID",
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


def _resolve_slurm_partition(
    requested: str | None,
    env: Mapping[str, str],
    *,
    required_gpus: int = 1,
    required_gpu_memory_bytes: int = 0,
) -> str:
    partitions = discover_slurm_gpu_partitions(env)
    eligible = tuple(
        partition
        for partition in partitions
        if partition.gpu_count >= required_gpus
        and (
            partition.gpu_memory_bytes is None
            or partition.gpu_memory_bytes >= required_gpu_memory_bytes
        )
    )
    if requested:
        requested_partitions = tuple(
            partition for partition in partitions if partition.name == requested
        )
        if partitions and not requested_partitions:
            available = ", ".join(sorted({partition.name for partition in partitions}))
            raise LaunchConfigurationError(
                f"GPU partition {requested!r} is unavailable; choose one of: {available}"
            )
        if requested_partitions and not any(
            partition.gpu_count >= required_gpus
            and (
                partition.gpu_memory_bytes is None
                or partition.gpu_memory_bytes >= required_gpu_memory_bytes
            )
            for partition in requested_partitions
        ):
            available_count = max(partition.gpu_count for partition in requested_partitions)
            raise LaunchConfigurationError(
                f"GPU partition {requested!r} exposes at most {available_count} GPU(s) or "
                "insufficient per-device memory for the selected model"
            )
        return requested
    for partition in eligible:
        if partition.is_default:
            return partition.name
    eligible_names = tuple(dict.fromkeys(partition.name for partition in eligible))
    if len(eligible_names) == 1:
        return eligible_names[0]
    if eligible:
        available = ", ".join(eligible_names)
        raise LaunchConfigurationError(
            f"no default GPU partition was detected; choose one of: {available}"
        )
    if partitions:
        largest = max(partition.gpu_count for partition in partitions)
        raise LaunchConfigurationError(
            f"available GPU partitions expose at most {largest} GPU(s); "
            f"the selected model requires {required_gpus}"
        )
    raise LaunchConfigurationError("no available GPU partition was detected; pass --partition")


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


def _ceil_gib(value: int) -> int:
    return max(1, (value + 1024**3 - 1) // 1024**3)


def _format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} seconds"
    minutes = (seconds + 59) // 60
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


def _progress_reporter(label: str) -> Callable[[int, int], None]:
    started = time.monotonic()
    last_report = -5.0

    def report(completed: int, total: int) -> None:
        nonlocal last_report
        now = time.monotonic()
        if completed < total and now - last_report < 5:
            return
        last_report = now
        elapsed = max(now - started, 0.001)
        percent = completed / total * 100 if total > 0 else 0.0
        rate = completed / elapsed
        remaining = (total - completed) / rate if rate > 0 and total > completed else 0
        timing = (
            f", about {_format_duration(max(1, int(remaining)))} remaining"
            if remaining > 0 and elapsed >= 5
            else ""
        )
        print(
            f"{label}: {percent:5.1f}% "
            f"({_format_bytes(completed)} of {_format_bytes(total)}{timing})"
        )

    return report


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
