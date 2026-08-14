# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Replace the pinned remote vLLM wheel with its verified local release asset."""

from __future__ import annotations

import argparse
import hashlib
import tomllib
from pathlib import Path
from typing import Any

_CHUNK_SIZE = 1024 * 1024


class LocalRuntimeError(ValueError):
    """Raised when a local runtime wheel differs from the pinned contract."""


def localize_requirements(
    *,
    source: Path,
    output: Path,
    wheel: Path,
    compatibility_path: Path,
) -> None:
    """Write a hashed requirements file that resolves vLLM from a local wheel."""
    runtime = _runtime_contract(compatibility_path)
    expected_filename = _string(runtime, "vllm_wheel_filename")
    source_url = _string(runtime, "vllm_wheel_source_url")
    expected_digest = _string(runtime, "vllm_wheel_sha256")
    if wheel.name != expected_filename:
        raise LocalRuntimeError(
            f"GPU runtime wheel must retain its pinned filename: {expected_filename}"
        )
    if _sha256(wheel) != expected_digest:
        raise LocalRuntimeError(
            "GPU runtime wheel digest differs from the compatibility contract"
        )

    expected_line = f"vllm @ {source_url}#sha256={expected_digest} \\\n"
    replacement = f"vllm @ {wheel.resolve().as_uri()}#sha256={expected_digest} \\\n"
    content = source.read_text(encoding="utf-8")
    if content.count(expected_line) != 1:
        raise LocalRuntimeError("GPU requirements do not contain the pinned vLLM source")
    output.write_text(content.replace(expected_line, replacement), encoding="utf-8")


def _runtime_contract(path: Path) -> dict[str, Any]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    runtime = data.get("runtime")
    if not isinstance(runtime, dict):
        raise LocalRuntimeError("GPU compatibility contract has no runtime table")
    return runtime


def _string(mapping: dict[str, Any], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise LocalRuntimeError(f"GPU runtime field is missing: {key}")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    """Localize the requirements file from command-line arguments."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--compatibility", type=Path, required=True)
    arguments = parser.parse_args()
    localize_requirements(
        source=arguments.source,
        output=arguments.output,
        wheel=arguments.wheel,
        compatibility_path=arguments.compatibility,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
