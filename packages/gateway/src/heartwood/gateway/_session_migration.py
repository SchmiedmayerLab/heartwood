# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Temporary verification of historical session records before migration."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from heartwood.core_adapter import FileSessionStore, session_maintenance
from heartwood.gateway._deployment import _validate_trusted_parents
from heartwood.gateway._project import ProjectContext, ProjectStateError
from heartwood.gateway._project_activity import project_gateway_maintenance
from heartwood.gateway._session_catalog import _read_metadata
from heartwood.persistence import (
    fsync_directory,
    native_file_lock,
    read_private_bytes,
    read_private_json,
    write_private_bytes_atomic,
    write_private_json_atomic,
)

_RECORDS = ("events.jsonl", "audit.jsonl", "metadata.json")
_RETAINED = {
    "openhands",
    ".deterministic-backend.json",
    ".writer.json",
    ".writer.lock",
    ".snapshot.lock",
    ".metadata.lock",
    ".audit.jsonl.lock",
    "audit-export.jsonl",
}


@contextmanager
def staged_session_records(source: ProjectContext, target: ProjectContext) -> Iterator[Path]:
    """Hold source leases and yield a verified temporary copy, never a live project.

    The caller must separately exclude all other project activity before a binding
    switch. This context does not relocate or remove any source record.
    """
    if (
        source.protected_state is not None
        or target.protected_state is None
        or source.root != target.root
        or target.state_root.exists()
        or target.state_root.resolve() != target.state_root
    ):
        raise ProjectStateError("session staging requires a new protected target for this project")
    _validate_trusted_parents(target.state_root.parent)
    if not source.state_exists():
        raise ProjectStateError("session staging requires initialized source state")
    with (
        project_gateway_maintenance(source),
        native_file_lock(source.config_lock_path, timeout=0, reentrant=False),
        session_maintenance(source.sessions_dir) as stores,
    ):
        if not source.state_exists():
            raise ProjectStateError("source state changed before staging acquired its locks")
        if any(
            not entry.is_dir() and entry.name != ".writers.lock"
            for entry in source.sessions_dir.iterdir()
        ):
            raise ProjectStateError("session root contains an unrecognized record; staging stopped")
        # Validate every source before creating any target files.
        heads = {store.session_id: _verify_session(store) for store in stores}
        paths = {store.session_id: _record_paths(store) for store in stores}
        with TemporaryDirectory(
            prefix=f".{target.project_id}.sessions-", dir=target.state_root.parent
        ) as temporary:
            stage = Path(temporary)
            sessions = stage / "sessions"
            sessions.mkdir(mode=0o700)
            files: dict[str, dict[str, object]] = {}
            for store in stores:
                destination = sessions / store.session_id
                destination.mkdir(mode=0o700)
                for path in paths[store.session_id]:
                    relative = path.relative_to(source.sessions_dir)
                    content = read_private_bytes(path)
                    copied = sessions / relative
                    write_private_bytes_atomic(copied, content)
                    if read_private_bytes(copied) != content:
                        raise ProjectStateError("staged session record differs from its source")
                    files[relative.as_posix()] = {
                        "bytes": len(content),
                        "sha256": hashlib.sha256(content).hexdigest(),
                    }
                copied_store = FileSessionStore(sessions, store.session_id)
                if _verify_session(copied_store) != heads[store.session_id]:
                    raise ProjectStateError("staged session history differs from its source")
                fsync_directory(destination)
            fsync_directory(sessions)
            write_private_json_atomic(
                stage / "receipt.json",
                {
                    "schema_version": "heartwood.session-staging.v1",
                    "workspace": str(source.root),
                    "project_id": str(target.project_id),
                    "target_state_root": str(target.state_root),
                    "assurance": "legacy-history-only",
                    "sessions": heads,
                    "files": files,
                },
            )
            fsync_directory(target.state_root.parent)
            yield stage


def _verify_session(store: FileSessionStore) -> tuple[int, str | None]:
    if store.pending_commit_path.exists() or store.pending_commit_path.is_symlink():
        raise ProjectStateError("recover the pending session append before staging")
    _record_paths(store)
    store.replay_events()
    if store.unresolved_command_ids():
        raise ProjectStateError("reconcile unresolved session commands before staging")
    metadata = store.session_dir / "metadata.json"
    if metadata.exists():
        _read_metadata(metadata)
    return store.verified_head()


def _record_paths(store: FileSessionStore) -> tuple[Path, ...]:
    records: list[Path] = []
    for path in sorted(store.session_dir.iterdir()):
        if path.is_symlink():
            raise ProjectStateError("session staging cannot follow symbolic links")
        if path.name in _RECORDS:
            if not path.is_file():
                raise ProjectStateError("session records must be regular files")
            records.append(path)
        elif path.name == ".commands":
            if not path.is_dir():
                raise ProjectStateError("session command receipts must be a directory")
            command_ids: set[str] = set()
            for receipt in sorted(path.iterdir()):
                if receipt.is_symlink() or not receipt.is_file() or receipt.suffix != ".json":
                    raise ProjectStateError("session command receipts must be regular JSON files")
                command_id = read_private_json(receipt).get("command_id")
                if (
                    not isinstance(command_id, str)
                    or command_id in command_ids
                    or store.command_record(command_id) is None
                ):
                    raise ProjectStateError("session command receipt has an invalid filename")
                command_ids.add(command_id)
                records.append(receipt)
        elif path.name not in _RETAINED:
            raise ProjectStateError("session contains an unrecognized record; staging stopped")
    return tuple(records)
