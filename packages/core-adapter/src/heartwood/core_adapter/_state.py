# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Workspace-disk session state persistence."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TextIO

from heartwood.session import SessionEvent, validate_session_id


class SessionStoreBoundaryError(ValueError):
    """Raised when session state would escape the configured workspace root."""


class FileSessionStore:
    """Persist session commands and events as JSONL under a workspace directory."""

    def __init__(self, root: Path, session_id: str) -> None:
        """Initialize a root-confined session store."""
        try:
            validate_session_id(session_id)
        except ValueError as error:
            raise SessionStoreBoundaryError(str(error)) from error
        self.root = root.resolve()
        self.session_id = session_id
        session_path = self.root / session_id
        if session_path.is_symlink():
            msg = f"session path must not be a symbolic link: {session_id}"
            raise SessionStoreBoundaryError(msg)
        self.session_dir = session_path.resolve()
        if self.session_dir != self.root and self.root not in self.session_dir.parents:
            msg = f"session path escapes workspace root: {session_id}"
            raise SessionStoreBoundaryError(msg)
        self.events_path = self.session_dir / "events.jsonl"
        self.audit_path = self.session_dir / "audit.jsonl"
        self.audit_export_path = self.session_dir / "audit-export.jsonl"
        self._next_sequence: int | None = None

    def append_event(self, event: SessionEvent) -> None:
        """Persist one session event envelope."""
        self._prepare_session_dir()
        with _open_private_text(self.events_path, append=True) as file:
            file.write(event.model_dump_json() + "\n")
        if self._next_sequence is not None:
            self._next_sequence = max(self._next_sequence, event.sequence + 1)

    def read_events(self) -> tuple[SessionEvent, ...]:
        """Read persisted session events."""
        try:
            with _open_private_read(self.events_path) as file:
                lines = file.read().splitlines()
        except FileNotFoundError:
            return ()
        return tuple(SessionEvent.model_validate_json(line) for line in lines if line)

    def next_sequence(self) -> int:
        """Return the next sequence without advancing until the event is durable."""
        if self._next_sequence is None:
            self._next_sequence = len(self.read_events())
        return self._next_sequence

    def write_audit_export(self, content: str) -> None:
        """Write the scrubbed audit export as an owner-only file."""
        self._prepare_session_dir()
        with _open_private_text(self.audit_export_path, append=False) as file:
            file.write(content)

    def read_audit_export(self) -> str:
        """Read the generated audit export without following symbolic links."""
        with _open_private_read(self.audit_export_path) as file:
            return file.read()

    def _prepare_session_dir(self) -> None:
        self.session_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.session_dir.chmod(0o700)


def _open_private_text(path: Path, *, append: bool) -> TextIO:
    flags = os.O_CREAT | os.O_WRONLY | (os.O_APPEND if append else os.O_TRUNC)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    os.fchmod(descriptor, 0o600)
    return os.fdopen(descriptor, "a" if append else "w", encoding="utf-8")


def _open_private_read(path: Path) -> TextIO:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    return os.fdopen(descriptor, encoding="utf-8")
