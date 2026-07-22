# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Resolve one reviewed GPU qualification configuration as JSON."""

from __future__ import annotations

import argparse
import json
import tomllib
from pathlib import Path
from typing import Any

_REQUIRED_CONFIGURATION_FIELDS = {
    "configuration_id": str,
    "status": str,
    "platform": str,
    "gpu_model": str,
    "gpu_count": int,
    "minimum_gpu_memory_bytes": int,
    "model_snapshot": str,
    "model_repository": str,
    "model_revision": str,
    "precision": str,
    "context_window": int,
    "tensor_parallel_size": int,
    "tool_call_parser": str,
    "agent_tool_mode": str,
    "vllm_version": str,
    "pytorch_version": str,
    "cuda_version": str,
    "minimum_driver_version": str,
    "qualification_test": str,
    "startup_seconds_min": int,
    "startup_seconds_max": int,
}


def _validate_configuration(configuration: object) -> dict[str, Any]:
    if not isinstance(configuration, dict):
        raise ValueError("GPU compatibility matrix contains a malformed configuration")
    for field, expected_type in _REQUIRED_CONFIGURATION_FIELDS.items():
        value = configuration.get(field)
        if not isinstance(value, expected_type) or isinstance(value, bool):
            raise ValueError(f"GPU configuration {field} must be a {expected_type.__name__}")
        if expected_type is str and not value:
            raise ValueError(f"GPU configuration {field} must not be empty")
        if expected_type is int and value <= 0:
            raise ValueError(f"GPU configuration {field} must be positive")
    return configuration


def list_configurations(path: Path, *, platform: str | None = None) -> list[dict[str, Any]]:
    """List reviewed configurations, optionally restricted to one platform."""
    with path.open("rb") as file:
        configurations = tomllib.load(file).get("configurations")
    if not isinstance(configurations, list):
        raise ValueError("GPU compatibility matrix is malformed")
    validated = [_validate_configuration(configuration) for configuration in configurations]
    return [
        configuration
        for configuration in validated
        if platform is None or configuration["platform"] == platform
    ]


def load_configuration(path: Path, configuration_id: str) -> dict[str, Any]:
    """Load one compatibility entry together with its runtime contract."""
    with path.open("rb") as file:
        matrix = tomllib.load(file)
    runtime = matrix.get("runtime")
    configurations = matrix.get("configurations")
    if not isinstance(runtime, dict) or not isinstance(configurations, list):
        raise ValueError("GPU compatibility matrix is malformed")
    for item in configurations:
        configuration = _validate_configuration(item)
        if configuration["configuration_id"] == configuration_id:
            return {"runtime": runtime, "configuration": configuration}
    raise ValueError(f"unknown GPU qualification configuration: {configuration_id}")


def main() -> int:
    """Print reviewed configuration metadata for shell and CI consumers."""
    parser = argparse.ArgumentParser()
    parser.add_argument("configuration_id", nargs="?")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--platform")
    parser.add_argument(
        "--matrix",
        type=Path,
        default=Path(__file__).with_name("compatibility.toml"),
    )
    args = parser.parse_args()
    if args.list and args.configuration_id is not None:
        parser.error("configuration_id cannot be combined with --list")
    if args.list:
        print(json.dumps(list_configurations(args.matrix, platform=args.platform), sort_keys=True))
        return 0
    if args.configuration_id is None:
        parser.error("configuration_id is required unless --list is used")
    print(json.dumps(load_configuration(args.matrix, args.configuration_id), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
