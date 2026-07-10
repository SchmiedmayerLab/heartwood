# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Workspace-disk session state persistence."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import TextIO

from heartwood.audit import scrub_json_value
from heartwood.session import SessionCommand, SessionEvent

_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class SessionStoreBoundaryError(ValueError):
    """Raised when session state would escape the configured workspace root."""


class FileSessionStore:
    """Persist session commands and events as JSONL under a workspace directory."""

    def __init__(self, root: Path, session_id: str) -> None:
        """Initialize a root-confined session store."""
        if not _SESSION_ID.fullmatch(session_id):
            msg = f"invalid session id: {session_id}"
            raise SessionStoreBoundaryError(msg)
        self.root = root.resolve()
        self.session_id = session_id
        self.session_dir = (self.root / session_id).resolve()
        if self.session_dir != self.root and self.root not in self.session_dir.parents:
            msg = f"session path escapes workspace root: {session_id}"
            raise SessionStoreBoundaryError(msg)
        self.commands_path = self.session_dir / "commands.jsonl"
        self.events_path = self.session_dir / "events.jsonl"
        self.audit_path = self.session_dir / "audit.jsonl"
        self.audit_export_path = self.session_dir / "audit-export.jsonl"
        self._next_sequence: int | None = None

    def append_command(self, command: SessionCommand) -> None:
        """Persist one command envelope."""
        self._prepare_session_dir()
        safe_payload = scrub_json_value(command.payload)
        safe_command = command.model_copy(update={"payload": safe_payload})
        with _open_private_text(self.commands_path, append=True) as file:
            file.write(safe_command.model_dump_json() + "\n")

    def append_event(self, event: SessionEvent) -> None:
        """Persist one session event envelope."""
        self._prepare_session_dir()
        with _open_private_text(self.events_path, append=True) as file:
            file.write(event.model_dump_json() + "\n")
        if self._next_sequence is not None:
            self._next_sequence = max(self._next_sequence, event.sequence + 1)

    def read_events(self) -> tuple[SessionEvent, ...]:
        """Read persisted session events."""
        if not self.events_path.exists():
            return ()
        return tuple(
            SessionEvent.model_validate_json(line)
            for line in self.events_path.read_text(encoding="utf-8").splitlines()
            if line
        )

    def next_sequence(self) -> int:
        """Return the next session-event sequence number."""
        if self._next_sequence is None:
            self._next_sequence = len(self.read_events())
        sequence = self._next_sequence
        self._next_sequence += 1
        return sequence

    def write_audit_export(self, content: str) -> None:
        """Write the scrubbed audit export as an owner-only file."""
        self._prepare_session_dir()
        with _open_private_text(self.audit_export_path, append=False) as file:
            file.write(content)

    def _prepare_session_dir(self) -> None:
        self.session_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.session_dir.chmod(0o700)


def _open_private_text(path: Path, *, append: bool) -> TextIO:
    flags = os.O_CREAT | os.O_WRONLY | (os.O_APPEND if append else os.O_TRUNC)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    os.fchmod(descriptor, 0o600)
    return os.fdopen(descriptor, "a" if append else "w", encoding="utf-8")
