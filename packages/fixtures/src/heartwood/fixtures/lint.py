# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""No-live-data linting for synthetic fixture trees."""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class FixtureFinding:
    """A single fixture lint finding."""

    path: Path
    line: int
    rule: str
    message: str


@dataclass(frozen=True, slots=True)
class _Rule:
    name: str
    pattern: re.Pattern[str]
    message: str


_TEXT_SUFFIXES = {
    ".csv",
    ".env",
    ".json",
    ".jsonl",
    ".md",
    ".txt",
    ".yaml",
    ".yml",
}

_RULES: tuple[_Rule, ...] = (
    _Rule(
        name="direct-identifier.email",
        pattern=re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
        message="fixture contains an email-shaped direct identifier",
    ),
    _Rule(
        name="direct-identifier.ssn",
        pattern=re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
        message="fixture contains an SSN-shaped direct identifier",
    ),
    _Rule(
        name="direct-identifier.phone",
        pattern=re.compile(r"\b(?:\+1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b"),
        message="fixture contains a phone-number-shaped direct identifier",
    ),
    _Rule(
        name="direct-identifier.mrn",
        pattern=re.compile(r"(?i)\b(?:mrn|medical_record_number)\b"),
        message="fixture contains a medical-record-number marker",
    ),
    _Rule(
        name="direct-identifier.birth-date",
        pattern=re.compile(r"(?i)\b(?:date_of_birth|birth_date|dob)\b"),
        message="fixture contains a birth-date marker",
    ),
    _Rule(
        name="secret.private-key",
        pattern=re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
        message="fixture contains a private-key marker",
    ),
    _Rule(
        name="secret.github-token",
        pattern=re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{20,}\b"),
        message="fixture contains a GitHub-token-shaped secret",
    ),
    _Rule(
        name="secret.aws-key",
        pattern=re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
        message="fixture contains an AWS-access-key-shaped secret",
    ),
    _Rule(
        name="secret.openai-key",
        pattern=re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
        message="fixture contains an API-key-shaped secret",
    ),
    _Rule(
        name="live-source-marker",
        pattern=re.compile(
            r"(?i)\b(?:production|prod dataset|live data|live phi|real patient|"
            r"non-synthetic|identified data)\b"
        ),
        message="fixture contains a live or non-synthetic source marker",
    ),
)


def _iter_fixture_files(paths: Iterable[Path]) -> Iterable[Path]:
    for path in paths:
        if path.is_file():
            if path.suffix.lower() in _TEXT_SUFFIXES:
                yield path
            continue
        for candidate in sorted(item for item in path.rglob("*") if item.is_file()):
            if candidate.suffix.lower() in _TEXT_SUFFIXES:
                yield candidate


def lint_fixture_tree(*roots: Path) -> tuple[FixtureFinding, ...]:
    """Return no-live-data findings for one or more fixture roots."""
    findings: list[FixtureFinding] = []
    for path in _iter_fixture_files(roots):
        text = path.read_text(encoding="utf-8")
        for line_number, line in enumerate(text.splitlines(), start=1):
            for rule in _RULES:
                if rule.pattern.search(line):
                    findings.append(
                        FixtureFinding(
                            path=path,
                            line=line_number,
                            rule=rule.name,
                            message=rule.message,
                        )
                    )
    return tuple(findings)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="heartwood-fixtures",
        description="Lint synthetic fixture trees for live identifiers, secrets, and markers.",
    )
    parser.add_argument("paths", nargs="+", type=Path, help="Fixture file or directory to lint.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run fixture linting and return a process exit code."""
    args = _build_parser().parse_args(argv)
    findings = lint_fixture_tree(*args.paths)
    for finding in findings:
        print(
            f"{finding.path}:{finding.line}: {finding.rule}: {finding.message}",
            file=sys.stderr,
        )
    return 1 if findings else 0
