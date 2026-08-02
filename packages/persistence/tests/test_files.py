# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Fault-injection and concurrency tests for durable file primitives."""

from __future__ import annotations

import errno
import json
import multiprocessing
import stat
from pathlib import Path

import pytest
from filelock import FileLock

from heartwood.persistence import (
    AppendRecoveryError,
    DurableFileError,
    LockedJsonlStore,
    NativeLockUnavailableError,
    _files,
    read_private_json,
    read_private_text,
    truncate_private_file,
    unlink_durable,
    write_private_text_atomic,
)


def test_atomic_write_preserves_previous_file_when_data_write_is_interrupted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "state.json"
    write_private_text_atomic(path, "previous\n")
    original_write_all = _files._write_all

    def interrupted_write(descriptor: int, content: bytes, *, operation: str) -> None:
        if operation == "atomic write":
            original_write_all(descriptor, content[:1], operation=operation)
            raise OSError("synthetic interruption")
        original_write_all(descriptor, content, operation=operation)

    monkeypatch.setattr(_files, "_write_all", interrupted_write)

    with pytest.raises(OSError, match="synthetic interruption"):
        write_private_text_atomic(path, "replacement\n")

    assert read_private_text(path) == "previous\n"
    assert not tuple(tmp_path.glob(".state.json-*"))


def test_private_readers_reject_invalid_utf8_and_non_object_json(tmp_path: Path) -> None:
    invalid_text = tmp_path / "invalid-text"
    invalid_text.write_bytes(b"\xff")
    with pytest.raises(DurableFileError, match="valid UTF-8"):
        read_private_text(invalid_text)

    malformed = tmp_path / "malformed.json"
    malformed.write_text("{", encoding="utf-8")
    with pytest.raises(DurableFileError, match="malformed"):
        read_private_json(malformed)

    non_object = tmp_path / "list.json"
    non_object.write_text("[]", encoding="utf-8")
    with pytest.raises(DurableFileError, match="must be an object"):
        read_private_json(non_object)


