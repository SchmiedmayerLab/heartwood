# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Content-minimized, versioned persistence for OpenHands conversation state."""

from __future__ import annotations

import json
import os
import re
import stat
from importlib.metadata import version
from pathlib import Path
from threading import RLock

from openhands.sdk import LocalFileStore, TextContent
from openhands.sdk.event import Event, ObservationEvent
from openhands.sdk.event.conversation_error import ConversationErrorEvent
from openhands.sdk.event.error_classification import FailureKind
from openhands.tools.task import TaskObservation

from heartwood.persistence import (
    OPENHANDS_STATE_KIND,
    OPENHANDS_STATE_VERSION,
    PERSISTENCE_MIGRATIONS,
    DurableFileError,
    MigrationError,
    MigrationResult,
    native_file_lock,
    read_private_json,
    read_private_text,
    write_private_bytes_atomic,
    write_private_json_atomic,
    write_private_text_atomic,
)

_CONTENT_POLICY = "heartwood.openhands-content-minimized.v1"
_MARKER_NAME = ".heartwood-persistence.json"
_MIGRATION_LOCK_SUFFIX = ".heartwood-migration.lock"
_EVENT_FILE = re.compile(r"^event-(?P<index>[0-9]{5,})-[0-9A-Fa-f-]{8,}\.json$")

_SAFE_ERROR_DETAILS = {
    FailureKind.AUTH: "Model provider authentication failed.",
    FailureKind.QUOTA: "The model provider reported an exhausted quota or budget.",
    FailureKind.RATE_LIMIT: "The model provider temporarily limited requests.",
    FailureKind.CONFIG: "The model connection is not configured correctly.",
    FailureKind.TRANSIENT: "The model provider is temporarily unavailable.",
    FailureKind.AGENT_ACTION: "The agent could not complete the requested action.",
    FailureKind.INTERNAL: "The agent runtime stopped unexpectedly.",
    FailureKind.UNKNOWN: "The agent conversation stopped unexpectedly.",
}


class OpenHandsPersistenceError(ValueError):
    """Raised when persisted OpenHands state is unsafe or incompatible."""


class ContentMinimizedLocalFileStore(LocalFileStore):
    """Persist typed OpenHands state atomically while minimizing provider failures."""

    def __init__(
        self,
        root: str,
        cache_limit_size: int = 500,
        cache_memory_size: int = 20 * 1024 * 1024,
    ) -> None:
        super().__init__(root, cache_limit_size, cache_memory_size)
        self._heartwood_write_lock = RLock()
        persistence_root = Path(self.root)
        migration_lock = persistence_root.with_name(
            f".{persistence_root.name}{_MIGRATION_LOCK_SUFFIX}"
        )
        with native_file_lock(migration_lock, secure_parent=False):
            self._ensure_compatible_state()

    def write(self, path: str, contents: str | bytes) -> None:
        """Atomically write one OpenHands record and keep the upstream cache coherent."""
        if isinstance(contents, str) and path.startswith("events/event-"):
            try:
                contents = _minimize_event(contents)
            except ValueError as error:
                raise OpenHandsPersistenceError("OpenHands event state is malformed") from error
        full_path = Path(self.get_full_path(path))
        with self._heartwood_write_lock:
            if isinstance(contents, str):
                write_private_text_atomic(full_path, contents)
                self.cache[str(full_path)] = contents
            else:
                write_private_bytes_atomic(full_path, contents)
                self.cache.pop(str(full_path), None)

    def _ensure_compatible_state(self) -> None:
        root = Path(self.root)
        if root.is_symlink() or not root.is_dir():
            raise OpenHandsPersistenceError("OpenHands persistence must be a regular directory")
        root.chmod(0o700)
        marker_path = root / _MARKER_NAME
        sdk_version = version("openhands-sdk")
        if marker_path.exists():
            try:
                result = _migrated_marker(read_private_json(marker_path))
            except (DurableFileError, OSError) as error:
                raise OpenHandsPersistenceError(
                    "OpenHands persistence marker is unavailable"
                ) from error
            marker = result.payload
            _validate_marker(marker, sdk_version=sdk_version)
            if result.applied_versions:
                _validate_and_minimize_existing_state(root, marker_path=marker_path)
                write_private_json_atomic(marker_path, marker)
        else:
            had_state = any(root.iterdir())
            _validate_and_minimize_existing_state(root)
            marker = (
                _migrated_marker(
                    {
                        "schema_version": "heartwood.openhands-state.unversioned",
                        "openhands_sdk_version": sdk_version,
                        "content_policy": _CONTENT_POLICY,
                    }
                ).payload
                if had_state
                else _fresh_marker()
            )
            write_private_json_atomic(marker_path, marker)
            _validate_marker(marker, sdk_version=sdk_version)


