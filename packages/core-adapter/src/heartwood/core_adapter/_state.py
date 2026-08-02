# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Workspace-disk session state persistence."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, overload

from filelock import FileLock
from filelock import Timeout as FileLockTimeout

from heartwood.audit import (
    AuditIntegrityError,
    AuditLog,
    AuditVerification,
    canonical_audit_jsonl,
    compute_event_hash,
)
from heartwood.persistence import (
    AUDIT_EVENT_KIND,
    PERSISTENCE_MIGRATIONS,
    SESSION_COMMAND_RECEIPT_KIND,
    SESSION_COMMAND_RECEIPT_VERSION,
    SESSION_COMMIT_KIND,
    SESSION_COMMIT_VERSION,
    SESSION_EVENT_KIND,
    SESSION_WRITER_KIND,
    SESSION_WRITER_VERSION,
    MigrationError,
    NativeLockUnavailableError,
    append_private_bytes,
    fsync_directory,
    read_private_json,
    read_private_text,
    truncate_private_file,
    write_private_json_atomic,
    write_private_text_atomic,
)
from heartwood.schemas import AuditEvent
from heartwood.session import SessionEvent, compute_session_event_hash, validate_session_id


class SessionStoreBoundaryError(ValueError):
    """Raised when session state would escape the configured workspace root."""


class SessionOwnershipError(RuntimeError):
    """Raised when another process owns the session writer lease."""


class SessionRecoveryError(ValueError):
    """Raised when an interrupted session commit cannot be recovered safely."""


