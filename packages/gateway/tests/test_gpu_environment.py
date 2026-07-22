# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import subprocess

import pytest

from heartwood.gateway._gpu_environment import (
    GpuCapacity,
    GpuDevice,
    GpuEnvironment,
    SlurmGpuPartition,
    discover_slurm_gpu_partitions,
    discover_visible_gpus,
    inspect_gpu_environment,
    minimum_compute_capability_for_model,
)


def test_visible_gpu_discovery_reports_t4_and_l40s_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "heartwood.gateway._gpu_environment.shutil.which",
        lambda *_args, **_kwargs: "/usr/bin/nvidia-smi",
    )
    monkeypatch.setattr(
        "heartwood.gateway._gpu_environment.subprocess.run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                "0, Tesla T4, 15109, 14000, 570.86.15, 7.5\n"
                "1, NVIDIA L40S, 46068, 45000, 570.86.15, 8.9\n"
            ),
        ),
    )

    devices = discover_visible_gpus({"PATH": "/usr/bin"})

    assert [device.name for device in devices] == ["Tesla T4", "NVIDIA L40S"]
    assert devices[0].total_memory_bytes == 15_109 * 1024**2
    assert devices[1].free_memory_bytes == 45_000 * 1024**2
    assert all(device.modern_vllm_issue is None for device in devices)


@pytest.mark.parametrize(
    ("device", "message"),
    [
        (
            GpuDevice(0, "Tesla P4", 8_000_000_000, 8_000_000_000, "570.86.15", (6, 1)),
            "requires 7.5 or newer",
        ),
        (
            GpuDevice(0, "Tesla V100", 16_000_000_000, 16_000_000_000, "570.86.15", None),
            "requires 7.5 or newer",
        ),
        (
            GpuDevice(0, "Tesla T4", 16_000_000_000, 16_000_000_000, "510.47.03", None),
            "older than the CUDA 12.x compatibility minimum",
        ),
    ],
)
def test_modern_vllm_compatibility_rejects_unsupported_devices(
    device: GpuDevice,
    message: str,
) -> None:
    assert message in str(device.modern_vllm_issue)


def test_modern_vllm_compatibility_rejects_unparseable_driver() -> None:
    device = GpuDevice(0, "NVIDIA T4", 16_000_000_000, 15_000_000_000, "unknown", None)

    assert device.modern_vllm_issue == "NVIDIA driver version could not be interpreted: unknown"


def test_visible_gpu_discovery_falls_back_when_compute_capability_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "heartwood.gateway._gpu_environment.shutil.which",
        lambda *_args, **_kwargs: "/usr/bin/nvidia-smi",
    )
    calls = 0

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return subprocess.CompletedProcess(command, 1, stdout="")
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="0, Tesla T4, 15109, 14000, 570.86.15\n",
        )

    monkeypatch.setattr("heartwood.gateway._gpu_environment.subprocess.run", run)

    devices = discover_visible_gpus({"PATH": "/usr/bin"})

    assert len(devices) == 1
    assert devices[0].compute_capability is None
    assert devices[0].modern_vllm_issue is None


def test_visible_gpu_discovery_rejects_p100_without_reported_compute_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "heartwood.gateway._gpu_environment.shutil.which",
        lambda *_args, **_kwargs: "/usr/bin/nvidia-smi",
    )
    calls = 0

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return subprocess.CompletedProcess(command, 1, stdout="")
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="0, Tesla P100-PCIE-16GB, 16280, 16000, 575.57.08\n",
        )

    monkeypatch.setattr("heartwood.gateway._gpu_environment.subprocess.run", run)

    devices = discover_visible_gpus({"PATH": "/usr/bin"})

    assert len(devices) == 1
    assert devices[0].compute_capability is None
    assert "requires 7.5 or newer" in str(devices[0].modern_vllm_issue)


def test_slurm_discovery_reports_partition_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "heartwood.gateway._gpu_environment.subprocess.run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                "dev*|gpu:nvidia_l40s:8(S:0-7)|up|515000|64\n"
                "long|gpu:nvidia_l40s:4|up|256G|32\n"
                "cpu|(null)|up|128G|16\n"
                "offline|gpu:nvidia_l40s:8|down|515000|64\n"
            ),
        ),
    )

    partitions = discover_slurm_gpu_partitions({"PATH": "/usr/bin"})

    assert [partition.name for partition in partitions] == ["dev", "long"]
    assert partitions[0].is_default is True
    assert partitions[0].gpu_model == "nvidia_l40s"
    assert partitions[0].gpu_count == 8
    assert partitions[0].gpu_memory_bytes == 48_000_000_000
    assert partitions[0].node_memory_bytes == 515_000 * 1024**2
    assert partitions[1].node_memory_bytes == 256 * 1024**3


