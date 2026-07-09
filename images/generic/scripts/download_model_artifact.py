#!/usr/bin/env python3
# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Download and verify the pinned local-runtime model artifact."""

from __future__ import annotations

import argparse
import hashlib
import shutil
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, BinaryIO


def main() -> int:
    """Download the manifest-selected artifact and atomically install it."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--skip-existing", action="store_true")
    parser.add_argument("--timeout-seconds", type=float, default=60)
    parser.add_argument("--retries", type=int, default=5)
    parser.add_argument("--retry-delay-seconds", type=float, default=2)
    args = parser.parse_args()
    if args.timeout_seconds <= 0:
        raise ValueError("--timeout-seconds must be positive")
    if args.retries <= 0:
        raise ValueError("--retries must be positive")
    if args.retry_delay_seconds < 0:
        raise ValueError("--retry-delay-seconds must be non-negative")

    manifest = _load_manifest(args.manifest)
    expected_sha256 = _string(manifest, "artifact_sha256")
    expected_size = _int(manifest, "artifact_size_bytes")
    source_url = _string(manifest, "source_url")
    output = args.output.resolve()

    if args.skip_existing and output.exists():
        _verify_file(output, expected_sha256=expected_sha256, expected_size=expected_size)
        print(f"verified existing local model artifact: {output}")
        return 0

    for attempt in range(1, args.retries + 1):
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=output.parent, delete=False) as tmp_file:
                tmp_path = Path(tmp_file.name)
                try:
                    print(
                        f"downloading local model artifact attempt {attempt}/{args.retries}: "
                        f"{source_url}",
                        flush=True,
                    )
                    _download(source_url, tmp_file, timeout_seconds=args.timeout_seconds)
                    tmp_file.flush()
                    _verify_file(
                        tmp_path,
                        expected_sha256=expected_sha256,
                        expected_size=expected_size,
                    )
                    tmp_path.replace(output)
                finally:
                    if tmp_path.exists():
                        tmp_path.unlink()
            print(f"installed local model artifact: {output}")
            return 0
        except (OSError, TimeoutError, ValueError, urllib.error.URLError) as error:
            if attempt >= args.retries:
                raise
            print(
                f"local model artifact download failed: {error}; retrying in "
                f"{args.retry_delay_seconds:g}s",
                flush=True,
            )
            time.sleep(args.retry_delay_seconds)
    raise AssertionError("unreachable")


def _download(source_url: str, tmp_file: BinaryIO, *, timeout_seconds: float) -> None:
    with urllib.request.urlopen(source_url, timeout=timeout_seconds) as response:
        shutil.copyfileobj(response, tmp_file)


def _load_manifest(path: Path) -> dict[str, Any]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        msg = f"manifest must be a TOML table: {path}"
        raise TypeError(msg)
    return data


def _string(manifest: dict[str, Any], key: str) -> str:
    value = manifest.get(key)
    if not isinstance(value, str) or not value:
        msg = f"manifest field {key} must be a non-empty string"
        raise ValueError(msg)
    return value


def _int(manifest: dict[str, Any], key: str) -> int:
    value = manifest.get(key)
    if not isinstance(value, int) or value <= 0:
        msg = f"manifest field {key} must be a positive integer"
        raise ValueError(msg)
    return value


def _verify_file(path: Path, *, expected_sha256: str, expected_size: int) -> None:
    actual_size = path.stat().st_size
    if actual_size != expected_size:
        msg = f"model artifact size mismatch: expected {expected_size}, got {actual_size}"
        raise ValueError(msg)
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    actual_sha256 = digest.hexdigest()
    if actual_sha256 != expected_sha256:
        msg = f"model artifact SHA-256 mismatch: expected {expected_sha256}, got {actual_sha256}"
        raise ValueError(msg)


if __name__ == "__main__":
    raise SystemExit(main())
