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


def load_configuration(path: Path, configuration_id: str) -> dict[str, Any]:
    """Load one compatibility entry together with its runtime contract."""
    with path.open("rb") as file:
        matrix = tomllib.load(file)
    runtime = matrix.get("runtime")
    configurations = matrix.get("configurations")
    if not isinstance(runtime, dict) or not isinstance(configurations, list):
        raise ValueError("GPU compatibility matrix is malformed")
    for configuration in configurations:
        if (
            isinstance(configuration, dict)
            and configuration.get("configuration_id") == configuration_id
        ):
            return {"runtime": runtime, "configuration": configuration}
    raise ValueError(f"unknown GPU qualification configuration: {configuration_id}")


def main() -> int:
    """Print one resolved configuration for shell and CI consumers."""
    parser = argparse.ArgumentParser()
    parser.add_argument("configuration_id")
    parser.add_argument(
        "--matrix",
        type=Path,
        default=Path(__file__).with_name("compatibility.toml"),
    )
    args = parser.parse_args()
    print(json.dumps(load_configuration(args.matrix, args.configuration_id), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
