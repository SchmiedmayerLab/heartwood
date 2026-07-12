# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Read and validate one target digest from Docker Buildx Bake metadata."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def main(argv: list[str] | None = None) -> int:
    """Print the selected target digest or return a nonzero status."""
    parser = argparse.ArgumentParser()
    parser.add_argument("metadata", type=Path)
    parser.add_argument("target")
    args = parser.parse_args(argv)
    try:
        digest = _target_digest(args.metadata, args.target)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"invalid Buildx metadata: {error}", file=sys.stderr)
        return 1
    print(digest)
    return 0


def _target_digest(metadata_path: Path, target: str) -> str:
    metadata: Any = json.loads(metadata_path.read_text(encoding="utf-8"))
    if not isinstance(metadata, dict):
        raise ValueError("root must be an object")
    target_metadata = metadata.get(target)
    if not isinstance(target_metadata, dict):
        raise ValueError(f"target {target!r} is missing")
    digest = target_metadata.get("containerimage.digest")
    if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
        raise ValueError(f"target {target!r} has no valid container image digest")
    return digest


if __name__ == "__main__":
    raise SystemExit(main())
