# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Durable benchmark results, including trials interrupted before verification."""

from __future__ import annotations

import re
from pathlib import Path
from uuid import UUID

from pydantic import ValidationError

from heartwood.persistence import (
    DurableFileError,
    native_file_lock,
    read_private_json,
    write_private_json_atomic,
)
from heartwood.schemas.evaluation import EvaluationRun

_ATOMIC_TEMPORARY = re.compile(r"^\.(?P<run_id>[0-9a-f-]{36})\.json-[a-z0-9_]{8}$")


class EvaluationStore:
    """Store one private record per trial using shared atomic persistence primitives.

    An incomplete record is written before model work starts. A failed or killed
    evaluator leaves that record visible; loading evidence never resumes an agent.
    These records are evaluation evidence, not session state or signed attestations.
    """

    def __init__(self, root: Path) -> None:
        """Use a dedicated private directory for benchmark result records."""
        self.root = root

    def begin(self, run: EvaluationRun) -> None:
        """Reserve a new trial identity before any model or tool execution."""
        if run.status != "incomplete":
            raise ValueError("A trial must begin with incomplete evidence")
        with native_file_lock(self.root / ".lock"):
            path = self._path(run.run_id)
            if path.exists() or path.is_symlink():
                raise ValueError("Evaluation trial identity already exists")
            write_private_json_atomic(path, run.model_dump(mode="json"))

    def complete(self, run: EvaluationRun) -> None:
        """Replace pending evidence once, accepting only an identical final retry."""
        if run.status != "completed":
            raise ValueError("Final evaluation must be completed")
        with native_file_lock(self.root / ".lock"):
            path = self._path(run.run_id)
            previous = _read_record(path)
            if previous == run:
                return
            if previous.status != "incomplete":
                raise ValueError("Completed evaluation evidence cannot be replaced")
            excluded = {"status", "finished_at", "checks", "usage"}
            if previous.model_dump(exclude=excluded) != run.model_dump(exclude=excluded):
                raise ValueError("Final evaluation changed the reserved trial identity")
            expected = tuple((check.check_id, check.dimension) for check in previous.checks)
            actual = tuple((check.check_id, check.dimension) for check in run.checks)
            if actual != expected:
                raise ValueError("Final evaluation changed the reserved check contract")
            write_private_json_atomic(path, run.model_dump(mode="json"))

    def records(self) -> tuple[EvaluationRun, ...]:
        """Read all trials, failing closed for corrupt or substituted records."""
        with native_file_lock(self.root / ".lock"):
            records = []
            for path in sorted(self.root.iterdir()):
                if path.name == ".lock":
                    continue
                temporary = _ATOMIC_TEMPORARY.fullmatch(path.name)
                if temporary and path.is_file() and not path.is_symlink():
                    run_id = UUID(temporary.group("run_id"))
                    canonical = self._path(run_id)
                    if not canonical.exists() or _read_record(canonical).run_id != run_id:
                        raise ValueError("Orphaned evaluation temporary requires inspection")
                    continue
                if path.suffix != ".json":
                    raise ValueError("Unexpected entry in evaluation result directory")
                run = _read_record(path)
                if path != self._path(run.run_id):
                    raise ValueError("Evaluation record filename does not match its identity")
                records.append(run)
            return tuple(records)

    def _path(self, run_id: UUID) -> Path:
        return self.root / f"{run_id}.json"


def _read_record(path: Path) -> EvaluationRun:
    try:
        return EvaluationRun.model_validate(read_private_json(path))
    except (ValidationError, DurableFileError):
        raise ValueError("Persisted evaluation record is invalid") from None
