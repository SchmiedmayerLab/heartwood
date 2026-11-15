# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Migration staging preserves historical records without enabling a second writer."""

from __future__ import annotations

import errno
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from heartwood.audit import AuditIntegrityError
from heartwood.core_adapter import FileSessionStore, SessionOwnershipError, SessionService
from heartwood.gateway import ProjectContext, ProjectStateError
from heartwood.gateway import _session_migration as migration
from heartwood.gateway._session_catalog import SessionCatalog
from heartwood.persistence import (
    NativeLockUnavailableError,
    native_file_lock,
    write_private_bytes_atomic,
)
from heartwood.session import CommandKind, SessionCommand


def _projects(tmp_path: Path) -> tuple[ProjectContext, ProjectContext]:
    workspace = tmp_path / "research"
    workspace.mkdir()
    parent = tmp_path / "protected"
    parent.mkdir(mode=0o700)
    source = ProjectContext(workspace)
    source.initialize()
    identity = uuid4()
    target = ProjectContext(workspace, protected_state=parent / str(identity), project_id=identity)
    return source, target


def _history(source: ProjectContext) -> FileSessionStore:
    service = SessionService.synthetic_default(source.sessions_dir)
    try:
        service.handle(
            SessionCommand(
                command_id="synthetic-pause",
                session_id="session-synthetic-001",
                kind=CommandKind.PAUSE,
                actor_id="human",
                created_at="2026-01-01T00:00:00Z",
            )
        )
    finally:
        service.close()
    SessionCatalog(source.sessions_dir).ensure(service.store.session_id, title="Synthetic history")
    return service.store


def _records(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and not path.name.endswith(".lock")
    }


