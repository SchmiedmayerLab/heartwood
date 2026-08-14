# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Package the release-owned GPU runtime wheel from the compatibility contract."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import tempfile
import tomllib
import urllib.request
from pathlib import Path
from typing import Any, BinaryIO

_CHECKSUM_PATTERN = re.compile(r"^(?P<digest>[0-9a-f]{64})  (?P<name>[^/\r\n]+)$")
_CHUNK_SIZE = 1024 * 1024


class RuntimeAssetError(ValueError):
    """Raised when the GPU runtime asset cannot be packaged safely."""


def package_runtime_asset(
    *,
    output_dir: Path,
    compatibility_path: Path,
    source_file: Path | None = None,
) -> Path:
    """Download or copy the pinned wheel and add it to the release checksums."""
    runtime = _runtime_contract(compatibility_path)
    filename = _string(runtime, "vllm_wheel_filename")
    source_url = _string(runtime, "vllm_wheel_source_url")
    expected_digest = _string(runtime, "vllm_wheel_sha256")
    if Path(filename).name != filename or not filename.endswith(".whl"):
        raise RuntimeAssetError("GPU runtime wheel filename is unsafe")
    if not source_url.startswith("https://wheels.vllm.ai/"):
        raise RuntimeAssetError("GPU runtime wheel source must use the official vLLM host")
    if re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None:
        raise RuntimeAssetError("GPU runtime wheel digest must be SHA-256")

    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / filename
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{filename}.", dir=output_dir)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        if source_file is None:
            request = urllib.request.Request(
                source_url,
                headers={"User-Agent": "Heartwood release packager"},
            )
            with urllib.request.urlopen(request, timeout=120) as response:
                _copy(response, temporary)
        else:
            with source_file.open("rb") as source:
                _copy(source, temporary)
        actual_digest = _sha256(temporary)
        if actual_digest != expected_digest:
            raise RuntimeAssetError(
                "GPU runtime wheel digest differs from the compatibility contract"
            )
        temporary.chmod(0o644)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)

    _update_checksums(
        output_dir / "SHA256SUMS",
        filename=filename,
        digest=expected_digest,
    )
    return destination


def _runtime_contract(path: Path) -> dict[str, Any]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    runtime = data.get("runtime")
    if not isinstance(runtime, dict):
        raise RuntimeAssetError("GPU compatibility contract has no runtime table")
    return runtime


def _string(mapping: dict[str, Any], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise RuntimeAssetError(f"GPU runtime field is missing: {key}")
    return value


def _copy(source: BinaryIO, destination: Path) -> None:
    with destination.open("wb") as output:
        shutil.copyfileobj(source, output, length=_CHUNK_SIZE)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _update_checksums(path: Path, *, filename: str, digest: str) -> None:
    entries: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            match = _CHECKSUM_PATTERN.fullmatch(line)
            if match is None:
                raise RuntimeAssetError("release checksum manifest is malformed")
            name = match.group("name")
            if name in entries:
                raise RuntimeAssetError(f"duplicate release checksum entry: {name}")
            entries[name] = match.group("digest")
    if "heartwood-native.tar.gz" not in entries:
        raise RuntimeAssetError("native release asset must be packaged first")
    entries[filename] = digest
    ordered_names = [
        "heartwood-native.tar.gz",
        *sorted(entries.keys() - {"heartwood-native.tar.gz"}),
    ]
    path.write_text(
        "".join(f"{entries[name]}  {name}\n" for name in ordered_names),
        encoding="utf-8",
    )


def main() -> int:
    """Package the pinned runtime asset from command-line arguments."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=Path("dist"))
    parser.add_argument(
        "--compatibility",
        type=Path,
        default=Path("images/gpu/compatibility.toml"),
    )
    parser.add_argument("--source-file", type=Path)
    arguments = parser.parse_args()
    destination = package_runtime_asset(
        output_dir=arguments.output_dir,
        compatibility_path=arguments.compatibility,
        source_file=arguments.source_file,
    )
    print(f"Packaged {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
