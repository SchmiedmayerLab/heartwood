# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Shared NVIDIA and Slurm resource discovery for managed model planning."""

from __future__ import annotations

import csv
import io
import re
import shutil
import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from packaging.version import InvalidVersion, Version

_CUDA_12_MINIMUM_DRIVER = Version("525.60.13")
_KNOWN_GPU_MEMORY_BYTES = {
    "nvidia_l40s": 48_000_000_000,
    "l40s": 48_000_000_000,
    "tesla_t4": 16_000_000_000,
    "t4": 16_000_000_000,
}
_KNOWN_COMPUTE_CAPABILITIES = {
    "l40s": (8, 9),
    "p4": (6, 1),
    "p100": (6, 0),
    "t4": (7, 5),
    "v100": (7, 0),
}


@dataclass(frozen=True, slots=True)
class GpuDevice:
    """One NVIDIA device visible to the current process."""

    index: int
    name: str
    total_memory_bytes: int
    free_memory_bytes: int
    driver_version: str
    compute_capability: tuple[int, int] | None

    @property
    def modern_vllm_issue(self) -> str | None:
        """Return why the CUDA 12.9 runtime must not start on this device."""
        capability = self.compute_capability or _capability_from_name(self.name)
        if capability is not None and capability < (7, 5):
            return (
                f"{self.name} has compute capability {capability[0]}.{capability[1]}; "
                "the vLLM CUDA 12.9 runtime requires 7.5 or newer"
            )
        try:
            driver = Version(self.driver_version)
        except InvalidVersion:
            return f"NVIDIA driver version could not be interpreted: {self.driver_version}"
        if driver < _CUDA_12_MINIMUM_DRIVER:
            return (
                f"NVIDIA driver {self.driver_version} is older than the CUDA 12.x "
                f"compatibility minimum {_CUDA_12_MINIMUM_DRIVER}"
            )
        return None


@dataclass(frozen=True, slots=True)
class SlurmGpuPartition:
    """GPU capacity reported for one available Slurm partition."""

    name: str
    is_default: bool
    gpu_model: str | None
    gpu_count: int
    node_memory_bytes: int | None
    node_cpu_count: int | None
    state: str

    @property
    def gpu_memory_bytes(self) -> int | None:
        """Return reviewed per-device memory for known scheduler GPU names."""
        if self.gpu_model is None:
            return None
        normalized = re.sub(r"[^a-z0-9]+", "_", self.gpu_model.casefold()).strip("_")
        return _KNOWN_GPU_MEMORY_BYTES.get(normalized)


@dataclass(frozen=True, slots=True)
class GpuCapacity:
    """One visible or schedulable GPU resource envelope."""

    label: str
    gpu_model: str
    gpu_count: int
    gpu_memory_bytes: int
    allocation_required: bool
    partition: str | None = None
    compute_capability: tuple[int, int] | None = None


@dataclass(frozen=True, slots=True)
class GpuEnvironment:
    """GPU inventory used by every Heartwood interaction surface."""

    platform_id: str
    visible_devices: tuple[GpuDevice, ...]
    slurm_partitions: tuple[SlurmGpuPartition, ...]
    capacities: tuple[GpuCapacity, ...]

    def assess(
        self,
        *,
        gpu_count: int,
        gpu_memory_bytes: int,
        minimum_compute_capability: tuple[int, int] | None = None,
    ) -> tuple[bool, str]:
        """Explain whether the inventory can run one catalog configuration."""
        eligible = tuple(
            capacity
            for capacity in self.capacities
            if capacity.gpu_count >= gpu_count
            and capacity.gpu_memory_bytes >= gpu_memory_bytes
            and _meets_compute_capability(capacity, minimum_compute_capability)
        )
        if eligible:
            capacity = min(
                eligible,
                key=lambda item: (
                    item.allocation_required,
                    item.gpu_count,
                    item.gpu_memory_bytes,
                    item.label,
                ),
            )
            if capacity.allocation_required:
                return (
                    True,
                    f"Available through Slurm partition {capacity.partition} with "
                    f"{capacity.gpu_count} {capacity.gpu_model} GPU(s); allocation approval "
                    "is required before startup.",
                )
            return (
                True,
                f"Compatible with {capacity.gpu_count} visible {capacity.gpu_model} GPU(s).",
            )
        issues = tuple(
            dict.fromkeys(
                issue
                for device in self.visible_devices
                if (issue := device.modern_vllm_issue) is not None
            )
        )
        if issues:
            return False, "; ".join(issues)
        if minimum_compute_capability is not None:
            capability = ".".join(str(part) for part in minimum_compute_capability)
            insufficient = tuple(
                capacity
                for capacity in self.capacities
                if capacity.gpu_count >= gpu_count
                and capacity.gpu_memory_bytes >= gpu_memory_bytes
                and capacity.compute_capability is not None
                and capacity.compute_capability < minimum_compute_capability
            )
            if insufficient:
                observed = ", ".join(
                    sorted(
                        {
                            (
                                f"{capacity.gpu_model} "
                                f"{capacity.compute_capability[0]}."
                                f"{capacity.compute_capability[1]}"
                            )
                            for capacity in insufficient
                            if capacity.compute_capability is not None
                        }
                    )
                )
                return (
                    False,
                    f"Requires GPU compute capability {capability} or newer; detected {observed}.",
                )
            if self.capacities:
                return (
                    False,
                    f"Requires GPU compute capability {capability} or newer, but the detected "
                    "GPU capability could not be verified before startup.",
                )
        if not self.capacities:
            if self.platform_id == "terra":
                return (
                    False,
                    "No compatible NVIDIA GPU is visible. Recreate the Terra cloud environment "
                    "with an NVIDIA T4 and the Heartwood GPU image.",
                )
            if self.platform_id == "carina":
                return False, "No compatible Slurm GPU capacity was reported by Carina."
            return False, "No compatible NVIDIA GPU is visible to Heartwood."
        largest_count = max(capacity.gpu_count for capacity in self.capacities)
        largest_memory = max(capacity.gpu_memory_bytes for capacity in self.capacities)
        return (
            False,
            f"Requires {gpu_count} GPU(s) with {_format_bytes(gpu_memory_bytes)} each; "
            f"the detected capacity provides at most {largest_count} GPU(s) with "
            f"{_format_bytes(largest_memory)} each.",
        )