def test_staging_preserves_history_bytes_and_excludes_runtime_state(tmp_path: Path) -> None:
    source, target = _projects(tmp_path)
    store = _history(source)
    runtime = store.session_dir / "openhands"
    runtime.mkdir()
    (runtime / "state.json").write_text('{"untrusted":"recovery"}\n')
    store.writer_metadata_path.write_text('{"legacy":"writer metadata"}\n')
    store.audit_export_path.write_text("legacy derived export\n")
    (source.models_dir / "model.gguf").write_bytes(b"synthetic model: do not copy")
    before = _records(source.state_root)
    head = store.verified_head()

    with migration.staged_session_records(source, target) as stage:
        assert stage.parent == target.state_root.parent
        assert not target.state_root.exists()
        assert not (stage / "state.json").exists()
        copied = FileSessionStore(stage / "sessions", store.session_id)
        assert copied.replay_events() == store.replay_events()
        assert copied.verified_head() == head
        receipt = json.loads((stage / "receipt.json").read_text())
        assert receipt["project_id"] == str(target.project_id)
        assert receipt["target_state_root"] == str(target.state_root)
        assert receipt["assurance"] == "legacy-history-only"
        assert receipt["sessions"] == {store.session_id: list(head)}
        assert not (copied.session_dir / "openhands").exists()
        assert not copied.writer_metadata_path.exists()
        assert not copied.audit_export_path.exists()
        assert not (stage / "models").exists()
        for relative, expected in receipt["files"].items():
            content = (source.sessions_dir / relative).read_bytes()
            assert (stage / "sessions" / relative).read_bytes() == content
            assert expected == {
                "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        for path in (stage, *stage.rglob("*")):
            assert path.stat().st_mode & 0o777 == (0o700 if path.is_dir() else 0o600)
        with (
            pytest.raises(NativeLockUnavailableError),
            native_file_lock(source.state_lock_path, timeout=0, reentrant=False),
        ):
            pytest.fail("state initialization must remain excluded")

    assert not stage.exists()
    assert _records(source.state_root) == before
    assert source.state_exists()
    assert not target.state_root.exists()


def test_staging_holds_session_admission_until_consumer_finishes(tmp_path: Path) -> None:
    source, target = _projects(tmp_path)
    store = _history(source)
    newcomer = FileSessionStore(source.sessions_dir, "session-new")
    with migration.staged_session_records(source, target):
        with pytest.raises(SessionOwnershipError):
            newcomer.acquire_writer()
        with pytest.raises(SessionOwnershipError):
            store.acquire_writer()
        assert not newcomer.session_dir.exists()
    try:
        newcomer.acquire_writer()
        assert newcomer.owns_writer
    finally:
        newcomer.release_writer()


@pytest.mark.parametrize(
    "damage", ["audit", "events", "pending", "unresolved", "duplicate", "unknown"]
)
def test_invalid_or_unsettled_history_prevents_staging(tmp_path: Path, damage: str) -> None:
    source, target = _projects(tmp_path)
    store = _history(source)
    if damage == "audit":
        store.audit_path.write_text(store.audit_path.read_text().replace('"human"', '"other"'))
    elif damage == "events":
        store.events_path.write_text(store.events_path.read_text().replace('"human"', '"other"'))
    elif damage == "pending":
        store.pending_commit_path.write_text("{}\n")
    elif damage == "unresolved":
        store.acquire_writer()
        try:
            store.accept_command(command_id="unfinished", command_hash="a" * 64, first_sequence=1)
        finally:
            store.release_writer()
    elif damage == "duplicate":
        original = next(store.commands_dir.glob("*.json"))
        (store.commands_dir / "duplicate.json").write_bytes(original.read_bytes())
    else:
        (store.session_dir / "unrecognized.json").write_text("{}\n")
    before = _records(source.state_root)
    with (
        pytest.raises((ProjectStateError, AuditIntegrityError)),
        migration.staged_session_records(source, target),
    ):
        pytest.fail("invalid source must not yield a staged copy")
    assert _records(source.state_root) == before
    assert list(target.state_root.parent.iterdir()) == []


@pytest.mark.parametrize("failure", ["disk-full", "corrupt-copy", "consumer"])
def test_staging_failure_preserves_original_and_releases_locks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    source, target = _projects(tmp_path)
    store = _history(source)
    before = _records(source.state_root)
    original = write_private_bytes_atomic

    def fail_copy(path: Path, content: bytes) -> None:
        if failure == "disk-full":
            raise OSError(errno.ENOSPC, "synthetic disk full")
        original(path, content + b"corruption" if failure == "corrupt-copy" else content)

    monkeypatch.setattr(migration, "write_private_bytes_atomic", fail_copy)
    with (
        pytest.raises((OSError, ProjectStateError)),
        migration.staged_session_records(source, target),
    ):
        raise OSError("synthetic consumer interruption")
    assert _records(source.state_root) == before
    assert list(target.state_root.parent.iterdir()) == []
    try:
        store.acquire_writer()
        assert store.owns_writer
    finally:
        store.release_writer()


def test_active_session_prevents_staging(tmp_path: Path) -> None:
    source, target = _projects(tmp_path)
    store = _history(source)
    store.acquire_writer()
    try:
        with pytest.raises(SessionOwnershipError), migration.staged_session_records(source, target):
            pytest.fail("active writer must prevent staging")
    finally:
        store.release_writer()
    assert list(target.state_root.parent.iterdir()) == []


@pytest.mark.parametrize("location", ["event", "commands", "runtime", "parent"])
def test_staging_rejects_symbolic_links(tmp_path: Path, location: str) -> None:
    source, target = _projects(tmp_path)
    store = _history(source)
    outside = tmp_path / "outside"
    outside.mkdir()
    if location == "event":
        store.events_path.rename(outside / "events.jsonl")
        store.events_path.symlink_to(outside / "events.jsonl")
    elif location == "commands":
        store.commands_dir.rename(outside / "commands")
        store.commands_dir.symlink_to(outside / "commands", target_is_directory=True)
    elif location == "runtime":
        (store.session_dir / "openhands").symlink_to(outside, target_is_directory=True)
    else:
        target.state_root.parent.rename(outside / "protected")
        target.state_root.parent.symlink_to(outside / "protected", target_is_directory=True)
    with pytest.raises(ProjectStateError), migration.staged_session_records(source, target):
        pytest.fail("symlinks must not be followed")


def test_staging_refuses_existing_target(tmp_path: Path) -> None:
    source, target = _projects(tmp_path)
    target.state_root.mkdir()
    sentinel = target.state_root / "keep"
    sentinel.write_bytes(b"existing state")
    with pytest.raises(ProjectStateError), migration.staged_session_records(source, target):
        pytest.fail("an existing target must not be overwritten")
    assert sentinel.read_bytes() == b"existing state"


def test_copied_workspace_cannot_use_another_projects_staging_target(tmp_path: Path) -> None:
    source, target = _projects(tmp_path)
    copied = tmp_path / "copied"
    copied.mkdir()
    other = ProjectContext(copied)
    other.initialize()
    with pytest.raises(ProjectStateError), migration.staged_session_records(other, target):
        pytest.fail("target binding must match the exact source workspace")
    assert source.state_exists()
    assert list(target.state_root.parent.iterdir()) == []


def test_staging_requires_existing_source_and_trusted_target_parent(tmp_path: Path) -> None:
    source, target = _projects(tmp_path)
    parent = target.state_root.parent
    parent.chmod(0o777)
    try:
        with (
            pytest.raises(ProjectStateError, match="writable parent"),
            migration.staged_session_records(source, target),
        ):
            pytest.fail("a writable destination parent must be rejected")
    finally:
        parent.chmod(0o700)
    empty = tmp_path / "empty"
    empty.mkdir()
    missing = ProjectContext(empty)
    identity = uuid4()
    new_target = ProjectContext(empty, protected_state=parent / str(identity), project_id=identity)
    with (
        pytest.raises(ProjectStateError, match="initialized source"),
        migration.staged_session_records(missing, new_target),
    ):
        pytest.fail("staging must not initialize a new project")
    assert not missing.state_root.exists()


def test_staging_excludes_writer_and_configuration_changes_in_another_process(
    tmp_path: Path,
) -> None:
    source, target = _projects(tmp_path)
    _history(source)
    script = """
import sys
from pathlib import Path
from heartwood.core_adapter import FileSessionStore, SessionOwnershipError
from heartwood.persistence import NativeLockUnavailableError, native_file_lock
root = Path(sys.argv[1])
for path in (root / '.state.lock', root / '.config.lock'):
    try:
        with native_file_lock(path, timeout=0):
            raise AssertionError('project lock was not held')
    except NativeLockUnavailableError:
        pass
try:
    FileSessionStore(root / 'sessions', 'session-new').acquire_writer()
except SessionOwnershipError:
    print('excluded')
else:
    raise AssertionError('new session writer was admitted')
"""
    with migration.staged_session_records(source, target):
        result = subprocess.run(
            [sys.executable, "-c", script, str(source.state_root)],
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
        assert result.stdout.strip() == "excluded"


def test_process_interruption_leaves_only_inert_staging_and_retry_uses_fresh_copy(
    tmp_path: Path,
) -> None:
    source, target = _projects(tmp_path)
    _history(source)
    before = _records(source.state_root)
    script = """
import os
import sys
from pathlib import Path
from uuid import UUID
from heartwood.gateway import ProjectContext
from heartwood.gateway import _session_migration as migration
root, target_path = map(Path, sys.argv[1:])
source = ProjectContext(root)
target = ProjectContext(root, protected_state=target_path, project_id=UUID(target_path.name))
original = migration.write_private_bytes_atomic
def interrupt(path, content):
    original(path, content)
    os._exit(23)
migration.write_private_bytes_atomic = interrupt
with migration.staged_session_records(source, target):
    raise AssertionError('copy should have been interrupted')
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(source.root), str(target.state_root)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 23, result.stderr
    (partial,) = target.state_root.parent.iterdir()
    assert not (partial / "receipt.json").exists()
    assert not (partial / "state.json").exists()
    assert not target.state_root.exists()
    assert _records(source.state_root) == before
    with migration.staged_session_records(source, target) as fresh:
        assert fresh != partial
        assert (fresh / "receipt.json").is_file()
    assert _records(source.state_root) == before
    assert list(target.state_root.parent.iterdir()) == [partial]


def test_staging_empty_project_does_not_initialize_protected_state(tmp_path: Path) -> None:
    source, target = _projects(tmp_path)
    with migration.staged_session_records(source, target) as stage:
        receipt = json.loads((stage / "receipt.json").read_text())
        assert receipt["sessions"] == {}
        assert receipt["files"] == {}
        with pytest.raises(ProjectStateError, match="requires migration"):
            target.initialize()
    assert not target.state_root.exists()