class SessionStorageCapabilityError(SessionRecoveryError):
    """Raised when session storage cannot provide required durability semantics."""


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
        self.writer_lock_path = self.session_dir / ".writer.lock"
        self.writer_metadata_path = self.session_dir / ".writer.json"
        self.snapshot_lock_path = self.session_dir / ".snapshot.lock"
        self.pending_commit_path = self.session_dir / ".pending-commit.json"
        self.commands_dir = self.session_dir / ".commands"
        self._next_sequence: int | None = None
        self._last_audit_hash: str | None = None
        self._writer_lock: FileLock | None = None
        self._snapshot_lock = FileLock(
            self.snapshot_lock_path,
            mode=0o600,
            fallback_to_soft=False,
        )
        self._writer_token: str | None = None
        self.recovered_stale_writer = False

    @property
    def owns_writer(self) -> bool:
        """Return whether this store currently owns the session writer lease."""
        return self._writer_lock is not None

    def acquire_writer(self) -> None:
        """Acquire the exclusive session writer lease without waiting."""
        if self._writer_lock is not None:
            return
        self._prepare_session_dir()
        if self.writer_lock_path.is_symlink():
            msg = f"session writer lock must not be a symbolic link: {self.session_id}"
            raise SessionStoreBoundaryError(msg)
        lock = FileLock(
            self.writer_lock_path,
            timeout=0,
            mode=0o600,
            fallback_to_soft=False,
            thread_local=False,
        )
        try:
            lock.acquire()
        except FileLockTimeout as error:
            owner = _writer_owner_summary(self.writer_metadata_path)
            raise SessionOwnershipError(
                f"session {self.session_id} is active in another Heartwood process{owner}; "
                "stop that process before continuing"
            ) from error
        except OSError as error:
            raise SessionStorageCapabilityError(
                f"session storage does not support required process locks: {self.session_id}"
            ) from error
        try:
            self.recovered_stale_writer = self.writer_metadata_path.exists()
            self.writer_lock_path.chmod(0o600)
            token = uuid.uuid4().hex
            _write_private_json_atomic(
                self.writer_metadata_path,
                {
                    "schema_version": SESSION_WRITER_VERSION,
                    "session_id": self.session_id,
                    "pid": os.getpid(),
                    "host": socket.gethostname(),
                    "started_at": _utc_now(),
                    "token": token,
                },
            )
        except Exception:
            lock.release()
            raise
        self._writer_lock = lock
        self._writer_token = token

    def release_writer(self) -> None:
        """Release the session writer lease owned by this store."""
        lock = self._writer_lock
        if lock is None:
            return
        try:
            with suppress(OSError, ValueError):
                metadata = _writer_record(_read_private_json(self.writer_metadata_path))
                if metadata.get("token") == self._writer_token:
                    self.writer_metadata_path.unlink(missing_ok=True)
                    fsync_directory(self.session_dir)
        finally:
            self._writer_lock = None
            self._writer_token = None
            lock.release()

    @contextmanager
    def snapshot(self) -> Iterator[None]:
        """Hold a stable paired view of the session event and audit logs."""
        self._prepare_session_dir()
        if self.snapshot_lock_path.is_symlink():
            raise SessionStoreBoundaryError(
                f"session snapshot lock must not be a symbolic link: {self.session_id}"
            )
        try:
            lock = self._snapshot_lock.acquire(timeout=-1)
        except OSError as error:
            raise SessionStorageCapabilityError(
                f"session storage does not support required process locks: {self.session_id}"
            ) from error
        with lock:
            self.snapshot_lock_path.chmod(0o600)
            yield

    def commit_event(self, event: SessionEvent, audit_event: AuditEvent) -> None:
        """Commit one session event and its audit record through a recovery journal."""
        if not self.owns_writer:
            raise SessionOwnershipError("session event commits require the writer lease")
        if event.session_id != self.session_id or audit_event.session_id != self.session_id:
            raise SessionRecoveryError("pending commit session does not match the session store")
        if event.sequence != audit_event.sequence:
            raise SessionRecoveryError("pending event and audit sequences do not match")
        _verify_pending_pair(event, audit_event)
        with self.snapshot():
            if self.pending_commit_path.exists():
                self._recover_pending_commit_locked()
            _write_private_json_atomic(
                self.pending_commit_path,
                {
                    "schema_version": SESSION_COMMIT_VERSION,
                    "session_event": event.model_dump(mode="json"),
                    "audit_event": audit_event.model_dump(mode="json"),
                },
            )
            _append_private_json_line(self.audit_path, audit_event.model_dump_json())
            _append_private_json_line(self.events_path, event.model_dump_json())
            self.pending_commit_path.unlink()
            fsync_directory(self.session_dir)
            self._next_sequence = event.sequence + 1
            self._last_audit_hash = audit_event.event_hash

    def recover_pending_commit(self) -> bool:
        """Complete one interrupted paired event and audit append."""
        if not self.owns_writer:
            raise SessionRecoveryError(
                f"session {self.session_id} requires writer-owned commit recovery"
            )
        with self.snapshot():
            return self._recover_pending_commit_locked()

    def _recover_pending_commit_locked(self) -> bool:
        if not self.pending_commit_path.exists():
            return False
        try:
            payload = PERSISTENCE_MIGRATIONS.migrate(
                SESSION_COMMIT_KIND,
                _read_private_json(self.pending_commit_path),
            ).payload
            event = _session_event(payload.get("session_event"))
            audit_event = _audit_event(payload.get("audit_event"))
        except MigrationError as error:
            raise SessionRecoveryError("unsupported pending session commit") from error
        except (OSError, ValueError) as error:
            if isinstance(error, SessionRecoveryError):
                raise
            raise SessionRecoveryError("pending session commit is malformed") from error
        if (
            event.session_id != self.session_id
            or audit_event.session_id != self.session_id
            or event.sequence != audit_event.sequence
        ):
            raise SessionRecoveryError("pending session commit identity does not match")
        _verify_pending_pair(event, audit_event)

        events, events_truncate_at = _read_recoverable_records(
            self.events_path,
            SessionEvent,
        )
        audit_events, audit_truncate_at = _read_recoverable_records(
            self.audit_path,
            AuditEvent,
        )
        _verify_recovery_prefix(audit_events, events)
        sequence = event.sequence
        _validate_pending_position("session event", events, event, sequence)
        _validate_pending_position("audit", audit_events, audit_event, sequence)
        if sequence:
            previous_audit = audit_events[sequence - 1]
            if (
                previous_audit.event_hash != audit_event.previous_event_hash
                or previous_audit.event_hash != event.previous_event_hash
            ):
                raise SessionRecoveryError("pending commit previous hash does not match")
        elif audit_event.previous_event_hash is not None or event.previous_event_hash is not None:
            raise SessionRecoveryError("first pending commit must not reference a previous hash")
        if audit_truncate_at is not None:
            truncate_private_file(self.audit_path, audit_truncate_at)
        if events_truncate_at is not None:
            truncate_private_file(self.events_path, events_truncate_at)
        if len(audit_events) == sequence:
            _append_private_json_line(self.audit_path, audit_event.model_dump_json())
        if len(events) == sequence:
            _append_private_json_line(self.events_path, event.model_dump_json())
        self.pending_commit_path.unlink()
        fsync_directory(self.session_dir)
        self._next_sequence = sequence + 1
        self._last_audit_hash = audit_event.event_hash
        return True

    def command_record(self, command_id: str) -> dict[str, Any] | None:
        """Return one durable command receipt."""
        path = self._command_path(command_id)
        if not path.exists():
            return None
        try:
            payload = _command_record(_read_private_json(path))
        except (OSError, ValueError) as error:
            raise SessionRecoveryError(f"command receipt is malformed for {command_id}") from error
        _validate_command_record(payload, command_id)
        if payload.get("session_id") != self.session_id:
            raise SessionRecoveryError(
                f"command receipt belongs to another session for {command_id}"
            )
        return payload

    def unresolved_command_ids(self) -> tuple[str, ...]:
        """Return accepted command identifiers that have no completion receipt."""
        if self.commands_dir.is_symlink():
            raise SessionStoreBoundaryError(
                f"session command receipt path must not be a symbolic link: {self.session_id}"
            )
        if not self.commands_dir.exists():
            return ()
        if not self.commands_dir.is_dir():
            raise SessionStoreBoundaryError(
                f"session command receipt path must be a directory: {self.session_id}"
            )
        unresolved: list[tuple[int, str]] = []
        for path in self.commands_dir.glob("*.json"):
            try:
                payload = _command_record(_read_private_json(path))
                command_id = payload.get("command_id")
                if not isinstance(command_id, str):
                    raise SessionRecoveryError(f"invalid command receipt in {path}")
                _validate_command_record(payload, command_id)
            except (OSError, ValueError) as error:
                if isinstance(error, SessionRecoveryError):
                    raise
                raise SessionRecoveryError(f"command receipt is malformed in {path}") from error
            if payload.get("session_id") != self.session_id:
                raise SessionRecoveryError(f"command receipt belongs to another session in {path}")
            if payload["state"] == "accepted":
                unresolved.append((payload["first_sequence"], command_id))
        return tuple(command_id for _, command_id in sorted(unresolved))

    def accept_command(
        self,
        *,
        command_id: str,
        command_hash: str,
        first_sequence: int,
    ) -> None:
        """Persist command intent before model or tool execution."""
        if not self.owns_writer:
            raise SessionOwnershipError("command acceptance requires the writer lease")
        if self.command_record(command_id) is not None:
            raise SessionRecoveryError(f"command receipt already exists for {command_id}")
        self._prepare_commands_dir()
        _write_private_json_atomic(
            self._command_path(command_id),
            {
                "schema_version": SESSION_COMMAND_RECEIPT_VERSION,
                "session_id": self.session_id,
                "command_id": command_id,
                "command_hash": command_hash,
                "state": "accepted",
                "first_sequence": first_sequence,
                "last_sequence": None,
            },
        )

    def complete_command(
        self,
        *,
        command_id: str,
        command_hash: str,
        first_sequence: int,
        last_sequence: int,
    ) -> None:
        """Mark one accepted command complete after all events are durable."""
        if not self.owns_writer:
            raise SessionOwnershipError("command completion requires the writer lease")
        if last_sequence < first_sequence:
            raise SessionRecoveryError("completed command sequence is invalid")
        record = self.command_record(command_id)
        if (
            record is None
            or record.get("command_hash") != command_hash
            or record.get("state") != "accepted"
            or record.get("first_sequence") != first_sequence
        ):
            raise SessionRecoveryError(f"accepted command receipt changed for {command_id}")
        _write_private_json_atomic(
            self._command_path(command_id),
            {
                **record,
                "state": "completed",
                "last_sequence": last_sequence,
            },
        )

    def record_completed_legacy_command(
        self,
        *,
        command_id: str,
        command_hash: str,
        first_sequence: int,
        last_sequence: int,
    ) -> None:
        """Create a completed receipt for an event-only command."""
        if not self.owns_writer:
            raise SessionOwnershipError("command migration requires the writer lease")
        if self.command_record(command_id) is not None:
            return
        self._prepare_commands_dir()
        _write_private_json_atomic(
            self._command_path(command_id),
            {
                "schema_version": SESSION_COMMAND_RECEIPT_VERSION,
                "session_id": self.session_id,
                "command_id": command_id,
                "command_hash": command_hash,
                "state": "completed",
                "first_sequence": first_sequence,
                "last_sequence": last_sequence,
            },
        )

    def read_events(self) -> tuple[SessionEvent, ...]:
        """Read persisted session events."""
        try:
            lines = read_private_text(self.events_path).splitlines()
        except FileNotFoundError:
            return ()
        return tuple(_session_event_json(line) for line in lines if line)

    def replay_events(self) -> tuple[SessionEvent, ...]:
        """Recover if needed, then return one verified event-and-audit snapshot."""
        events, _audit_events, _verification = self._verified_records()
        return events

    def verified_audit_export(self) -> tuple[str, AuditVerification]:
        """Return a stable canonical export after full paired-log verification."""
        _events, audit_events, verification = self._verified_records()
        return canonical_audit_jsonl(audit_events), verification

    def _verified_records(
        self,
    ) -> tuple[tuple[SessionEvent, ...], tuple[AuditEvent, ...], AuditVerification]:
        if not self.session_dir.exists():
            verification = AuditLog(self.audit_path).verify(())
            return (), (), verification
        acquired_writer = False
        try:
            while True:
                with self.snapshot():
                    if not self.pending_commit_path.exists():
                        try:
                            audit_log = AuditLog(self.audit_path)
                            audit_events = audit_log.read()
                            verification = audit_log.verify(audit_events)
                            events = self.read_events()
                        except NativeLockUnavailableError as error:
                            raise SessionStorageCapabilityError(
                                "session storage does not support required process locks: "
                                f"{self.session_id}"
                            ) from error
                        except (OSError, UnicodeDecodeError, ValueError) as error:
                            if isinstance(error, AuditIntegrityError):
                                raise
                            raise SessionRecoveryError(
                                f"session {self.session_id} records are malformed"
                            ) from error
                        _verify_event_correspondence(audit_events, events)
                        self._next_sequence = len(events)
                        self._last_audit_hash = (
                            audit_events[-1].event_hash if audit_events else None
                        )
                        return events, audit_events, verification
                if not self.owns_writer:
                    self.acquire_writer()
                    acquired_writer = True
                self.recover_pending_commit()
        finally:
            if acquired_writer:
                self.release_writer()

    def next_sequence(self) -> int:
        """Return the next sequence without advancing until the event is durable."""
        if self._next_sequence is None:
            self.replay_events()
        if self._next_sequence is None:  # pragma: no cover - replay invariant
            raise SessionRecoveryError("session sequence could not be initialized")
        return self._next_sequence

    def verified_head(self) -> tuple[int, str | None]:
        """Return the sequence and audit hash established by full replay verification."""
        return self.next_sequence(), self._last_audit_hash

    def write_audit_export(self, content: str) -> None:
        """Write the scrubbed audit export as an owner-only file."""
        if not self.owns_writer:
            raise SessionOwnershipError("audit export writes require the writer lease")
        self._prepare_session_dir()
        _write_private_text_atomic(self.audit_export_path, content)

    def read_audit_export(self) -> str:
        """Read the generated audit export without following symbolic links."""
        return read_private_text(self.audit_export_path)

    def _prepare_session_dir(self) -> None:
        if self.session_dir.is_symlink():
            raise SessionStoreBoundaryError(
                f"session path must not be a symbolic link: {self.session_id}"
            )
        self.session_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.session_dir.chmod(0o700)

    def _prepare_commands_dir(self) -> None:
        if self.commands_dir.is_symlink():
            raise SessionStoreBoundaryError(
                f"session command receipt path must not be a symbolic link: {self.session_id}"
            )
        self.commands_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not self.commands_dir.is_dir():
            raise SessionStoreBoundaryError(
                f"session command receipt path must be a directory: {self.session_id}"
            )
        self.commands_dir.chmod(0o700)

    def _command_path(self, command_id: str) -> Path:
        if self.commands_dir.is_symlink():
            raise SessionStoreBoundaryError(
                f"session command receipt path must not be a symbolic link: {self.session_id}"
            )
        digest = hashlib.sha256(command_id.encode("utf-8")).hexdigest()
        return self.commands_dir / f"{digest}.json"