def _migrated_marker(payload: dict[str, object]) -> MigrationResult:
    try:
        result = PERSISTENCE_MIGRATIONS.migrate(OPENHANDS_STATE_KIND, payload)
    except MigrationError as error:
        raise OpenHandsPersistenceError("OpenHands persistence schema is unsupported") from error
    return result


def _validate_marker(payload: dict[str, object], *, sdk_version: str) -> None:
    if set(payload) != {
        "adopted_from",
        "content_policy",
        "openhands_sdk_version",
        "schema_version",
    }:
        raise OpenHandsPersistenceError("OpenHands persistence marker is malformed")
    if payload.get("schema_version") != OPENHANDS_STATE_VERSION:
        raise OpenHandsPersistenceError("OpenHands persistence schema is unsupported")
    if payload.get("content_policy") != _CONTENT_POLICY:
        raise OpenHandsPersistenceError("OpenHands persistence content policy is unsupported")
    if payload.get("openhands_sdk_version") != sdk_version:
        raise OpenHandsPersistenceError(
            "OpenHands persisted state requires an explicit SDK migration"
        )
    if payload.get("adopted_from") not in {"new", "unversioned"}:
        raise OpenHandsPersistenceError("OpenHands persistence origin is unsupported")


def _validate_and_minimize_existing_state(
    root: Path,
    *,
    marker_path: Path | None = None,
) -> None:
    replacements: list[tuple[Path, str]] = []
    event_indices: list[int] = []
    for path in sorted(root.rglob("*")):
        metadata = _entry_metadata(path)
        if stat.S_ISLNK(metadata.st_mode) or not (
            stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)
        ):
            raise OpenHandsPersistenceError(
                "OpenHands persistence contains a symbolic link or special file"
            )
        if stat.S_ISDIR(metadata.st_mode):
            _set_entry_mode(path, 0o700)
            continue
        relative = path.relative_to(root)
        _set_entry_mode(path, 0o600)
        if marker_path is not None and path == marker_path:
            continue
        if relative == Path("base_state.json"):
            try:
                if not isinstance(json.loads(read_private_text(path)), dict):
                    raise OpenHandsPersistenceError("OpenHands base state must be an object")
            except json.JSONDecodeError as error:
                raise OpenHandsPersistenceError("OpenHands base state is malformed") from error
            continue
        if relative.parent == Path("events") and relative.name != ".eventlog.lock":
            match = _EVENT_FILE.fullmatch(relative.name)
            if match is None:
                raise OpenHandsPersistenceError("OpenHands event filename is unsupported")
            event_indices.append(int(match.group("index")))
            try:
                persisted = read_private_text(path)
                minimized = _minimize_event(persisted)
            except (DurableFileError, ValueError) as error:
                raise OpenHandsPersistenceError("OpenHands event state is malformed") from error
            if minimized != persisted:
                replacements.append((path, minimized))
    if event_indices and sorted(event_indices) != list(range(len(event_indices))):
        raise OpenHandsPersistenceError("OpenHands event sequence contains a gap")
    for path, minimized in replacements:
        write_private_text_atomic(path, minimized)


def _entry_metadata(path: Path) -> os.stat_result:
    try:
        return path.lstat()
    except OSError as error:
        raise OpenHandsPersistenceError("OpenHands persistence entry is unavailable") from error


def _set_entry_mode(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError as error:
        raise OpenHandsPersistenceError("OpenHands persistence entry is unavailable") from error


def _minimize_event(contents: str) -> str:
    event = Event.model_validate_json(contents)
    if isinstance(event, ConversationErrorEvent):
        classification = event.classification
        kind = FailureKind.UNKNOWN if classification is None else classification.kind
        detail = _SAFE_ERROR_DETAILS.get(kind, _SAFE_ERROR_DETAILS[FailureKind.UNKNOWN])
        return event.model_copy(update={"detail": detail}).model_dump_json(exclude_none=True)
    if (
        isinstance(event, ObservationEvent)
        and isinstance(event.observation, TaskObservation)
        and event.observation.is_error
    ):
        observation = event.observation.model_copy(
            update={"content": [TextContent(text=_SAFE_ERROR_DETAILS[FailureKind.AGENT_ACTION])]}
        )
        return event.model_copy(
            update={"observation": observation, "extended_content": []}
        ).model_dump_json(exclude_none=True)
    return contents


def _fresh_marker() -> dict[str, object]:
    return {
        "schema_version": OPENHANDS_STATE_VERSION,
        "openhands_sdk_version": version("openhands-sdk"),
        "content_policy": _CONTENT_POLICY,
        "adopted_from": "new",
    }


__all__ = ["ContentMinimizedLocalFileStore", "OpenHandsPersistenceError"]
