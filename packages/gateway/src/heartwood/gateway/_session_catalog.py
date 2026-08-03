# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Gateway-owned session discovery and researcher-facing metadata."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from heartwood.audit import AuditIntegrityError
from heartwood.core_adapter import (
    FileSessionStore,
    SessionOwnershipError,
    SessionRecoveryError,
    SessionStoreBoundaryError,
)
from heartwood.persistence import (
    PERSISTENCE_MIGRATIONS,
    SESSION_METADATA_KIND,
    SESSION_METADATA_VERSION,
    DurableFileError,
    MigrationError,
    NativeLockUnavailableError,
    native_file_lock,
    read_private_json,
    write_private_json_atomic,
)
from heartwood.session import EventKind, SessionEvent

_SCHEMA_VERSION = SESSION_METADATA_VERSION
_MAX_TITLE_LENGTH = 120
DEFAULT_SESSION_ID = "session-main"


class SessionCatalogError(ValueError):
    """Raised when persisted session metadata is invalid."""


class SessionNotFoundError(SessionCatalogError):
    """Raised when a valid session identifier has no persisted session."""


@dataclass(frozen=True, slots=True)
class SessionSummary:
    """Researcher-facing summary of one persisted session."""

    session_id: str
    title: str
    status: str
    created_at: str
    updated_at: str
    event_count: int

    def safe_dict(self) -> dict[str, object]:
        """Return the stable JSON response shape."""
        return {
            "session_id": self.session_id,
            "title": self.title,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "event_count": self.event_count,
        }


@dataclass(frozen=True, slots=True)
class _SessionMetadata:
    title: str
    created_at: str
    updated_at: str