def _append_private_json_line(path: Path, content: str) -> None:
    append_private_bytes(path, (content + "\n").encode("utf-8"))


def _write_private_text_atomic(path: Path, content: str) -> None:
    write_private_text_atomic(path, content)


def _write_private_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    write_private_json_atomic(path, payload)


def _read_private_json(path: Path) -> dict[str, Any]:
    return read_private_json(path)


@overload
def _read_recoverable_records(
    path: Path,
    record_type: type[SessionEvent],
) -> tuple[tuple[SessionEvent, ...], int | None]: ...


@overload
def _read_recoverable_records(
    path: Path,
    record_type: type[AuditEvent],
) -> tuple[tuple[AuditEvent, ...], int | None]: ...


def _read_recoverable_records(
    path: Path,
    record_type: type[SessionEvent] | type[AuditEvent],
) -> tuple[tuple[SessionEvent, ...] | tuple[AuditEvent, ...], int | None]:
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return (), None
    try:
        content = bytearray()
        while chunk := os.read(descriptor, 64 * 1024):
            content.extend(chunk)
    finally:
        os.close(descriptor)
    truncate_at: int | None = None
    if content and not content.endswith(b"\n"):
        boundary = content.rfind(b"\n") + 1
        truncate_at = boundary
        content = content[:boundary]
    try:
        lines = tuple(line for line in content.decode("utf-8").splitlines() if line)
        if record_type is SessionEvent:
            return tuple(_session_event_json(line) for line in lines), truncate_at
        return tuple(_audit_event_json(line) for line in lines), truncate_at
    except (UnicodeDecodeError, ValueError) as error:
        raise SessionRecoveryError(f"unable to recover session records in {path}") from error