def inspect_gpu_environment(platform_id: str, env: Mapping[str, str]) -> GpuEnvironment:
    """Inspect visible devices or, on Carina login nodes, schedulable devices."""
    visible = discover_visible_gpus(env)
    inside_slurm = bool(env.get("SLURM_JOB_ID"))
    partitions = (
        discover_slurm_gpu_partitions(env) if platform_id == "carina" and not inside_slurm else ()
    )
    capacities = _visible_capacities(visible) if visible else _slurm_capacities(partitions)
    return GpuEnvironment(
        platform_id=platform_id,
        visible_devices=visible,
        slurm_partitions=partitions,
        capacities=capacities,
    )


def minimum_compute_capability_for_model(
    *,
    model_id: str,
    precision: str,
) -> tuple[int, int] | None:
    """Return the minimum GPU generation required by reviewed quantization paths."""
    normalized_model = model_id.casefold().replace("_", "-")
    normalized_precision = precision.casefold().replace("_", "-")
    if "gpt-oss" in normalized_model or "mxfp4" in normalized_precision:
        return (8, 0)
    if normalized_precision.startswith("fp8"):
        return (8, 9)
    return None


def discover_visible_gpus(env: Mapping[str, str]) -> tuple[GpuDevice, ...]:
    """Inspect visible NVIDIA devices without initializing CUDA."""
    executable = shutil.which("nvidia-smi", path=env.get("PATH"))
    if executable is None:
        return ()
    fields = "index,name,memory.total,memory.free,driver_version,compute_cap"
    completed = _run_nvidia_smi(executable, fields, env)
    has_capability = completed is not None and completed.returncode == 0
    if not has_capability:
        fields = "index,name,memory.total,memory.free,driver_version"
        completed = _run_nvidia_smi(executable, fields, env)
    if completed is None or completed.returncode != 0:
        return ()
    devices: list[GpuDevice] = []
    for row in csv.reader(io.StringIO(completed.stdout), skipinitialspace=True):
        if len(row) != (6 if has_capability else 5):
            continue
        try:
            capability = _parse_compute_capability(row[5]) if has_capability else None
            devices.append(
                GpuDevice(
                    index=int(row[0].strip()),
                    name=row[1].strip(),
                    total_memory_bytes=int(row[2].strip()) * 1024**2,
                    free_memory_bytes=int(row[3].strip()) * 1024**2,
                    driver_version=row[4].strip(),
                    compute_capability=capability,
                )
            )
        except ValueError:
            continue
    return tuple(sorted(devices, key=lambda device: device.index))


