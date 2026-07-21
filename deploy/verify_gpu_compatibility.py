# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Verify that GPU runtime, model catalog, and qualification evidence agree."""

from __future__ import annotations

import argparse
import re
import tomllib
from pathlib import Path
from typing import Any

_SCHEMA = "heartwood.gpu-compatibility.v1"
_CATALOG_SCHEMA = "heartwood.model-snapshot-catalog.v2"
_QUALIFICATION_TEST = "heartwood.coding-agent-e2e.v1"
_MINIMUM_AGENT_CONTEXT_WINDOW = 18_432
_CONFIGURATION_FIELDS = {
    "configuration_id",
    "status",
    "platform",
    "gpu_model",
    "gpu_count",
    "minimum_gpu_memory_bytes",
    "model_snapshot",
    "model_repository",
    "model_revision",
    "precision",
    "context_window",
    "tensor_parallel_size",
    "tool_call_parser",
    "vllm_version",
    "pytorch_version",
    "cuda_version",
    "minimum_driver_version",
    "qualification_test",
    "startup_seconds_min",
    "startup_seconds_max",
}
_FORBIDDEN_CUDA_13 = (
    "cuda-tile==",
    "nvidia-cuda-crt==",
    "nvidia-cuda-nvcc==",
    "nvidia-cuda-runtime==",
    "nvidia-cuda-tileiras==",
    "nvidia-nvvm==",
    "-cu13",
)


class CompatibilityError(ValueError):
    """Raised when compatibility evidence is incomplete or contradictory."""


def verify_repository(root: Path) -> None:
    """Verify the repository's complete GPU compatibility contract."""
    matrix = _toml(root / "images/gpu/compatibility.toml")
    catalog = _toml(root / "images/generic/local-runtime/snapshots.toml")
    if matrix.get("schema_version") != _SCHEMA:
        raise CompatibilityError("unsupported GPU compatibility matrix schema")
    if catalog.get("schema_version") != _CATALOG_SCHEMA:
        raise CompatibilityError("unsupported model snapshot catalog schema")
    runtime = _mapping(matrix, "runtime")
    _verify_runtime_lock(root, runtime)
    snapshots = _mapping(catalog, "snapshots")
    configurations = matrix.get("configurations")
    if not isinstance(configurations, list) or not configurations:
        raise CompatibilityError("GPU compatibility matrix has no configurations")

    seen_ids: set[str] = set()
    covered_snapshots: set[str] = set()
    qualified_platforms: dict[str, set[str]] = {}
    for configuration in configurations:
        if not isinstance(configuration, dict):
            raise CompatibilityError("GPU compatibility configurations must be tables")
        missing = sorted(_CONFIGURATION_FIELDS - configuration.keys())
        if missing:
            raise CompatibilityError(
                f"GPU compatibility configuration is missing fields: {', '.join(missing)}"
            )
        configuration_id = _string(configuration, "configuration_id")
        if configuration_id in seen_ids:
            raise CompatibilityError(f"duplicate GPU configuration: {configuration_id}")
        seen_ids.add(configuration_id)
        status = _string(configuration, "status")
        if status not in {"candidate", "qualified"}:
            raise CompatibilityError(f"invalid GPU qualification status: {status}")
        snapshot_id = _string(configuration, "model_snapshot")
        snapshot = snapshots.get(snapshot_id)
        if not isinstance(snapshot, dict):
            raise CompatibilityError(f"unknown model snapshot in GPU matrix: {snapshot_id}")
        covered_snapshots.add(snapshot_id)
        _verify_configuration(configuration, snapshot, runtime)
        if status == "qualified":
            for field in ("validated_at", "validated_driver_version", "evidence"):
                _string(configuration, field)
            qualified_platforms.setdefault(snapshot_id, set()).add(
                _string(configuration, "platform")
            )

    if covered_snapshots != set(snapshots):
        missing = sorted(set(snapshots) - covered_snapshots)
        raise CompatibilityError(
            f"GPU catalog snapshots are absent from the matrix: {', '.join(missing)}"
        )
    for snapshot_id, snapshot in snapshots.items():
        if not isinstance(snapshot, dict):
            raise CompatibilityError(f"invalid model snapshot: {snapshot_id}")
        qualified = snapshot.get("qualification") == "qualified"
        platforms = set(_string_list(snapshot, "validated_platforms"))
        if qualified != bool(qualified_platforms.get(snapshot_id)):
            raise CompatibilityError(
                f"model qualification and compatibility evidence disagree: {snapshot_id}"
            )
        if platforms != qualified_platforms.get(snapshot_id, set()):
            raise CompatibilityError(
                f"validated platforms disagree with compatibility evidence: {snapshot_id}"
            )
        if snapshot.get("recommended", False) and not qualified:
            raise CompatibilityError(
                f"only qualified catalog models may be recommended: {snapshot_id}"
            )


