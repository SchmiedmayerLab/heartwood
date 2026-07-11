# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Append-only hash-chained audit log persistence."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import cast

from heartwood.schemas import AuditEvent, JsonValue


class AuditIntegrityError(ValueError):
    """Raised when an audit log hash chain is malformed or tampered."""


def _canonical_event_payload(event: AuditEvent) -> str:
    payload = event.model_dump(mode="json")
    payload["event_hash"] = None
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def compute_event_hash(event: AuditEvent) -> str:
    """Return the deterministic SHA-256 hash for an audit event."""
    digest = hashlib.sha256(_canonical_event_payload(event).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


class AuditLog:
    """Append-only JSONL audit log with hash-chain verification."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def read(self) -> tuple[AuditEvent, ...]:
        """Read all events from disk."""
        if not self.path.exists():
            return ()
        return tuple(
            AuditEvent.model_validate_json(line)
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line
        )

    def append(
        self,
        *,
        session_id: str,
        event_type: str,
        occurred_at: str,
        payload: dict[str, JsonValue] | None = None,
    ) -> AuditEvent:
        """Append a scrubbed event and return the persisted record."""
        events = self.read()
        if events:
            self.verify(events)
        safe_payload = (
            {} if payload is None else cast(dict[str, JsonValue], scrub_json_value(payload))
        )
        sequence = len(events)
        previous_event_hash = events[-1].event_hash if events else None
        event = AuditEvent(
            event_id=f"{session_id}-audit-{sequence:06d}",
            session_id=session_id,
            sequence=sequence,
            event_type=event_type,
            occurred_at=occurred_at,
            payload=safe_payload,
            previous_event_hash=previous_event_hash,
            event_hash=None,
        )
        event = event.model_copy(update={"event_hash": compute_event_hash(event)})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.path, flags, 0o600)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as file:
            file.write(event.model_dump_json() + "\n")
        return event

    def verify(self, events: tuple[AuditEvent, ...] | None = None) -> None:
        """Verify event sequence numbers and hash-chain links."""
        records = self.read() if events is None else events
        previous_hash: str | None = None
        for expected_sequence, event in enumerate(records):
            if event.sequence != expected_sequence:
                msg = f"audit sequence gap at {event.event_id}"
                raise AuditIntegrityError(msg)
            if event.previous_event_hash != previous_hash:
                msg = f"audit previous hash mismatch at {event.event_id}"
                raise AuditIntegrityError(msg)
            if event.event_hash != compute_event_hash(event):
                msg = f"audit event hash mismatch at {event.event_id}"
                raise AuditIntegrityError(msg)
            previous_hash = event.event_hash

    def export_jsonl(self) -> str:
        """Return a JSONL export of the current scrubbed audit log."""
        events = self.read()
        self.verify(events)
        exported: list[str] = []
        for event in events:
            payload: dict[str, JsonValue] = event.model_dump(mode="json")
            exported.append(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return "\n".join(exported) + ("\n" if exported else "")


_SENSITIVE_KEYS = {
    "api_key",
    "authorization",
    "client_secret",
    "content",
    "date_of_birth",
    "dob",
    "email",
    "mrn",
    "name",
    "password",
    "path",
    "patient_id",
    "person_id",
    "prompt",
    "record",
    "records",
    "response",
    "result",
    "results",
    "row",
    "rows",
    "secret",
    "summary",
    "table_rows",
    "token",
    "value",
    "values",
}
_SENSITIVE_NORMALIZED_KEYS = {
    "".join(character for character in key if character.isalnum()) for key in _SENSITIVE_KEYS
}


def scrub_json_value(value: JsonValue) -> JsonValue:
    """Recursively scrub values under sensitive payload keys."""
    if isinstance(value, dict):
        scrubbed: dict[str, JsonValue] = {}
        for key, item in value.items():
            normalized_key = str(key)
            scrubbed[normalized_key] = (
                "[scrubbed]" if _is_sensitive_key(normalized_key) else scrub_json_value(item)
            )
        return scrubbed
    if isinstance(value, list):
        return [scrub_json_value(item) for item in value]
    return value


def _is_sensitive_key(key: str) -> bool:
    normalized = "".join(character for character in key.lower() if character.isalnum())
    return (
        normalized in _SENSITIVE_NORMALIZED_KEYS
        or "password" in normalized
        or "secret" in normalized
        or normalized.endswith(("apikey", "token"))
    )
