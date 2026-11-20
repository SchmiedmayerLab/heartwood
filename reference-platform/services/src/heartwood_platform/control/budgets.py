# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Allocations, reservations and incurred spending of the projects.

Each project has one budget for each category of work, with the amount that the chart
allocates to it. Running work holds a reservation on a budget: a session holds one on
its project's inference budget from its first launch until its deletion. Settling a
reservation records what the work incurred, which can exceed the reservation, and
releases the rest. Every change is one SQLite transaction that checks availability, so
concurrent sessions and jobs cannot reserve more than a budget holds.
"""

from __future__ import annotations

import secrets
import sqlite3
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

Category = Literal["inference", "compute"]
CATEGORIES: tuple[Category, ...] = ("inference", "compute")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS budget (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    category TEXT NOT NULL,
    allocated TEXT NOT NULL,
    revision INTEGER NOT NULL,
    UNIQUE (project_id, category)
) STRICT;
CREATE TABLE IF NOT EXISTS reservation (
    id TEXT PRIMARY KEY,
    budget_id TEXT NOT NULL REFERENCES budget (id),
    holder TEXT NOT NULL,
    amount TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('held', 'settled')),
    incurred TEXT,
    created_at TEXT NOT NULL,
    settled_at TEXT
) STRICT;
CREATE UNIQUE INDEX IF NOT EXISTS held_reservation ON reservation (holder) WHERE state = 'held';
"""


class InsufficientBudgetError(Exception):
    """The budget does not hold enough available amount for the reservation."""


@dataclass(frozen=True)
class Allocation:
    """The amount allocated to one category of a project's work."""

    project: str
    category: Category
    amount: Decimal


@dataclass(frozen=True)
class Reservation:
    """An amount of a budget held by one piece of running work."""

    id: str
    budget_id: str
    holder: str
    amount: Decimal
    state: Literal["held", "settled"]
    incurred: Decimal | None


@dataclass(frozen=True)
class Balance:
    """The state of one budget: allocated, reserved, incurred and available amounts.

    ``available`` is negative when incurred spending exceeded what was reserved for it
    and nothing else was left.
    """

    budget_id: str
    project: str
    category: Category
    allocated: Decimal
    reserved: Decimal
    spent: Decimal
    available: Decimal
    revision: int


def budget_id(project: str, category: Category) -> str:
    """Return the identifier of a project's budget for one category."""
    return f"{project}-{category}"


class Ledger:
    """Budgets and reservations in one SQLite database."""

    def __init__(
        self, path: Path, clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    ) -> None:
        """Open the database at a path and create its tables."""
        self._path = path
        self._clock = clock
        with self._transaction() as connection:
            for statement in _SCHEMA.split(";"):
                connection.execute(statement)

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._path, timeout=30, isolation_level=None)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def synchronize(self, allocations: Iterable[Allocation]) -> None:
        """Set the allocated amounts to the configured ones.

        A budget that is no longer configured keeps its reservations and spending and
        has nothing allocated.
        """
        configured = {budget_id(item.project, item.category): item for item in allocations}
        with self._transaction() as connection:
            existing = dict(connection.execute("SELECT id, allocated FROM budget").fetchall())
            for identifier, allocated in existing.items():
                amount = configured[identifier].amount if identifier in configured else Decimal(0)
                if Decimal(allocated) != amount:
                    connection.execute(
                        "UPDATE budget SET allocated = ?, revision = revision + 1 WHERE id = ?",
                        (str(amount), identifier),
                    )
            for identifier, item in configured.items():
                if identifier not in existing:
                    connection.execute(
                        "INSERT INTO budget VALUES (?, ?, ?, ?, 1)",
                        (identifier, item.project, item.category, str(item.amount)),
                    )

    @staticmethod
    def _balance(connection: sqlite3.Connection, row: tuple[str, str, str, str, int]) -> Balance:
        identifier, project, category, allocated, revision = row
        reserved = spent = Decimal(0)
        for amount, state, incurred in connection.execute(
            "SELECT amount, state, incurred FROM reservation WHERE budget_id = ?", (identifier,)
        ):
            if state == "held":
                reserved += Decimal(amount)
            else:
                spent += Decimal(incurred)
        assert category in CATEGORIES
        return Balance(
            budget_id=identifier,
            project=project,
            category=category,
            allocated=Decimal(allocated),
            reserved=reserved,
            spent=spent,
            available=Decimal(allocated) - reserved - spent,
            revision=revision,
        )

    def balances(self, project: str) -> list[Balance]:
        """Return the budgets of a project with their amounts."""
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT id, project_id, category, allocated, revision FROM budget"
                " WHERE project_id = ? ORDER BY category",
                (project,),
            ).fetchall()
            return [self._balance(connection, row) for row in rows]

    def held(self, holder: str) -> Reservation | None:
        """Return the reservation that a holder holds, if any."""
        with self._transaction() as connection:
            return _reservation(connection, holder)

    def reserve(
        self,
        project: str,
        category: Category,
        holder: str,
        amount: Decimal,
        *,
        minimum: Decimal = Decimal(0),
    ) -> Reservation:
        """Reserve up to ``amount`` of a budget for a holder, bounded by availability.

        A holder that already holds a reservation keeps it. Less than ``amount`` is
        reserved when less is available, and nothing is reserved when the available
        amount does not exceed ``minimum``.
        """
        identifier = budget_id(project, category)
        with self._transaction() as connection:
            existing = _reservation(connection, holder)
            if existing is not None:
                return existing
            row = connection.execute(
                "SELECT id, project_id, category, allocated, revision FROM budget WHERE id = ?",
                (identifier,),
            ).fetchone()
            if row is None:
                raise InsufficientBudgetError
            reserved = min(amount, self._balance(connection, row).available)
            if reserved <= minimum:
                raise InsufficientBudgetError
            reservation = secrets.token_hex(8)
            connection.execute(
                "INSERT INTO reservation VALUES (?, ?, ?, ?, 'held', NULL, ?, NULL)",
                (reservation, identifier, holder, str(reserved), self._clock().isoformat()),
            )
            connection.execute(
                "UPDATE budget SET revision = revision + 1 WHERE id = ?", (identifier,)
            )
            held = _reservation(connection, holder)
            assert held is not None
            return held

    def settle(self, holder: str, incurred: Decimal) -> Reservation | None:
        """Record what a holder's work incurred and release the rest of its reservation.

        The incurred amount is recorded in full, also when it exceeds the reservation.
        A holder without a reservation has nothing to settle.
        """
        if incurred < 0:
            raise ValueError("an incurred amount is not negative")
        with self._transaction() as connection:
            held = _reservation(connection, holder)
            if held is None:
                return None
            connection.execute(
                "UPDATE reservation SET state = 'settled', incurred = ?, settled_at = ?"
                " WHERE id = ?",
                (str(incurred), self._clock().isoformat(), held.id),
            )
            connection.execute(
                "UPDATE budget SET revision = revision + 1 WHERE id = ?", (held.budget_id,)
            )
        return Reservation(held.id, held.budget_id, holder, held.amount, "settled", incurred)


def _reservation(connection: sqlite3.Connection, holder: str) -> Reservation | None:
    row = connection.execute(
        "SELECT id, budget_id, holder, amount FROM reservation WHERE holder = ? AND state = 'held'",
        (holder,),
    ).fetchone()
    if row is None:
        return None
    return Reservation(row[0], row[1], row[2], Decimal(row[3]), "held", None)