def _validate_pending_position[Record](
    label: str,
    records: tuple[Record, ...],
    pending: Record,
    sequence: int,
) -> None:
    if len(records) == sequence:
        return
    if len(records) == sequence + 1 and records[-1] == pending:
        return
    raise SessionRecoveryError(f"{label} log does not match the pending commit")


def _verify_recovery_prefix(
    audit_events: tuple[AuditEvent, ...],
    events: tuple[SessionEvent, ...],
) -> None:
    previous_hash: str | None = None
    for expected_sequence, audit_event in enumerate(audit_events):
        if (
            audit_event.sequence != expected_sequence
            or audit_event.previous_event_hash != previous_hash
            or audit_event.event_hash != compute_event_hash(audit_event)
        ):
            raise SessionRecoveryError("existing audit prefix is invalid")
        previous_hash = audit_event.event_hash
    for expected_sequence, event in enumerate(events):
        if event.sequence != expected_sequence:
            raise SessionRecoveryError("existing session event prefix is invalid")
    for audit_event, event in zip(audit_events, events, strict=False):
        _verify_pending_pair(event, audit_event)


def _verify_pending_pair(event: SessionEvent, audit_event: AuditEvent) -> None:
    if audit_event.event_hash != compute_event_hash(audit_event):
        raise SessionRecoveryError("pending audit event hash does not match")
    if (
        audit_event.sequence != event.sequence
        or audit_event.session_id != event.session_id
        or audit_event.event_type != str(event.kind)
        or audit_event.occurred_at != event.occurred_at
        or audit_event.previous_event_hash != event.previous_event_hash
        or audit_event.payload.get("session_event_hash") != compute_session_event_hash(event)
    ):
        raise SessionRecoveryError("pending event and audit record do not correspond")


