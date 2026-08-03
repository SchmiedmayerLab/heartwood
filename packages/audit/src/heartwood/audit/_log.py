# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Append-only hash-chained audit log persistence."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from heartwood.persistence import (
    AUDIT_EVENT_KIND,
    PERSISTENCE_MIGRATIONS,
    AppendRecoveryError,
    LockedJsonlStore,
    MigrationError,
    NativeLockUnavailableError,
)
from heartwood.schemas import AuditEvent, JsonValue


class AuditIntegrityError(ValueError):
    """Raised when an audit log hash chain is malformed or tampered."""


@dataclass(frozen=True, slots=True)
class AuditVerification:
    """Verified identity of one complete canonical audit stream."""

    event_count: int
    terminal_event_hash: str | None
    content_sha256: str
    size_bytes: int


def _canonical_event_payload(event: AuditEvent) -> str:
    payload = event.model_dump(mode="json")
    payload["event_hash"] = None
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def compute_event_hash(event: AuditEvent) -> str:
    """Return the deterministic SHA-256 hash for an audit event."""
    digest = hashlib.sha256(_canonical_event_payload(event).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def prepare_audit_event(
    *,
    session_id: str,
    sequence: int,
    previous_event_hash: str | None,
    event_type: str,
    occurred_at: str,
    payload: dict[str, JsonValue] | None = None,
) -> AuditEvent:
    """Build one scrubbed audit event from an already verified chain head."""
    safe_payload = {} if payload is None else cast(dict[str, JsonValue], scrub_json_value(payload))
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
    return event.model_copy(update={"event_hash": compute_event_hash(event)})


class AuditLog:
    """Append-only JSONL audit log with recoverable append and full verification."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._store = LockedJsonlStore(path)

    def read(self) -> tuple[AuditEvent, ...]:
        """Read one stable, append-recovered event snapshot from disk."""
        try:
            payloads = self._store.read_objects()
            return tuple(_audit_event(payload) for payload in payloads)
        except NativeLockUnavailableError:
            raise
        except (AppendRecoveryError, MigrationError, ValueError) as error:
            if isinstance(error, AuditIntegrityError):
                raise
            raise AuditIntegrityError(f"audit log is malformed: {self.path}") from error

    def append(
        self,
        *,
        session_id: str,
        event_type: str,
        occurred_at: str,
        payload: dict[str, JsonValue] | None = None,
    ) -> AuditEvent:
        """Incrementally append a scrubbed event under the log's native lock."""

        def build(records: tuple[dict[str, object], ...]) -> dict[str, object]:
            events = tuple(_audit_event(record) for record in records)
            if events:
                _verify_tail(events)
                if any(event.session_id != session_id for event in events):
                    raise AuditIntegrityError("audit append session does not match existing log")
            event = prepare_audit_event(
                session_id=session_id,
                sequence=len(events),
                previous_event_hash=events[-1].event_hash if events else None,
                event_type=event_type,
                occurred_at=occurred_at,
                payload=payload,
            )
            return event.model_dump(mode="json")

        try:
            persisted = self._store.append_derived(build)
        except NativeLockUnavailableError:
            raise
        except (AppendRecoveryError, MigrationError, ValueError) as error:
            if isinstance(error, AuditIntegrityError):
                raise
            raise AuditIntegrityError(f"audit log is malformed: {self.path}") from error
        return _audit_event(persisted)

    def prepare(
        self,
        *,
        session_id: str,
        event_type: str,
        occurred_at: str,
        payload: dict[str, JsonValue] | None = None,
    ) -> AuditEvent:
        """Build the next record after explicitly verifying the complete chain."""
        events = self.read()
        self.verify(events)
        return prepare_audit_event(
            session_id=session_id,
            sequence=len(events),
            previous_event_hash=events[-1].event_hash if events else None,
            event_type=event_type,
            occurred_at=occurred_at,
            payload=payload,
        )

    def verify(self, events: tuple[AuditEvent, ...] | None = None) -> AuditVerification:
        """Fully verify sequence, hash links, and the canonical stream digest."""
        records = self.read() if events is None else events
        return verify_audit_events(records)

    def export_jsonl(self) -> str:
        """Return a fully verified canonical export of the scrubbed audit log."""
        events = self.read()
        self.verify(events)
        return canonical_audit_jsonl(events)


def verify_audit_events(events: tuple[AuditEvent, ...]) -> AuditVerification:
    """Fully verify an in-memory audit stream and return its stable identity."""
    previous_hash: str | None = None
    session_id: str | None = None
    for expected_sequence, event in enumerate(events):
        if session_id is None:
            session_id = event.session_id
        elif event.session_id != session_id:
            raise AuditIntegrityError(f"audit session mismatch at {event.event_id}")
        if event.sequence != expected_sequence:
            raise AuditIntegrityError(f"audit sequence gap at {event.event_id}")
        if event.previous_event_hash != previous_hash:
            raise AuditIntegrityError(f"audit previous hash mismatch at {event.event_id}")
        if event.event_hash != compute_event_hash(event):
            raise AuditIntegrityError(f"audit event hash mismatch at {event.event_id}")
        previous_hash = event.event_hash
    canonical = canonical_audit_jsonl(events).encode("utf-8")
    return AuditVerification(
        event_count=len(events),
        terminal_event_hash=previous_hash,
        content_sha256=f"sha256:{hashlib.sha256(canonical).hexdigest()}",
        size_bytes=len(canonical),
    )


def verify_audit_jsonl(content: str) -> tuple[tuple[AuditEvent, ...], AuditVerification]:
    """Parse and fully verify one canonical or equivalent JSON Lines audit export."""
    try:
        events = tuple(
            _audit_event(json.loads(line)) for line in content.splitlines() if line.strip()
        )
    except (json.JSONDecodeError, MigrationError, ValueError) as error:
        raise AuditIntegrityError("audit export is malformed") from error
    return events, verify_audit_events(events)


def canonical_audit_jsonl(events: tuple[AuditEvent, ...]) -> str:
    """Serialize audit events as deterministic JSON Lines."""
    return "".join(
        json.dumps(
            event.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        + "\n"
        for event in events
    )


def _audit_event(payload: object) -> AuditEvent:
    if not isinstance(payload, dict) or not all(isinstance(key, str) for key in payload):
        raise AuditIntegrityError("audit record must be an object")
    migrated = PERSISTENCE_MIGRATIONS.migrate(AUDIT_EVENT_KIND, payload)
    return AuditEvent.model_validate(migrated.payload)


def _verify_tail(events: tuple[AuditEvent, ...]) -> None:
    last = events[-1]
    if last.sequence != len(events) - 1 or last.event_hash != compute_event_hash(last):
        raise AuditIntegrityError(f"audit tail is invalid at {last.event_id}")
    if len(events) > 1 and last.previous_event_hash != events[-2].event_hash:
        raise AuditIntegrityError(f"audit tail link is invalid at {last.event_id}")


_SENSITIVE_KEYS = {
    "api_key",
    "arguments",
    "authorization",
    "client_secret",
    "content",
    "date_of_birth",
    "dob",
    "email",
    "individual_id",
    "mrn",
    "name",
    "password",
    "path",
    "patient_id",
    "participant",
    "participant_id",
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
    "subject",
    "subject_id",
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