class SessionCatalog:
    """Manage session metadata alongside the existing event stores."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()

    def create(self, title: str | None = None) -> SessionSummary:
        """Create an empty session with a collision-resistant identifier."""
        now = _utc_now()
        for _ in range(10):
            session_id = f"session-{now:%Y%m%dT%H%M%SZ}-{secrets.token_hex(4)}"
            store = _session_store(self.workspace, session_id)
            if not store.session_dir.exists():
                return self.ensure(session_id, title=title or "Untitled session")
        msg = "unable to allocate a unique session id"
        raise SessionCatalogError(msg)

    def default(self) -> SessionSummary:
        """Return the shared first session used by every interaction surface."""
        return self.ensure(DEFAULT_SESSION_ID, title="Main session")

    def ensure(self, session_id: str, *, title: str | None = None) -> SessionSummary:
        """Register a session used by any command surface if it is not known yet."""
        store = _session_store(self.workspace, session_id)
        store.session_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        store.session_dir.chmod(0o700)
        events, recovery_required = _catalog_events(store)
        metadata_path = store.session_dir / "metadata.json"
        with native_file_lock(store.session_dir / ".metadata.lock"):
            if metadata_path.exists():
                metadata = _read_metadata(metadata_path)
            else:
                now = _utc_timestamp()
                metadata = _SessionMetadata(
                    title=_validate_title(title or session_id),
                    created_at=events[0].occurred_at if events else now,
                    updated_at=events[-1].occurred_at if events else now,
                )
                _write_metadata(metadata_path, metadata)
        return _summary(
            store,
            metadata,
            events=events,
            recovery_required=recovery_required,
        )

    def get(self, session_id: str) -> SessionSummary:
        """Return one session, registering persisted state if needed."""
        store = _session_store(self.workspace, session_id)
        if not store.session_dir.is_dir() or store.session_dir.is_symlink():
            msg = f"unknown session: {session_id}"
            raise SessionNotFoundError(msg)
        return self.ensure(session_id)

    def list(self) -> tuple[SessionSummary, ...]:
        """List persisted sessions with most recently updated first."""
        if not self.workspace.exists():
            return ()
        summaries: list[SessionSummary] = []
        for candidate in self.workspace.iterdir():
            if not candidate.is_dir() or candidate.is_symlink():
                continue
            try:
                summaries.append(self.ensure(candidate.name))
            except NativeLockUnavailableError:
                summaries.append(_unavailable_summary(candidate))
            except (SessionCatalogError, SessionStoreBoundaryError):
                continue
        return tuple(
            sorted(
                summaries,
                key=lambda summary: (summary.updated_at, summary.session_id),
                reverse=True,
            )
        )

    def rename(self, session_id: str, title: str) -> SessionSummary:
        """Replace the researcher-facing title without changing session identity."""
        store = _session_store(self.workspace, session_id)
        if not store.session_dir.is_dir() or store.session_dir.is_symlink():
            msg = f"unknown session: {session_id}"
            raise SessionNotFoundError(msg)
        self.ensure(session_id)
        metadata_path = store.session_dir / "metadata.json"
        with native_file_lock(store.session_dir / ".metadata.lock"):
            current = _read_metadata(metadata_path)
            metadata = _SessionMetadata(
                title=_validate_title(title),
                created_at=current.created_at,
                updated_at=_utc_timestamp(),
            )
            _write_metadata(metadata_path, metadata)
        events, recovery_required = _catalog_events(store)
        return _summary(
            store,
            metadata,
            events=events,
            recovery_required=recovery_required,
        )


def _summary(
    store: FileSessionStore,
    metadata: _SessionMetadata,
    *,
    events: tuple[SessionEvent, ...],
    recovery_required: bool,
) -> SessionSummary:
    created_at = events[0].occurred_at if events else metadata.created_at
    updated_at = _latest_timestamp(metadata.updated_at, events)
    return SessionSummary(
        session_id=store.session_id,
        title=metadata.title,
        status="recovery-required" if recovery_required else _session_status(events),
        created_at=created_at,
        updated_at=updated_at,
        event_count=len(events),
    )


def _unavailable_summary(session_dir: Path) -> SessionSummary:
    metadata_path = session_dir / "metadata.json"
    try:
        metadata = _read_metadata(metadata_path)
    except SessionCatalogError:
        try:
            timestamp = (
                datetime.fromtimestamp(session_dir.stat().st_mtime, UTC)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z")
            )
        except OSError:
            timestamp = _utc_timestamp()
        metadata = _SessionMetadata(
            title=session_dir.name,
            created_at=timestamp,
            updated_at=timestamp,
        )
    return SessionSummary(
        session_id=session_dir.name,
        title=metadata.title,
        status="recovery-required",
        created_at=metadata.created_at,
        updated_at=metadata.updated_at,
        event_count=0,
    )


def _catalog_events(store: FileSessionStore) -> tuple[tuple[SessionEvent, ...], bool]:
    try:
        return store.replay_events(), False
    except (AuditIntegrityError, SessionOwnershipError, SessionRecoveryError):
        return (), True


def _session_store(workspace: Path, session_id: str) -> FileSessionStore:
    try:
        return FileSessionStore(workspace, session_id)
    except SessionStoreBoundaryError as error:
        raise SessionCatalogError(str(error)) from error


def _session_status(events: tuple[SessionEvent, ...]) -> str:
    if not events:
        return "empty"
    paused = False
    pending: set[str] = set()
    status = "idle"
    for event in events:
        if event.kind == EventKind.SESSION_PAUSED:
            paused = True
        elif event.kind == EventKind.SESSION_RESUMED:
            paused = False
            status = "idle"
        elif event.kind == EventKind.CONFIRMATION_REQUESTED:
            request = event.payload.get("request")
            if isinstance(request, dict):
                tool_call_id = request.get("tool_call_id")
                if isinstance(tool_call_id, str):
                    pending.add(tool_call_id)
        elif event.kind == EventKind.CONFIRMATION_RESOLVED:
            tool_call_id = event.payload.get("tool_call_id")
            if isinstance(tool_call_id, str):
                pending.discard(tool_call_id)
        elif event.kind == EventKind.ERROR_RECORDED:
            status = "error"
        elif event.kind in {
            EventKind.AGENT_MESSAGE_EMITTED,
            EventKind.COMMAND_RECEIVED,
            EventKind.TOOL_EXECUTION_RECORDED,
        }:
            status = "idle"
    if paused:
        return "paused"
    if pending:
        return "waiting"
    return status


def _latest_timestamp(metadata_timestamp: str, events: tuple[SessionEvent, ...]) -> str:
    if not events:
        return metadata_timestamp
    event_timestamp = events[-1].occurred_at
    try:
        metadata_date = datetime.fromisoformat(metadata_timestamp.replace("Z", "+00:00"))
        event_date = datetime.fromisoformat(event_timestamp.replace("Z", "+00:00"))
    except ValueError:
        return event_timestamp
    return metadata_timestamp if metadata_date >= event_date else event_timestamp


def _validate_title(value: str) -> str:
    title = " ".join(value.split())
    if not title:
        msg = "session title must not be empty"
        raise SessionCatalogError(msg)
    if len(title) > _MAX_TITLE_LENGTH:
        msg = f"session title must be at most {_MAX_TITLE_LENGTH} characters"
        raise SessionCatalogError(msg)
    return title


def _read_metadata(path: Path) -> _SessionMetadata:
    try:
        payload = PERSISTENCE_MIGRATIONS.migrate(
            SESSION_METADATA_KIND,
            read_private_json(path),
        ).payload
    except (DurableFileError, MigrationError, OSError) as error:
        msg = f"unable to load session metadata {path}: {error}"
        raise SessionCatalogError(msg) from error
    if payload.get("schema_version") != _SCHEMA_VERSION:
        msg = f"unsupported session metadata in {path}"
        raise SessionCatalogError(msg)
    title = payload.get("title")
    created_at = payload.get("created_at")
    updated_at = payload.get("updated_at")
    if not all(isinstance(value, str) for value in (title, created_at, updated_at)):
        msg = f"invalid session metadata in {path}"
        raise SessionCatalogError(msg)
    return _SessionMetadata(
        title=_validate_title(str(title)),
        created_at=str(created_at),
        updated_at=str(updated_at),
    )


def _write_metadata(path: Path, metadata: _SessionMetadata) -> None:
    payload = {
        "schema_version": _SCHEMA_VERSION,
        "title": metadata.title,
        "created_at": metadata.created_at,
        "updated_at": metadata.updated_at,
    }
    write_private_json_atomic(path, payload)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _utc_timestamp() -> str:
    return _utc_now().isoformat(timespec="seconds").replace("+00:00", "Z")
