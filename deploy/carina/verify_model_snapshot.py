# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Verify that a model snapshot exactly matches its SHA-256 manifest."""

from __future__ import annotations

import sys
from pathlib import Path

from heartwood.cli._model_snapshot import verify_snapshot


def main() -> int:
    """Verify the model root supplied on the command line."""
    if len(sys.argv) != 2:
        print("usage: verify_model_snapshot.py MODEL_ROOT", file=sys.stderr)
        return 64
    try:
        verify_snapshot(Path(sys.argv[1]))
    except (OSError, UnicodeError, ValueError) as error:
        print(f"model verification failed: {error}", file=sys.stderr)
        return 66
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