def _verify_event_correspondence(
    audit_events: tuple[AuditEvent, ...],
    events: tuple[SessionEvent, ...],
) -> None:
    """Reject tampered, truncated, or partially persisted replay streams."""
    if len(audit_events) != len(events):
        raise AuditIntegrityError("session event and audit logs have different lengths")
    for expected_sequence, (audit_event, event) in enumerate(
        zip(audit_events, events, strict=True)
    ):
        if event.sequence != expected_sequence:
            raise AuditIntegrityError(f"session event sequence gap at {event.event_id}")
        if (
            audit_event.sequence != event.sequence
            or audit_event.session_id != event.session_id
            or audit_event.event_type != str(event.kind)
            or audit_event.occurred_at != event.occurred_at
            or audit_event.previous_event_hash != event.previous_event_hash
        ):
            raise AuditIntegrityError(
                f"session event does not match audit record at {event.event_id}"
            )
        recorded_hash = audit_event.payload.get("session_event_hash")
        if recorded_hash != compute_session_event_hash(event):
            raise AuditIntegrityError(f"session event hash mismatch at {event.event_id}")


def _validate_command_record(payload: dict[str, Any], command_id: str) -> None:
    if (
        payload.get("schema_version") != SESSION_COMMAND_RECEIPT_VERSION
        or payload.get("command_id") != command_id
        or not isinstance(payload.get("session_id"), str)
        or not isinstance(payload.get("command_hash"), str)
        or payload.get("state") not in {"accepted", "completed"}
        or not isinstance(payload.get("first_sequence"), int)
        or payload["first_sequence"] < 0
    ):
        raise SessionRecoveryError(f"invalid command receipt for {command_id}")
    last_sequence = payload.get("last_sequence")
    if payload["state"] == "completed" and (
        not isinstance(last_sequence, int) or last_sequence < payload["first_sequence"]
    ):
        raise SessionRecoveryError(f"completed command receipt is incomplete for {command_id}")
    if payload["state"] == "accepted" and last_sequence is not None:
        raise SessionRecoveryError(f"accepted command receipt is inconsistent for {command_id}")


