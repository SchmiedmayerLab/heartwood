# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Allocations, reservations and incurred spending of the projects.

Each project has one budget for each category of work, with the amount that the chart
allocates to it. Running work holds a reservation on a budget: a session holds one on
its project's inference budget from its first launch until its deletion, and a batch
job holds one on its project's compute budget from its submission until its task ends.
Settling a reservation records what the work incurred, which can exceed the reservation,
and releases the rest. Every change is one SQLite transaction that checks availability,
so concurrent sessions and jobs cannot reserve more than a budget holds.
"""

from __future__ import annotations

import secrets
import sqlite3
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Literal

from heartwood.schemas import CostDenomination
from heartwood_platform.control.prices import TaskRate

Category = Literal["inference", "compute"]
CATEGORIES: tuple[Category, ...] = ("inference", "compute")
# Amounts have at most 18 integer and 12 fractional digits; sums of many of them stay
# exact at this precision.
_PRECISION = 60

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
CREATE TABLE IF NOT EXISTS job_rate (
    holder TEXT PRIMARY KEY,
    unit_price TEXT,
    seconds_per_unit TEXT
) STRICT;
CREATE UNIQUE INDEX IF NOT EXISTS held_reservation ON reservation (holder) WHERE state = 'held';
"""


# The prefixes of the holders of session and job reservations.
SESSION_HOLDER = "session:"
JOB_HOLDER = "job/"


class InsufficientBudgetError(Exception):
    """The budget does not hold enough available amount for the reservation.

    ``available`` is the available amount of the budget, or None when the project has
    no budget of the category.
    """

    def __init__(self, budget_id: str, available: Decimal | None, amount: Decimal) -> None:
        """Name the budget that refused the reservation and what it has available."""
        super().__init__(budget_id)
        self.budget_id = budget_id
        self.available = available
        self.amount = amount


class EndedHolderError(Exception):
    """The holder's reservation was settled, so the holder reserves nothing again."""


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
    created_at: datetime


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
    # The number and amount of the held reservations of jobs and of sessions.
    job_reservations: tuple[int, Decimal] = (0, Decimal(0))
    session_reservations: tuple[int, Decimal] = (0, Decimal(0))


def budget_id(project: str, category: Category) -> str:
    """Return the identifier of a project's budget for one category."""
    return f"{project}-{category}"


