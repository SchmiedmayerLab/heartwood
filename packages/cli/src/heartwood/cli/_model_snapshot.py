# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Exact manifest verification for staged local-model snapshots."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path, PurePosixPath

_ENTRY = re.compile(r"^([0-9a-fA-F]{64}) [ *](.+)$")


def verify_snapshot(root: Path) -> None:
    """Reject unlisted, missing, linked, duplicated, or modified snapshot files."""
    manifest = root / "SHA256SUMS"
    if not root.is_dir() or not manifest.is_file() or manifest.is_symlink():
        raise ValueError("model root must contain a regular SHA256SUMS manifest")
    expected: dict[str, str] = {}
    for line_number, line in enumerate(manifest.read_text(encoding="utf-8").splitlines(), 1):
        match = _ENTRY.fullmatch(line)
        if match is None:
            raise ValueError(f"invalid SHA256SUMS entry on line {line_number}")
        digest, name = match.groups()
        manifest_relative = PurePosixPath(name)
        if (
            manifest_relative.is_absolute()
            or ".." in manifest_relative.parts
            or name in {"", "SHA256SUMS"}
        ):
            raise ValueError(f"unsafe SHA256SUMS path on line {line_number}")
        normalized = manifest_relative.as_posix()
        if normalized in expected:
            raise ValueError(f"duplicate SHA256SUMS path: {normalized}")
        expected[normalized] = digest.lower()

    actual: set[str] = set()
    for path in root.rglob("*"):
        snapshot_relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise ValueError(f"model snapshot contains a symbolic link: {snapshot_relative}")
        if path.is_file() and snapshot_relative != "SHA256SUMS":
            actual.add(snapshot_relative)
    if actual != set(expected):
        missing = sorted(set(expected) - actual)
        unlisted = sorted(actual - set(expected))
        detail = "; ".join(
            item
            for item in (
                f"missing: {', '.join(missing)}" if missing else "",
                f"unlisted: {', '.join(unlisted)}" if unlisted else "",
            )
            if item
        )
        raise ValueError(f"model snapshot does not match SHA256SUMS coverage ({detail})")

    for relative_name, expected_digest in expected.items():
        hasher = hashlib.sha256()
        descriptor = os.open(root / relative_name, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as file:
            while chunk := file.read(1024 * 1024):
                hasher.update(chunk)
        if hasher.hexdigest() != expected_digest:
            raise ValueError(f"SHA-256 mismatch: {relative_name}")