def test_gpu_environment_assesses_visible_scheduler_and_insufficient_capacity() -> None:
    visible = GpuEnvironment(
        platform_id="terra",
        visible_devices=(),
        slurm_partitions=(),
        capacities=(GpuCapacity("one T4", "NVIDIA T4", 1, 16_000_000_000, False),),
    )
    compatible, reason = visible.assess(gpu_count=1, gpu_memory_bytes=15_000_000_000)
    assert compatible
    assert reason == "Compatible with 1 visible NVIDIA T4 GPU(s)."

    insufficient, reason = visible.assess(gpu_count=2, gpu_memory_bytes=42_000_000_000)
    assert not insufficient
    assert "Requires 2 GPU(s) with 39.1 GiB each" in reason
    assert "at most 1 GPU(s) with 14.9 GiB each" in reason

    scheduled = GpuEnvironment(
        platform_id="carina",
        visible_devices=(),
        slurm_partitions=(),
        capacities=(
            GpuCapacity(
                "Slurm partition dev",
                "NVIDIA L40S",
                8,
                48_000_000_000,
                True,
                "dev",
            ),
        ),
    )
    compatible, reason = scheduled.assess(gpu_count=4, gpu_memory_bytes=42_000_000_000)
    assert compatible
    assert "Slurm partition dev" in reason
    assert "allocation approval is required" in reason


def test_gpu_environment_rejects_models_requiring_newer_compute_capability() -> None:
    environment = GpuEnvironment(
        platform_id="terra",
        visible_devices=(),
        slurm_partitions=(),
        capacities=(
            GpuCapacity(
                "4 visible Tesla T4 GPU(s)",
                "Tesla T4",
                4,
                16_000_000_000,
                False,
                compute_capability=(7, 5),
            ),
        ),
    )

    compatible, reason = environment.assess(
        gpu_count=4,
        gpu_memory_bytes=15_000_000_000,
        minimum_compute_capability=(8, 0),
    )

    assert not compatible
    assert "Requires GPU compute capability 8.0 or newer" in reason
    assert "Tesla T4 7.5" in reason


def test_gpu_environment_accepts_scheduled_l40s_for_mxfp4() -> None:
    environment = GpuEnvironment(
        platform_id="carina",
        visible_devices=(),
        slurm_partitions=(),
        capacities=(
            GpuCapacity(
                "Slurm partition dev",
                "nvidia_l40s",
                8,
                48_000_000_000,
                True,
                "dev",
                (8, 9),
            ),
        ),
    )

    compatible, reason = environment.assess(
        gpu_count=2,
        gpu_memory_bytes=42_000_000_000,
        minimum_compute_capability=(8, 0),
    )

    assert compatible
    assert "Slurm partition dev" in reason


def test_gpu_environment_rejects_unverified_compute_capability_for_model_requirement() -> None:
    environment = GpuEnvironment(
        platform_id="generic",
        visible_devices=(),
        slurm_partitions=(),
        capacities=(
            GpuCapacity(
                "1 visible NVIDIA GPU",
                "NVIDIA GPU",
                1,
                48_000_000_000,
                False,
            ),
        ),
    )

    compatible, reason = environment.assess(
        gpu_count=1,
        gpu_memory_bytes=42_000_000_000,
        minimum_compute_capability=(8, 0),
    )

    assert not compatible
    assert "could not be verified before startup" in reason


@pytest.mark.parametrize(
    ("model_id", "precision", "expected"),
    [
        ("gpt-oss-20b-vllm", "MXFP4", (8, 0)),
        ("qwen3-coder-30b-a3b-instruct-fp8-vllm", "FP8", (8, 9)),
        ("qwen3-coder-30b-a3b-instruct-w4a16-awq-vllm", "W4A16 AWQ", None),
    ],
)
def test_model_quantization_declares_minimum_compute_capability(
    model_id: str,
    precision: str,
    expected: tuple[int, int] | None,
) -> None:
    assert minimum_compute_capability_for_model(model_id=model_id, precision=precision) == expected


@pytest.mark.parametrize(
    ("platform_id", "message"),
    [
        ("terra", "Recreate the Terra cloud environment with an NVIDIA T4"),
        ("carina", "No compatible Slurm GPU capacity was reported by Carina"),
        ("generic", "No compatible NVIDIA GPU is visible to Heartwood"),
    ],
)
def test_gpu_environment_explains_missing_capacity(platform_id: str, message: str) -> None:
    environment = GpuEnvironment(platform_id, (), (), ())

    compatible, reason = environment.assess(gpu_count=1, gpu_memory_bytes=1)

    assert not compatible
    assert message in reason


def test_gpu_environment_surfaces_incompatible_visible_devices() -> None:
    p4 = GpuDevice(0, "Tesla P4", 8_000_000_000, 8_000_000_000, "570.86.15", (6, 1))
    environment = GpuEnvironment("terra", (p4,), (), ())

    compatible, reason = environment.assess(gpu_count=1, gpu_memory_bytes=1)

    assert not compatible
    assert "Tesla P4 has compute capability 6.1" in reason