def _writer_owner_summary(path: Path) -> str:
    try:
        payload = _writer_record(_read_private_json(path))
    except (OSError, ValueError):
        return ""
    pid = payload.get("pid")
    host = payload.get("host")
    if isinstance(pid, int) and isinstance(host, str):
        return f" (pid {pid} on {host})"
    return ""


def _command_record(payload: dict[str, Any]) -> dict[str, Any]:
    schema_version = payload.get("schema_version")
    if not isinstance(schema_version, str) or not schema_version:
        raise SessionRecoveryError("session command receipt is invalid")
    try:
        return PERSISTENCE_MIGRATIONS.migrate(
            SESSION_COMMAND_RECEIPT_KIND,
            payload,
        ).payload
    except MigrationError as error:
        raise SessionRecoveryError("session command receipt schema is unsupported") from error


def _writer_record(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return PERSISTENCE_MIGRATIONS.migrate(SESSION_WRITER_KIND, payload).payload
    except MigrationError as error:
        raise SessionRecoveryError("session writer metadata schema is unsupported") from error


def _session_event_json(content: str | bytes) -> SessionEvent:
    try:
        return _session_event(json.loads(content))
    except json.JSONDecodeError as error:
        raise SessionRecoveryError("session event record is malformed") from error


def _session_event(payload: object) -> SessionEvent:
    if not isinstance(payload, dict):
        raise SessionRecoveryError("session event record must be an object")
    try:
        migrated = PERSISTENCE_MIGRATIONS.migrate(SESSION_EVENT_KIND, payload)
        return SessionEvent.model_validate(migrated.payload)
    except (MigrationError, ValueError) as error:
        raise SessionRecoveryError("session event schema is unsupported") from error


def _audit_event_json(content: str | bytes) -> AuditEvent:
    try:
        return _audit_event(json.loads(content))
    except json.JSONDecodeError as error:
        raise SessionRecoveryError("audit event record is malformed") from error


def _audit_event(payload: object) -> AuditEvent:
    if not isinstance(payload, dict):
        raise SessionRecoveryError("audit event record must be an object")
    try:
        migrated = PERSISTENCE_MIGRATIONS.migrate(AUDIT_EVENT_KIND, payload)
        return AuditEvent.model_validate(migrated.payload)
    except (MigrationError, ValueError) as error:
        raise SessionRecoveryError("audit event schema is unsupported") from error


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