class Ledger:
    """Budgets and reservations in one SQLite database."""

    def __init__(
        self,
        path: Path,
        denomination: CostDenomination,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        """Open the database at a path and create its tables."""
        self._path = path
        self._clock = clock
        with self._transaction() as connection:
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            if "ledger_denomination" not in tables:
                if "budget" in tables:
                    raise ValueError(
                        "Incompatible ledger without a denomination. Preserve the control database "
                        "and start compatible state."
                    )
                connection.execute(
                    "CREATE TABLE ledger_denomination (id INTEGER PRIMARY KEY CHECK (id = 1), "
                    "kind TEXT NOT NULL, unit TEXT NOT NULL) STRICT"
                )
                connection.execute(
                    "INSERT INTO ledger_denomination VALUES (1, ?, ?)",
                    (denomination.kind, denomination.unit),
                )
            stored = connection.execute(
                "SELECT kind, unit FROM ledger_denomination WHERE id = 1"
            ).fetchone()
            if stored is None:
                raise ValueError("Missing ledger denomination. Restore the control database.")
            if stored != (denomination.kind, denomination.unit):
                raise ValueError(
                    f"Ledger denomination is {stored[0]} {stored[1]}. "
                    "Restore the matching price sheet."
                )
            self.denomination = denomination
            for statement in _SCHEMA.split(";"):
                connection.execute(statement)

    @contextmanager
    def _transaction(self, *, write: bool = True) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._path, timeout=30, isolation_level=None)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            # A write takes the write lock at once, so its checks and changes see one state.
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
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
        held: dict[str, tuple[int, Decimal]] = {
            JOB_HOLDER: (0, Decimal(0)),
            SESSION_HOLDER: (0, Decimal(0)),
        }
        with localcontext(prec=_PRECISION):
            for holder, amount, state, incurred in connection.execute(
                "SELECT holder, amount, state, incurred FROM reservation WHERE budget_id = ?",
                (identifier,),
            ):
                if state == "held":
                    reserved += Decimal(amount)
                    for prefix, (count, total) in held.items():
                        if holder.startswith(prefix):
                            held[prefix] = (count + 1, total + Decimal(amount))
                else:
                    spent += Decimal(incurred)
            available = Decimal(allocated) - reserved - spent
        assert category in CATEGORIES
        return Balance(
            budget_id=identifier,
            project=project,
            category=category,
            allocated=Decimal(allocated),
            reserved=reserved,
            spent=spent,
            available=available,
            revision=revision,
            job_reservations=held[JOB_HOLDER],
            session_reservations=held[SESSION_HOLDER],
        )

    def balances(self, project: str) -> list[Balance]:
        """Return the budgets of a project with their amounts."""
        with self._transaction(write=False) as connection:
            rows = connection.execute(
                "SELECT id, project_id, category, allocated, revision FROM budget"
                " WHERE project_id = ? ORDER BY category",
                (project,),
            ).fetchall()
            return [self._balance(connection, row) for row in rows]

    def held(self, holder: str) -> Reservation | None:
        """Return the reservation that a holder holds, if any."""
        with self._transaction(write=False) as connection:
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
                raise InsufficientBudgetError(identifier, None, amount)
            available = self._balance(connection, row).available
            reserved = min(amount, available)
            if reserved <= minimum:
                raise InsufficientBudgetError(identifier, available, amount)
            return self._insert(connection, identifier, holder, reserved)

    def reserve_once(
        self, project: str, holder: str, seconds: int, rate_for_job: Callable[[], TaskRate]
    ) -> Reservation:
        """Hold the full cost and save the rate used to settle the job."""
        identifier = budget_id(project, "compute")
        with self._transaction() as connection:
            existing = _latest(connection, holder)
            if existing is not None and existing.state == "held":
                return existing
            if (
                existing
                or connection.execute(
                    "SELECT 1 FROM job_rate WHERE holder = ?", (holder,)
                ).fetchone()
            ):
                raise EndedHolderError(holder)
            rate = rate_for_job()
            amount = rate.cost(seconds)
            row = connection.execute(
                "SELECT id, project_id, category, allocated, revision FROM budget WHERE id = ?",
                (identifier,),
            ).fetchone()
            available = self._balance(connection, row).available if row else None
            if available is None or amount > available:
                raise InsufficientBudgetError(identifier, available, amount)
            connection.execute(
                "INSERT INTO job_rate VALUES (?, ?, ?)",
                (holder, str(rate.unit_price), str(rate.seconds_per_unit)),
            )
            return self._insert(connection, identifier, holder, amount)

    def _insert(
        self, connection: sqlite3.Connection, identifier: str, holder: str, amount: Decimal
    ) -> Reservation:
        connection.execute(
            "INSERT INTO reservation VALUES (?, ?, ?, ?, 'held', NULL, ?, NULL)",
            (secrets.token_hex(8), identifier, holder, str(amount), self._clock().isoformat()),
        )
        connection.execute("UPDATE budget SET revision = revision + 1 WHERE id = ?", (identifier,))
        held = _reservation(connection, holder)
        assert held is not None
        return held

    def settle(self, holder: str, incurred: Decimal) -> Reservation | None:
        """Record what a holder's work incurred and release the rest of its reservation.

        The incurred amount is recorded in full, also when it exceeds the reservation.
        A holder without a reservation has nothing to settle.
        """
        if not incurred.is_finite() or incurred < 0:
            raise ValueError("an incurred amount is a finite amount that is not negative")
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
        return Reservation(
            held.id, held.budget_id, holder, held.amount, "settled", incurred, held.created_at
        )

    def settle_once(self, project: str, holder: str, seconds: int) -> Reservation:
        """Charge observed seconds once, fencing a delayed reserve even without a budget."""
        with self._transaction() as connection:
            existing = _latest(connection, holder)
            if existing is None:
                connection.execute(
                    "INSERT OR IGNORE INTO job_rate VALUES (?, NULL, NULL)", (holder,)
                )
                return Reservation(
                    "",
                    budget_id(project, "compute"),
                    holder,
                    Decimal(0),
                    "settled",
                    Decimal(0),
                    self._clock(),
                )
            if existing.state == "settled":
                return existing
            price, quantity = connection.execute(
                "SELECT unit_price, seconds_per_unit FROM job_rate WHERE holder = ?", (holder,)
            ).fetchone()
            amount = TaskRate(Decimal(price), Decimal(quantity)).cost(seconds)
            connection.execute(
                "UPDATE reservation SET state = 'settled', incurred = ?, settled_at = ?"
                " WHERE id = ?",
                (str(amount), self._clock().isoformat(), existing.id),
            )
            connection.execute(
                "UPDATE budget SET revision = revision + 1 WHERE id = ?", (existing.budget_id,)
            )
            settled = _latest(connection, holder)
            assert settled is not None
            return settled


_COLUMNS = "id, budget_id, holder, amount, state, incurred, created_at"


def _row(row: tuple[str, str, str, str, str, str | None, str]) -> Reservation:
    identifier, budget, holder, amount, state, incurred, created = row
    assert state in {"held", "settled"}
    return Reservation(
        identifier,
        budget,
        holder,
        Decimal(amount),
        "held" if state == "held" else "settled",
        None if incurred is None else Decimal(incurred),
        datetime.fromisoformat(created),
    )


def _reservation(connection: sqlite3.Connection, holder: str) -> Reservation | None:
    row = connection.execute(
        f"SELECT {_COLUMNS} FROM reservation WHERE holder = ? AND state = 'held'",
        (holder,),
    ).fetchone()
    return None if row is None else _row(row)


def _latest(connection: sqlite3.Connection, holder: str) -> Reservation | None:
    """Return the held reservation of a holder, or else its latest settled one."""
    row = connection.execute(
        f"SELECT {_COLUMNS} FROM reservation WHERE holder = ?"
        " ORDER BY state = 'held' DESC, created_at DESC, rowid DESC LIMIT 1",
        (holder,),
    ).fetchone()
    return None if row is None else _row(row)