def test_environment_inspection_prefers_visible_devices_and_skips_slurm_in_allocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    t4 = GpuDevice(0, "NVIDIA T4", 16_000_000_000, 15_000_000_000, "570.86.15", (7, 5))
    monkeypatch.setattr(
        "heartwood.gateway._gpu_environment.discover_visible_gpus",
        lambda _env: (t4,),
    )
    discovered_slurm = False

    def discover(_env: object) -> tuple[SlurmGpuPartition, ...]:
        nonlocal discovered_slurm
        discovered_slurm = True
        return ()

    monkeypatch.setattr(
        "heartwood.gateway._gpu_environment.discover_slurm_gpu_partitions",
        discover,
    )

    environment = inspect_gpu_environment("carina", {"SLURM_JOB_ID": "123"})

    assert not discovered_slurm
    assert environment.capacities[0].gpu_count == 1
    assert environment.capacities[0].gpu_memory_bytes == 16_000_000_000


def test_environment_inspection_builds_distinct_mixed_gpu_capacities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    devices = (
        GpuDevice(0, "NVIDIA L40S", 48_000_000_000, 47_000_000_000, "570.86.15", (8, 9)),
        GpuDevice(1, "NVIDIA T4", 16_000_000_000, 15_000_000_000, "570.86.15", (7, 5)),
    )
    monkeypatch.setattr(
        "heartwood.gateway._gpu_environment.discover_visible_gpus",
        lambda _env: devices,
    )

    environment = inspect_gpu_environment("generic", {})

    assert [(item.gpu_count, item.gpu_memory_bytes) for item in environment.capacities] == [
        (1, 48_000_000_000),
        (2, 16_000_000_000),
    ]


def test_environment_inspection_builds_known_slurm_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    partition = SlurmGpuPartition("dev", True, "nvidia_l40s", 8, None, None, "up")
    monkeypatch.setattr(
        "heartwood.gateway._gpu_environment.discover_visible_gpus",
        lambda _env: (),
    )
    monkeypatch.setattr(
        "heartwood.gateway._gpu_environment.discover_slurm_gpu_partitions",
        lambda _env: (partition, SlurmGpuPartition("other", False, None, 2, None, None, "up")),
    )

    environment = inspect_gpu_environment("carina", {})

    assert environment.capacities == (
        GpuCapacity(
            "Slurm partition dev",
            "nvidia_l40s",
            8,
            48_000_000_000,
            True,
            "dev",
            (8, 9),
        ),
    )
    assert environment.slurm_partitions[1].gpu_memory_bytes is None


def test_slurm_discovery_accepts_generic_gpu_gres_and_invalid_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "heartwood.gateway._gpu_environment.subprocess.run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout="generic|gpu:2|up|unknown|8\n",
        ),
    )

    partitions = discover_slurm_gpu_partitions({"PATH": "/usr/bin"})

    assert partitions == (SlurmGpuPartition("generic", False, None, 2, None, 8, "up"),)


def test_slurm_discovery_returns_empty_when_sinfo_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "heartwood.gateway._gpu_environment.subprocess.run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 1, stdout=""),
    )

    assert discover_slurm_gpu_partitions({"PATH": "/usr/bin"}) == ()


def test_visible_gpu_discovery_handles_missing_malformed_and_failed_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("heartwood.gateway._gpu_environment.shutil.which", lambda *_a, **_k: None)
    assert discover_visible_gpus({}) == ()

    monkeypatch.setattr(
        "heartwood.gateway._gpu_environment.shutil.which",
        lambda *_args, **_kwargs: "/usr/bin/nvidia-smi",
    )
    monkeypatch.setattr(
        "heartwood.gateway._gpu_environment.subprocess.run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout="malformed\nnot-an-index, T4, x, y, driver, bad\n",
        ),
    )
    assert discover_visible_gpus({}) == ()

    def fail(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired("nvidia-smi", 10)

    monkeypatch.setattr("heartwood.gateway._gpu_environment.subprocess.run", fail)
    assert discover_visible_gpus({}) == ()


def test_slurm_discovery_ignores_malformed_rows_and_keeps_largest_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "heartwood.gateway._gpu_environment.subprocess.run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                "malformed\n"
                "*|gpu:nvidia_l40s:8|up|bad|bad\n"
                "dev|gpu|up|1T|0\n"
                "dev|gpu:nvidia_l40s:x|up|1T|32\n"
                "dev|gpu:nvidia_l40s:2,gpu:nvidia_l40s:4|idle|1T+|32\n"
                "dev|gpu:nvidia_l40s:2|alloc|512G|16\n"
            ),
        ),
    )

    partitions = discover_slurm_gpu_partitions(
        {"PATH": "/usr/bin", "HOME": "/home/test", "SECRET": "excluded"}
    )

    assert len(partitions) == 1
    assert partitions[0].gpu_count == 4
    assert partitions[0].node_memory_bytes == 1024**4
    assert partitions[0].node_cpu_count == 32


@pytest.mark.parametrize(
    "failure",
    [OSError("missing sinfo"), subprocess.TimeoutExpired("sinfo", 15)],
)
def test_slurm_discovery_handles_unavailable_scheduler(
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise failure

    monkeypatch.setattr("heartwood.gateway._gpu_environment.subprocess.run", fail)

    assert discover_slurm_gpu_partitions({}) == ()