def _verify_runtime_lock(root: Path, runtime: dict[str, Any]) -> None:
    lock = (root / "images/gpu/vllm-requirements.txt").read_text(encoding="utf-8")
    expected = (
        f"vllm-{_base_version(_string(runtime, 'vllm_version'))}%2Bcu129",
        f"torch-{_base_version(_string(runtime, 'pytorch_version'))}%2Bcu129",
        f"torchaudio-{_base_version(_string(runtime, 'torchaudio_version'))}%2Bcu129",
        f"torchvision-{_base_version(_string(runtime, 'torchvision_version'))}%2Bcu129",
    )
    missing = [item for item in expected if item not in lock]
    if missing:
        raise CompatibilityError(f"GPU lock is missing runtime wheels: {', '.join(missing)}")
    forbidden = [item for item in _FORBIDDEN_CUDA_13 if item in lock.casefold()]
    if forbidden:
        raise CompatibilityError(
            f"GPU lock contains unqualified CUDA 13 dependencies: {', '.join(forbidden)}"
        )
    if runtime.get("cuda_version") != "12.9" or runtime.get("cuda_13_qualified") is not False:
        raise CompatibilityError("GPU runtime must remain on qualified CUDA 12.9")


def _verify_configuration(
    configuration: dict[str, Any],
    snapshot: dict[str, Any],
    runtime: dict[str, Any],
) -> None:
    matching_fields = {
        "model_repository": "source_repository",
        "model_revision": "source_revision",
        "precision": "precision",
        "tensor_parallel_size": "tensor_parallel_size",
        "tool_call_parser": "tool_call_parser",
        "startup_seconds_min": "startup_seconds_min",
        "startup_seconds_max": "startup_seconds_max",
    }
    for matrix_field, snapshot_field in matching_fields.items():
        if configuration.get(matrix_field) != snapshot.get(snapshot_field):
            raise CompatibilityError(
                f"GPU matrix field {matrix_field} disagrees with model snapshot"
            )
    runtime_fields = {
        "vllm_version": "vllm_version",
        "pytorch_version": "pytorch_version",
        "cuda_version": "cuda_version",
        "minimum_driver_version": "minimum_driver_version",
    }
    for matrix_field, runtime_field in runtime_fields.items():
        if configuration.get(matrix_field) != runtime.get(runtime_field):
            raise CompatibilityError(
                f"GPU matrix field {matrix_field} disagrees with runtime contract"
            )
    if configuration.get("qualification_test") != _QUALIFICATION_TEST:
        raise CompatibilityError("GPU model uses an unsupported qualification test")
    for field in (
        "gpu_count",
        "minimum_gpu_memory_bytes",
        "context_window",
        "startup_seconds_min",
        "startup_seconds_max",
    ):
        value = configuration.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise CompatibilityError(f"GPU matrix field {field} must be positive")
    if configuration["startup_seconds_min"] > configuration["startup_seconds_max"]:
        raise CompatibilityError("GPU startup estimate is invalid")
    enforce_eager = configuration.get("enforce_eager", False)
    if not isinstance(enforce_eager, bool):
        raise CompatibilityError("GPU matrix field enforce_eager must be a boolean")
    context_window = configuration["context_window"]
    maximum_context_window = snapshot.get("maximum_context_window")
    if (
        not isinstance(maximum_context_window, int)
        or isinstance(maximum_context_window, bool)
        or not _MINIMUM_AGENT_CONTEXT_WINDOW <= context_window <= maximum_context_window
    ):
        raise CompatibilityError(
            "GPU matrix context must be agent-capable and within model capacity"
        )
    if configuration["gpu_count"] < snapshot.get("minimum_gpu_count", 0):
        raise CompatibilityError("GPU matrix does not satisfy the model GPU count")
    if configuration["minimum_gpu_memory_bytes"] < snapshot.get("minimum_gpu_memory_bytes", 0):
        raise CompatibilityError("GPU matrix does not satisfy model GPU memory")
    revision = _string(configuration, "model_revision")
    if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise CompatibilityError("GPU model revision must be an immutable commit")


def _toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as file:
            return tomllib.load(file)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise CompatibilityError(f"unable to load {path}: {error}") from error


def _mapping(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise CompatibilityError(f"{key} must be a table")
    return value


def _string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise CompatibilityError(f"{key} must be a non-empty string")
    return value


def _string_list(data: dict[str, Any], key: str) -> list[str]:
    value = data.get(key, [])
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise CompatibilityError(f"{key} must be an array of strings")
    return value


def _base_version(value: str) -> str:
    return value.split("+", maxsplit=1)[0]


def main() -> int:
    """Verify one repository checkout."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    verify_repository(args.root.resolve())
    print("GPU compatibility matrix verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
