# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

import subprocess
import sys
from pathlib import Path

import pytest

from heartwood.core_adapter import (
    FileSessionStore,
    SessionOwnershipError,
    SessionService,
    SessionStoreBoundaryError,
    session_maintenance,
)
from heartwood.persistence import native_file_lock
from heartwood.session import CommandKind, SessionCommand


def test_maintenance_gate_rejects_new_session_before_creation(tmp_path: Path) -> None:
    store = FileSessionStore(tmp_path, "session-new")
    try:
        with native_file_lock(tmp_path / ".writers.lock"):
            with pytest.raises(SessionOwnershipError, match="maintenance"):
                store.acquire_writer()
            assert not store.session_dir.exists()
    finally:
        store.release_writer()


def test_maintenance_preserves_records_and_rejects_new_writers(tmp_path: Path) -> None:
    service = SessionService.synthetic_default(tmp_path)
    service.handle(
        SessionCommand(
            command_id="pause-before-maintenance",
            session_id="session-synthetic-001",
            kind=CommandKind.PAUSE,
            actor_id="human",
            created_at="2026-01-01T00:00:00Z",
        )
    )
    service.close()
    store = service.store
    store.writer_metadata_path.write_text('{"legacy":"retain unchanged"}\n')
    paths = (store.events_path, store.audit_path, store.writer_metadata_path)
    before = {path: path.read_bytes() for path in paths}

    with session_maintenance(tmp_path) as stores:
        assert [entry.session_id for entry in stores] == [store.session_id]
        assert stores[0].replay_events() == service.replay_events()
        assert not stores[0].owns_writer
        with pytest.raises(SessionOwnershipError, match="maintenance"):
            FileSessionStore(tmp_path, "session-new").acquire_writer()
        assert not (tmp_path / "session-new").exists()
        assert {path: path.read_bytes() for path in paths} == before

    assert {path: path.read_bytes() for path in paths} == before


def test_maintenance_prevents_snapshot_creating_a_missing_session(tmp_path: Path) -> None:
    store = FileSessionStore(tmp_path, "session-new")
    with session_maintenance(tmp_path):
        assert store.replay_events() == ()
        with pytest.raises(SessionOwnershipError, match="maintenance"), store.snapshot():
            pytest.fail("snapshot must not create a session during maintenance")
        assert not store.session_dir.exists()


def test_active_writer_prevents_maintenance_and_releases_partial_locks(tmp_path: Path) -> None:
    first = FileSessionStore(tmp_path, "session-a")
    active = FileSessionStore(tmp_path, "session-z")
    first.acquire_writer()
    first.release_writer()
    active.acquire_writer()
    try:
        with (
            pytest.raises(SessionOwnershipError, match="writers to stop"),
            session_maintenance(tmp_path),
        ):
            pytest.fail("maintenance must not begin while a writer is active")
        first.acquire_writer()
        assert first.owns_writer
        assert active.owns_writer
    finally:
        first.release_writer()
        active.release_writer()


def test_maintenance_releases_locks_after_copy_failure(tmp_path: Path) -> None:
    store = FileSessionStore(tmp_path, "session-existing")
    store.acquire_writer()
    store.release_writer()
    with pytest.raises(OSError, match="copy failed"), session_maintenance(tmp_path):
        raise OSError("copy failed")
    try:
        store.acquire_writer()
        assert store.owns_writer
    finally:
        store.release_writer()


def test_maintenance_rejects_symlinked_session_without_touching_target(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    outside = tmp_path / "outside"
    sessions.mkdir()
    outside.mkdir()
    (sessions / "session-linked").symlink_to(outside, target_is_directory=True)
    with (
        pytest.raises(SessionStoreBoundaryError, match="symbolic links"),
        session_maintenance(sessions),
    ):
        pytest.fail("symlinked session must prevent maintenance")
    assert list(outside.iterdir()) == []


def test_maintenance_excludes_another_process(tmp_path: Path) -> None:
    script = """
import sys
from pathlib import Path
from heartwood.core_adapter import FileSessionStore, SessionOwnershipError
store = FileSessionStore(Path(sys.argv[1]), "session-child")
try:
    store.acquire_writer()
except SessionOwnershipError:
    print("blocked")
else:
    store.release_writer()
    raise SystemExit("writer was admitted during maintenance")
"""
    with session_maintenance(tmp_path):
        result = subprocess.run(
            [sys.executable, "-c", script, str(tmp_path)],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        assert result.stdout.strip() == "blocked"
        assert not (tmp_path / "session-child").exists()


def test_process_exit_releases_maintenance(tmp_path: Path) -> None:
    script = """
import sys
from pathlib import Path
from heartwood.core_adapter import session_maintenance
with session_maintenance(Path(sys.argv[1])):
    print("ready", flush=True)
    sys.stdin.read()
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "ready"
        process.terminate()
        process.wait(timeout=5)
        with session_maintenance(tmp_path) as stores:
            assert stores == ()
    finally:
        if process.poll() is None:
            process.terminate()
        process.communicate(timeout=5)