def discover_slurm_gpu_partitions(env: Mapping[str, str]) -> tuple[SlurmGpuPartition, ...]:
    """Return available GPU partitions and their node-level capacity."""
    try:
        completed = subprocess.run(
            ("sinfo", "--noheader", "--format=%P|%G|%a|%m|%c"),
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
    partitions: dict[tuple[str, str | None], SlurmGpuPartition] = {}
    for line in completed.stdout.splitlines():
        fields = line.strip().split("|")
        if len(fields) != 5:
            continue
        raw_name, resources, state, memory, cpus = fields
        gpu_model, gpu_count = _parse_gres(resources)
        if gpu_count < 1 or state.casefold() not in {"alloc", "idle", "mix", "up"}:
            continue
        name = raw_name.rstrip("*")
        if not name:
            continue
        candidate = SlurmGpuPartition(
            name=name,
            is_default=raw_name.endswith("*"),
            gpu_model=gpu_model,
            gpu_count=gpu_count,
            node_memory_bytes=_parse_slurm_memory(memory),
            node_cpu_count=_parse_positive_int(cpus),
            state=state.casefold(),
        )
        key = (candidate.name, candidate.gpu_model)
        previous = partitions.get(key)
        if previous is None or candidate.gpu_count > previous.gpu_count:
            partitions[key] = candidate
    return tuple(partitions.values())


def _visible_capacities(devices: tuple[GpuDevice, ...]) -> tuple[GpuCapacity, ...]:
    compatible = tuple(device for device in devices if device.modern_vllm_issue is None)
    thresholds = sorted({device.total_memory_bytes for device in compatible}, reverse=True)
    capacities: list[GpuCapacity] = []
    for threshold in thresholds:
        eligible = tuple(device for device in compatible if device.total_memory_bytes >= threshold)
        names = ", ".join(sorted({device.name for device in eligible}))
        capacities.append(
            GpuCapacity(
                label=f"{len(eligible)} visible {names} GPU(s)",
                gpu_model=names,
                gpu_count=len(eligible),
                gpu_memory_bytes=threshold,
                allocation_required=False,
                compute_capability=_minimum_capability(
                    device.compute_capability or _capability_from_name(device.name)
                    for device in eligible
                ),
            )
        )
    return tuple(capacities)


def _slurm_capacities(
    partitions: tuple[SlurmGpuPartition, ...],
) -> tuple[GpuCapacity, ...]:
    return tuple(
        GpuCapacity(
            label=f"Slurm partition {partition.name}",
            gpu_model=partition.gpu_model or "NVIDIA",
            gpu_count=partition.gpu_count,
            gpu_memory_bytes=partition.gpu_memory_bytes,
            allocation_required=True,
            partition=partition.name,
            compute_capability=_capability_from_name(partition.gpu_model or ""),
        )
        for partition in partitions
        if partition.gpu_memory_bytes is not None
    )


def _format_bytes(value: int) -> str:
    return f"{value / 1024**3:.1f} GiB"


def _run_nvidia_smi(
    executable: str,
    fields: str,
    env: Mapping[str, str],
) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            (
                executable,
                f"--query-gpu={fields}",
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


def _parse_compute_capability(value: str) -> tuple[int, int] | None:
    match = re.fullmatch(r"\s*(\d+)\.(\d+)\s*", value)
    return (int(match.group(1)), int(match.group(2))) if match is not None else None


def _capability_from_name(name: str) -> tuple[int, int] | None:
    normalized = name.casefold()
    return next(
        (
            capability
            for marker, capability in _KNOWN_COMPUTE_CAPABILITIES.items()
            if marker in normalized
        ),
        None,
    )


def _minimum_capability(
    capabilities: Iterable[tuple[int, int] | None],
) -> tuple[int, int] | None:
    values = tuple(
        capability
        for capability in capabilities
        if isinstance(capability, tuple)
        and len(capability) == 2
        and all(isinstance(part, int) for part in capability)
    )
    return min(values) if values else None


def _meets_compute_capability(
    capacity: GpuCapacity,
    minimum: tuple[int, int] | None,
) -> bool:
    if minimum is None:
        return True
    return capacity.compute_capability is not None and capacity.compute_capability >= minimum


def _parse_gres(value: str) -> tuple[str | None, int]:
    best_model: str | None = None
    best_count = 0
    for resource in value.split(","):
        fields = resource.strip().split("(", maxsplit=1)[0].split(":")
        if not fields or fields[0].casefold() != "gpu":
            continue
        if len(fields) == 2:
            model = None
            raw_count = fields[1]
        elif len(fields) >= 3:
            model = fields[1]
            raw_count = fields[2]
        else:
            continue
        count_match = re.match(r"(\d+)", raw_count)
        if count_match is None:
            continue
        count = int(count_match.group(1))
        if count > best_count:
            best_model = model
            best_count = count
    return best_model, best_count


def _parse_slurm_memory(value: str) -> int | None:
    match = re.fullmatch(r"\s*(\d+)([KMGT]?)\+?\s*", value, re.IGNORECASE)
    if match is None:
        return None
    amount = int(match.group(1))
    unit = match.group(2).upper()
    multiplier = {"": 1024**2, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}[unit]
    return amount * multiplier


def _parse_positive_int(value: str) -> int | None:
    match = re.match(r"\s*(\d+)", value)
    parsed = int(match.group(1)) if match is not None else 0
    return parsed if parsed > 0 else None


def _scheduler_environment(env: Mapping[str, str]) -> dict[str, str]:
    return {
        name: env[name]
        for name in ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE")
        if name in env
    }
