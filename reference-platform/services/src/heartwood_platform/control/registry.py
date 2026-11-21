# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""The project registry: projects, their members and the sessions of researchers."""

from __future__ import annotations

import secrets
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS project (
    id TEXT PRIMARY KEY,
    billing_attribution_ref TEXT NOT NULL,
    policy_ref TEXT NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS member (
    project_id TEXT NOT NULL REFERENCES project (id) ON DELETE CASCADE,
    subject TEXT NOT NULL,
    PRIMARY KEY (project_id, subject)
) STRICT;
CREATE TABLE IF NOT EXISTS session (
    id TEXT PRIMARY KEY,
    subject TEXT NOT NULL,
    project_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (subject, project_id)
) STRICT;
"""


class NotAMemberError(Exception):
    """The subject is not a member of the project, or the project does not exist."""


@dataclass(frozen=True)
class Project:
    """A platform project with its billing binding and policy."""

    id: str
    billing_attribution_ref: str
    policy_ref: str
    members: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Session:
    """The session of one researcher in one project."""

    id: str
    subject: str
    project: str
    created_at: datetime


class Registry:
    """Projects, members and sessions in one SQLite database."""

    def __init__(self, path: Path) -> None:
        """Open the database at a path and create its tables."""
        self._path = path
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

    def synchronize(self, projects: Iterable[Project]) -> None:
        """Replace the projects and their members with the configured ones.

        Sessions are kept. A session of a removed member can no longer be launched.
        """
        with self._transaction() as connection:
            connection.execute("DELETE FROM project")
            for project in projects:
                connection.execute(
                    "INSERT INTO project VALUES (?, ?, ?)",
                    (project.id, project.billing_attribution_ref, project.policy_ref),
                )
                connection.executemany(
                    "INSERT INTO member VALUES (?, ?)",
                    [(project.id, subject) for subject in sorted(project.members)],
                )

    def projects_of(self, subject: str) -> list[Project]:
        """Return the projects that a subject is a member of."""
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT id, billing_attribution_ref, policy_ref FROM project"
                " JOIN member ON member.project_id = project.id"
                " WHERE member.subject = ? ORDER BY id",
                (subject,),
            ).fetchall()
        return [Project(*row) for row in rows]

    def project(self, subject: str, project: str) -> Project:
        """Return a project of which the subject is a member."""
        for candidate in self.projects_of(subject):
            if candidate.id == project:
                return candidate
        raise NotAMemberError

    def session(self, subject: str, project: str, *, create: bool = False) -> Session | None:
        """Return the session of a subject in a project, creating it on request."""
        with self._transaction() as connection:
            if create:
                connection.execute(
                    "INSERT OR IGNORE INTO session VALUES (?, ?, ?, ?)",
                    (secrets.token_hex(8), subject, project, datetime.now(UTC).isoformat()),
                )
            row = connection.execute(
                "SELECT id, subject, project_id, created_at FROM session"
                " WHERE subject = ? AND project_id = ?",
                (subject, project),
            ).fetchone()
        if row is None:
            return None
        return Session(row[0], row[1], row[2], datetime.fromisoformat(row[3]))

    def delete_session(self, subject: str, project: str) -> None:
        """Remove the session of a subject in a project."""
        with self._transaction() as connection:
            connection.execute(
                "DELETE FROM session WHERE subject = ? AND project_id = ?", (subject, project)
            )