@pytest.mark.parametrize("boundary", ["journal", "partial-append", "complete-append"])
def test_jsonl_append_recovers_each_interruption_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    store = LockedJsonlStore(tmp_path / "audit.jsonl")
    store.append({"sequence": 0})
    original_append = _files.append_private_bytes
    original_unlink = _files.unlink_durable

    def interrupt_append(_path: Path, _content: bytes) -> None:
        raise OSError("interrupted")

    if boundary == "journal":
        monkeypatch.setattr(_files, "append_private_bytes", interrupt_append)
    elif boundary == "partial-append":

        def partial_append(path: Path, content: bytes) -> None:
            original_append(path, content[: max(1, len(content) // 2)])
            raise OSError("interrupted")

        monkeypatch.setattr(_files, "append_private_bytes", partial_append)
    else:

        def interrupted_unlink(path: Path, *, missing_ok: bool = False) -> None:
            if path == store.journal_path:
                raise OSError("interrupted")
            original_unlink(path, missing_ok=missing_ok)

        monkeypatch.setattr(_files, "unlink_durable", interrupted_unlink)

    with pytest.raises(OSError, match="interrupted"):
        store.append({"sequence": 1})
    monkeypatch.undo()

    assert store.recover() is True
    assert store.read_objects() == ({"sequence": 0}, {"sequence": 1})
    assert not store.journal_path.exists()


def test_jsonl_recovery_rejects_changed_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LockedJsonlStore(tmp_path / "audit.jsonl")
    store.append({"sequence": 0})

    def interrupt_append(_path: Path, _content: bytes) -> None:
        raise OSError("interrupted")

    monkeypatch.setattr(_files, "append_private_bytes", interrupt_append)
    with pytest.raises(OSError, match="interrupted"):
        store.append({"sequence": 1})
    monkeypatch.undo()
    store.path.write_bytes(store.path.read_bytes().replace(b"0", b"9", 1))

    with pytest.raises(AppendRecoveryError, match="prefix does not match"):
        store.recover()


def test_jsonl_read_rejects_unjournaled_partial_record(tmp_path: Path) -> None:
    store = LockedJsonlStore(tmp_path / "audit.jsonl")
    store.path.write_text('{"sequence":0}', encoding="utf-8")

    with pytest.raises(AppendRecoveryError, match="incomplete"):
        store.read_objects()


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ('{"sequence":0}\n\n', "empty"),
        ('{"sequence":}\n', "malformed"),
        ("[0]\n", "must be an object"),
    ],
)
def test_jsonl_read_rejects_invalid_records(
    tmp_path: Path,
    content: str,
    message: str,
) -> None:
    store = LockedJsonlStore(tmp_path / "audit.jsonl")
    store.path.write_text(content, encoding="utf-8")

    with pytest.raises(AppendRecoveryError, match=message):
        store.read_objects()


@pytest.mark.parametrize("failure", ["malformed", "invalid", "digest", "suffix"])
def test_jsonl_recovery_rejects_invalid_journal_or_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    store = LockedJsonlStore(tmp_path / "audit.jsonl")
    store.append({"sequence": 0})

    def interrupt_append(_path: Path, _content: bytes) -> None:
        raise OSError("interrupted")

    monkeypatch.setattr(_files, "append_private_bytes", interrupt_append)
    with pytest.raises(OSError, match="interrupted"):
        store.append({"sequence": 1})
    monkeypatch.undo()

    if failure == "malformed":
        store.journal_path.write_text("{", encoding="utf-8")
    else:
        journal = json.loads(store.journal_path.read_text(encoding="utf-8"))
        if failure == "invalid":
            journal["schema_version"] = "unsupported"
        elif failure == "digest":
            journal["line_sha256"] = "0" * 64
        else:
            store.path.write_text(
                store.path.read_text(encoding="utf-8") + '{"unexpected":true}\n',
                encoding="utf-8",
            )
        store.journal_path.write_text(json.dumps(journal), encoding="utf-8")

    with pytest.raises(AppendRecoveryError):
        store.recover()


def test_jsonl_store_serializes_process_writers(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    try:
        context = multiprocessing.get_context("fork")
    except ValueError:  # pragma: no cover - Heartwood deployment targets provide fork
        pytest.skip("process-shared lock test requires fork")
    processes = [
        context.Process(target=_append_records, args=(path, worker, 20)) for worker in range(4)
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=30)
        assert process.exitcode == 0

    records = LockedJsonlStore(path).read_objects()
    assert len(records) == 80
    assert {(record["worker"], record["index"]) for record in records} == {
        (worker, index) for worker in range(4) for index in range(20)
    }


def test_private_file_primitives_reject_symlinks_and_enforce_modes(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("outside\n", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(target)

    with pytest.raises(DurableFileError, match="regular file"):
        write_private_text_atomic(link, "replacement\n")
    with pytest.raises(DurableFileError, match="regular file"):
        read_private_text(link)

    private = tmp_path / "private" / "state.txt"
    write_private_text_atomic(private, "state\n")
    assert stat.S_IMODE(private.stat().st_mode) == 0o600
    assert stat.S_IMODE(private.parent.stat().st_mode) == 0o700

    with pytest.raises(DurableFileError, match="regular file"):
        unlink_durable(link)
    with pytest.raises(DurableFileError, match="cannot be negative"):
        truncate_private_file(private, -1)


def test_external_atomic_write_does_not_change_parent_permissions(tmp_path: Path) -> None:
    parent = tmp_path / "deployment"
    parent.mkdir(mode=0o755)
    parent.chmod(0o755)

    _files.write_private_text_atomic(
        parent / "audit.jsonl",
        "{}\n",
        secure_parent=False,
    )

    assert stat.S_IMODE(parent.stat().st_mode) == 0o755
    assert stat.S_IMODE((parent / "audit.jsonl").stat().st_mode) == 0o600


@pytest.mark.parametrize("operation", ["open", "fsync"])
def test_directory_sync_ignores_only_unsupported_operations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    def unsupported(*_args: object, **_kwargs: object) -> None:
        raise OSError(errno.ENOTSUP, "synthetic unsupported directory sync")

    monkeypatch.setattr(_files.os, operation, unsupported)

    _files.fsync_directory(tmp_path)


def test_directory_sync_propagates_storage_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failed_sync(_descriptor: int) -> None:
        raise OSError(errno.EIO, "synthetic storage failure")

    monkeypatch.setattr(_files.os, "fsync", failed_sync)

    with pytest.raises(OSError, match="synthetic storage failure"):
        _files.fsync_directory(tmp_path)


def test_external_native_lock_does_not_change_parent_permissions(tmp_path: Path) -> None:
    parent = tmp_path / "deployment"
    parent.mkdir(mode=0o750)
    parent.chmod(0o750)

    with _files.native_file_lock(parent / ".checkpoint.lock", secure_parent=False):
        assert stat.S_IMODE(parent.stat().st_mode) == 0o750

    assert stat.S_IMODE((parent / ".checkpoint.lock").stat().st_mode) == 0o600


def test_native_lock_timeout_is_a_per_acquisition_setting(tmp_path: Path) -> None:
    lock_path = tmp_path / ".state.lock"
    singleton = FileLock(
        lock_path,
        mode=0o600,
        fallback_to_soft=False,
        is_singleton=True,
        thread_local=True,
    )

    with _files.native_file_lock(lock_path, timeout=0):
        assert singleton.is_locked


def test_native_lock_failure_is_explicit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(*_args: object, **_kwargs: object) -> None:
        raise OSError("unsupported filesystem")

    monkeypatch.setattr(FileLock, "acquire", unavailable)

    with pytest.raises(NativeLockUnavailableError, match="required native lock"):
        LockedJsonlStore(tmp_path / "records.jsonl").read_objects()


def test_append_recovery_journal_contains_no_uncommitted_duplicate_after_recovery(
    tmp_path: Path,
) -> None:
    store = LockedJsonlStore(tmp_path / "records.jsonl")
    store.append({"value": "first"})
    store.append({"value": "second"})

    lines = store.path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line) for line in lines] == [
        {"value": "first"},
        {"value": "second"},
    ]
    assert not store.journal_path.exists()


def test_jsonl_append_reuses_the_parsed_snapshot_for_its_prefix_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LockedJsonlStore(tmp_path / "records.jsonl")
    store.append({"value": "first"})

    def unexpected_prefix_read(_path: Path, _size: int) -> str:
        pytest.fail("normal append must not re-read the verified prefix")

    monkeypatch.setattr(_files, "_sha256_prefix", unexpected_prefix_read)

    store.append({"value": "second"})

    assert store.read_objects() == ({"value": "first"}, {"value": "second"})


def _append_records(path: Path, worker: int, count: int) -> None:
    store = LockedJsonlStore(path)
    for index in range(count):
        store.append({"worker": worker, "index": index})
