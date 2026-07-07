# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""The ``heartwood`` command-line interface — the primary interaction surface.

Every interface is a thin presentation over shared core logic; this CLI renders
environment detection as a propose-not-commit report. Analysis commands
(``chat``, ``run``, ``replay``, ``audit``) arrive with the session contract in
later phases. See ``design/03-architecture.md`` and ``design/09-implementation-plan.md``.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from heartwood.detector import PlatformDetection, detect_platform

__all__ = ["__version__", "main"]

__version__ = "0.0.0"

_PROG = "heartwood"


def _format_detection(detection: PlatformDetection) -> str:
    """Render a platform detection as a plain-language, propose-not-commit report."""
    lines = [
        "Heartwood — environment detection",
        "",
        "This is a proposal only. Nothing loads or runs without your confirmation.",
        "",
        f"Platform: {detection.platform.value} (confidence {detection.confidence:.2f})",
    ]
    lines += [f"  - {item}" for item in detection.evidence]
    lines += [
        "",
        "Dataset detection and skill proposals are not implemented yet (see design/04-skills.md).",
    ]
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=_PROG,
        description="Compliance-first coding harness for sensitive biomedical research data.",
    )
    parser.add_argument("--version", action="version", version=f"{_PROG} {__version__}")
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")
    subparsers.add_parser(
        "detect",
        help="Detect the platform and propose next steps (nothing runs).",
        description="Inspect environment markers and propose the platform. Propose-not-commit.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the ``heartwood`` command and return a process exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "detect":
        print(_format_detection(detect_platform()))
        return 0
    parser.print_help()
    return 0
