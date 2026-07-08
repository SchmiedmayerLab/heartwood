# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""The ``heartwood`` command-line interface — the primary interaction surface."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from heartwood.core_adapter import SessionService
from heartwood.session import CommandKind, EventKind, JsonValue, SessionCommand, SessionEvent

__all__ = ["__version__", "main"]

__version__ = "0.0.0"

_PROG = "heartwood"


def _format_detection(event: SessionEvent, *, workspace: Path) -> str:
    """Render a detection event as a plain-language, propose-not-commit report."""
    platform = _mapping_payload(event.payload["platform"], "platform")
    dataset = _mapping_payload(event.payload["dataset"], "dataset")
    platform_confidence = _float_payload(platform["confidence"], "platform.confidence")
    dataset_confidence = _float_payload(dataset["confidence"], "dataset.confidence")
    lines = [
        "Heartwood — environment detection",
        "",
        "This is a proposal only. Nothing loads or runs without your confirmation.",
        "",
        f"Session: {event.session_id}",
        f"State: {workspace}",
        "",
        f"Platform: {platform['adapter_id']} (confidence {platform_confidence:.2f})",
    ]
    lines += [
        f"  - {item}" for item in _string_list_payload(platform["evidence"], "platform.evidence")
    ]
    lines += [
        "",
        f"Dataset: {dataset['dataset_type']} (confidence {dataset_confidence:.2f})",
    ]
    lines += [
        f"  - {item}" for item in _string_list_payload(dataset["evidence"], "dataset.evidence")
    ]
    return "\n".join(lines)


def _mapping_payload(value: JsonValue, name: str) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        msg = f"expected {name} payload to be an object"
        raise TypeError(msg)
    return value


def _float_payload(value: JsonValue, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        msg = f"expected {name} payload to be numeric"
        raise TypeError(msg)
    return float(value)


def _string_list_payload(value: JsonValue, name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        msg = f"expected {name} payload to be a string list"
        raise TypeError(msg)
    items: list[str] = []
    for item in value:
        if not isinstance(item, str):
            msg = f"expected {name} payload to be a string list"
            raise TypeError(msg)
        items.append(item)
    return tuple(items)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=_PROG,
        description="Compliance-first coding harness for sensitive biomedical research data.",
    )
    parser.add_argument("--version", action="version", version=f"{_PROG} {__version__}")
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path(".heartwood") / "sessions",
        help="Directory for local session state.",
    )
    parser.add_argument("--session-id", default="session-local", help="Session identifier.")
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")
    subparsers.add_parser(
        "detect",
        help="Detect the platform and propose next steps (nothing runs).",
        description="Inspect environment markers and propose the platform. Propose-not-commit.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the ``heartwood`` command and return a process exit code.

    Note that ``argparse`` raises :class:`SystemExit` for ``--version``,
    ``--help``, and invalid arguments, so callers should also handle that in
    addition to the returned exit code.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "detect":
        command = SessionCommand(
            command_id=f"{args.session_id}-detect",
            session_id=args.session_id,
            kind=CommandKind.DETECT,
            created_at="2026-01-01T00:00:00Z",
        )
        result = SessionService.local_default(
            args.workspace,
            session_id=args.session_id,
        ).handle(command)
        detection = next(
            event for event in result.events if event.kind == EventKind.DETECTION_PROPOSED.value
        )
        print(_format_detection(detection, workspace=args.workspace))
        return 0
    parser.print_help()
    return 0
