# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""The job service's own record of every operation, in one SQLite database.

Idempotency, reservation and settlement read this record, so they do not depend on how
long the executor keeps a task. An operation identifier is unique within its project.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, fields, replace
from datetime import datetime
from pathlib import Path
from typing import Literal

# submit: a task submission; cancel: a cancellation that names another operation;
# absent: an identifier that was asked about before it was received, refused for good.
type Kind = Literal["submit", "cancel", "absent"]
# A submission is first recorded as SUBMITTING, before the executor is called, so a task
# whose creation reply was lost is found again by its operation tag. DENIED records a
# refusal by entitlement or budget; the other states are TES states.
type RecordState = str

_SCHEMA = """
CREATE TABLE IF NOT EXISTS operation (
    project TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('submit', 'cancel', 'absent')),
    subject TEXT NOT NULL,
    context TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    state TEXT NOT NULL,
    task_id TEXT,
    target TEXT,
    detail TEXT,
    failure TEXT,
    cpu_cores INTEGER NOT NULL,
    ram_gb REAL NOT NULL,
    gpu_count INTEGER NOT NULL,
    max_runtime_seconds INTEGER NOT NULL,
    reservation TEXT,
    settled INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    ended_at TEXT,
    PRIMARY KEY (project, operation_id)
) STRICT;
CREATE UNIQUE INDEX IF NOT EXISTS operation_task ON operation (project, task_id)
    WHERE task_id IS NOT NULL;
"""


@dataclass(frozen=True)
class Operation:
    """One operation as the job service recorded it."""

    project: str
    operation_id: str
    kind: Kind
    subject: str
    # The launch context the operation was received in, which its observations carry.
    context: str
    request_sha256: str
    state: RecordState
    task_id: str | None
    target: str | None
    # Why the platform refused a submission, or what ended its task.
    detail: str | None
    # Why the job service ended a task itself, such as a missed start deadline.
    failure: str | None
    cpu_cores: int
    ram_gb: float
    gpu_count: int
    max_runtime_seconds: int
    reservation: str | None
    settled: bool
    created_at: datetime
    started_at: datetime | None
    ended_at: datetime | None


_COLUMNS = [item.name for item in fields(Operation)]


def _row(row: tuple[object, ...]) -> Operation:
    values = dict(zip(_COLUMNS, row, strict=True))
    for name in ("created_at", "started_at", "ended_at"):
        value = values[name]
        values[name] = None if value is None else datetime.fromisoformat(str(value))
    values["settled"] = bool(values["settled"])
    return Operation(**values)  # type: ignore[arg-type]


def _value(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bool):
        return int(value)
    return value


class ConflictError(Exception):
    """An operation with this identifier exists."""


class Records:
    """Operations in one SQLite database."""

    def __init__(self, path: Path) -> None:
        """Open the database at a path and create its tables."""
        self._path = path
        with self._transaction() as connection:
            for statement in _SCHEMA.split(";"):
                connection.execute(statement)

    @contextmanager
    def _transaction(self, *, write: bool = True) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._path, timeout=30, isolation_level=None)
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def get(self, project: str, operation_id: str) -> Operation | None:
        """Return an operation of a project."""
        with self._transaction(write=False) as connection:
            row = connection.execute(
                f"SELECT {', '.join(_COLUMNS)} FROM operation"
                " WHERE project = ? AND operation_id = ?",
                (project, operation_id),
            ).fetchone()
        return None if row is None else _row(row)

    def by_task(self, project: str, task_id: str) -> Operation | None:
        """Return the operation that created a task."""
        with self._transaction(write=False) as connection:
            row = connection.execute(
                f"SELECT {', '.join(_COLUMNS)} FROM operation WHERE project = ? AND task_id = ?",
                (project, task_id),
            ).fetchone()
        return None if row is None else _row(row)

    def insert(self, operation: Operation) -> None:
        """Record a new operation; raise :class:`ConflictError` when its identifier exists."""
        try:
            with self._transaction() as connection:
                connection.execute(
                    f"INSERT INTO operation VALUES ({', '.join('?' for _ in _COLUMNS)})",
                    [_value(getattr(operation, name)) for name in _COLUMNS],
                )
        except sqlite3.IntegrityError:
            raise ConflictError from None

    def update(self, operation: Operation, **changes: object) -> Operation:
        """Change the fields of a recorded operation and return it.

        Raise :class:`ConflictError` when a task identifier belongs to another operation.
        """
        changed = replace(operation, **changes)  # type: ignore[arg-type]
        assignments = ", ".join(f"{name} = ?" for name in changes)
        try:
            with self._transaction() as connection:
                connection.execute(
                    f"UPDATE operation SET {assignments} WHERE project = ? AND operation_id = ?",
                    [
                        *(_value(value) for value in changes.values()),
                        operation.project,
                        operation.operation_id,
                    ],
                )
        except sqlite3.IntegrityError:
            raise ConflictError from None
        return changed

    def active_submissions(self, project: str) -> int:
        """Return how many submissions of a project are waiting or running."""
        with self._transaction(write=False) as connection:
            row = connection.execute(
                "SELECT count(*) FROM operation WHERE project = ? AND kind = 'submit'"
                " AND state NOT IN ('DENIED', 'COMPLETE', 'EXECUTOR_ERROR', 'SYSTEM_ERROR',"
                " 'CANCELED', 'PREEMPTED')",
                (project,),
            ).fetchone()
        return int(row[0])

    def open_submissions(self, project: str) -> list[Operation]:
        """Return the submissions of a project whose task has not ended or is unsettled."""
        with self._transaction(write=False) as connection:
            rows = connection.execute(
                f"SELECT {', '.join(_COLUMNS)} FROM operation"
                " WHERE project = ? AND kind = 'submit' AND state != 'DENIED'"
                " AND (ended_at IS NULL OR settled = 0) ORDER BY created_at",
                (project,),
            ).fetchall()
        return [_row(row) for row in rows]
