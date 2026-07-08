# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Workspace-disk session state persistence."""

from __future__ import annotations

import re
from pathlib import Path

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

    def append_command(self, command: SessionCommand) -> None:
        """Persist one command envelope."""
        self.session_dir.mkdir(parents=True, exist_ok=True)
        safe_payload = scrub_json_value(command.payload)
        safe_command = command.model_copy(update={"payload": safe_payload})
        with self.commands_path.open("a", encoding="utf-8") as file:
            file.write(safe_command.model_dump_json() + "\n")

    def append_event(self, event: SessionEvent) -> None:
        """Persist one session event envelope."""
        self.session_dir.mkdir(parents=True, exist_ok=True)
        with self.events_path.open("a", encoding="utf-8") as file:
            file.write(event.model_dump_json() + "\n")

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
        return len(self.read_events())
