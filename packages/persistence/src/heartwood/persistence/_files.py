# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Owner-only durable filesystem primitives."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

from filelock import FileLock
from filelock import Timeout as FileLockTimeout


class DurableFileError(ValueError):
    """Raised when a durable file violates the private regular-file contract."""


class NativeLockUnavailableError(DurableFileError):
    """Raised when storage cannot provide a process-shared native lock."""


class AppendRecoveryError(DurableFileError):
    """Raised when an interrupted append cannot be recovered unambiguously."""


def read_private_bytes(path: Path) -> bytes:
    """Read one regular file without following its final symbolic link."""
    descriptor = _open_regular(path, os.O_RDONLY)
    try:
        content = bytearray()
        while chunk := os.read(descriptor, 64 * 1024):
            content.extend(chunk)
        return bytes(content)
    finally:
        os.close(descriptor)


def read_private_text(path: Path) -> str:
    """Read one UTF-8 regular file without following its final symbolic link."""
    try:
        return read_private_bytes(path).decode("utf-8")
    except UnicodeDecodeError as error:
        raise DurableFileError(f"persisted file is not valid UTF-8: {path}") from error


def read_private_json(path: Path) -> dict[str, Any]:
    """Read one JSON object from a private regular file."""
    try:
        payload = json.loads(read_private_text(path))
    except json.JSONDecodeError as error:
        raise DurableFileError(f"persisted JSON is malformed: {path}") from error
    if not isinstance(payload, dict):
        raise DurableFileError(f"persisted JSON must be an object: {path}")
    return payload


def write_private_bytes_atomic(
    path: Path,
    content: bytes,
    *,
    secure_parent: bool = True,
) -> None:
    """Atomically replace an owner-only file after syncing data and its directory."""
    _prepare_parent(path, secure=secure_parent)
    _reject_non_regular_target(path)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, content, operation="atomic write")
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        temporary_path.replace(path)
        path.chmod(0o600)
        fsync_directory(path.parent)
    except Exception:
        if descriptor >= 0:
            with suppress(OSError):
                os.close(descriptor)
        temporary_path.unlink(missing_ok=True)
        raise


def write_private_text_atomic(
    path: Path,
    content: str,
    *,
    secure_parent: bool = True,
) -> None:
    """Atomically replace an owner-only UTF-8 file."""
    write_private_bytes_atomic(
        path,
        content.encode("utf-8"),
        secure_parent=secure_parent,
    )


def write_private_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    """Atomically replace an owner-only canonical JSON object."""
    content = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
    write_private_text_atomic(path, content)


def append_private_bytes(path: Path, content: bytes) -> None:
    """Durably append bytes to an owner-only regular file."""
    _prepare_parent(path)
    _reject_non_regular_target(path)
    descriptor = _open_regular(
        path,
        os.O_APPEND | os.O_CREAT | os.O_WRONLY,
        mode=0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, content, operation="append")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def truncate_private_file(path: Path, size: int) -> None:
    """Durably truncate a regular file to a verified byte boundary."""
    if size < 0:
        raise DurableFileError("truncate size cannot be negative")
    descriptor = _open_regular(path, os.O_WRONLY)
    try:
        os.ftruncate(descriptor, size)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def unlink_durable(path: Path, *, missing_ok: bool = False) -> None:
    """Remove a non-symlink regular file and sync its containing directory."""
    try:
        path.lstat()
    except FileNotFoundError:
        if missing_ok:
            return
        raise FileNotFoundError(path) from None
    _reject_non_regular_target(path)
    path.unlink()
    fsync_directory(path.parent)


def fsync_directory(path: Path) -> None:
    """Sync directory metadata where the host filesystem supports it."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        # Some network filesystems permit directory opens but not directory fsync.
        pass
    finally:
        os.close(descriptor)


@contextmanager
def native_file_lock(
    path: Path,
    *,
    timeout: float = -1,
    secure_parent: bool = True,
) -> Iterator[None]:
    """Hold a process-shared native advisory lock without existence-lock fallback."""
    _prepare_parent(path, secure=secure_parent)
    _reject_non_regular_target(path)
    lock = FileLock(
        path,
        timeout=timeout,
        mode=0o600,
        fallback_to_soft=False,
        is_singleton=True,
        thread_local=True,
    )
    try:
        lock.acquire()
    except FileLockTimeout as error:
        raise NativeLockUnavailableError(f"timed out acquiring persistence lock: {path}") from error
    except OSError as error:
        raise NativeLockUnavailableError(
            f"storage does not support the required native lock: {path}"
        ) from error
    try:
        path.chmod(0o600)
        yield
    finally:
        lock.release()


class LockedJsonlStore:
    """Serialize recoverable, canonical JSON-object appends through one native lock."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock_path = path.with_name(f".{path.name}.lock")
        self.journal_path = path.with_name(f".{path.name}.pending")

    def append(self, payload: Mapping[str, object]) -> None:
        """Append one JSON object through a durable recovery journal."""
        self.append_derived(lambda _records: payload)

    def append_derived(
        self,
        build: Callable[[tuple[dict[str, Any], ...]], Mapping[str, object]],
    ) -> dict[str, Any]:
        """Build and append one record from a stable snapshot under the same lock."""
        with native_file_lock(self.lock_path):
            self._recover_locked()
            records = self._read_objects_locked()
            payload = dict(build(records))
            line = (
                json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
            ).encode("utf-8")
            offset = _regular_size(self.path)
            prefix_sha256 = _sha256_prefix(self.path, offset)
            write_private_json_atomic(
                self.journal_path,
                {
                    "schema_version": "heartwood.jsonl-append.v1",
                    "offset": offset,
                    "prefix_sha256": prefix_sha256,
                    "line": line.decode("utf-8"),
                    "line_sha256": hashlib.sha256(line).hexdigest(),
                },
            )
            append_private_bytes(self.path, line)
            unlink_durable(self.journal_path)
            return payload

    def recover(self) -> bool:
        """Complete one interrupted append if its journal and file suffix agree."""
        with native_file_lock(self.lock_path):
            return self._recover_locked()

    def read_objects(self) -> tuple[dict[str, Any], ...]:
        """Return a stable recovered snapshot of all JSON objects."""
        with native_file_lock(self.lock_path):
            self._recover_locked()
            return self._read_objects_locked()

    def _read_objects_locked(self) -> tuple[dict[str, Any], ...]:
        try:
            content = read_private_text(self.path)
        except FileNotFoundError:
            return ()
        if content and not content.endswith("\n"):
            raise AppendRecoveryError(f"JSON Lines record is incomplete: {self.path}")
        objects: list[dict[str, Any]] = []
        for line in content.splitlines():
            if not line:
                raise AppendRecoveryError(f"JSON Lines record is empty: {self.path}")
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as error:
                raise AppendRecoveryError(f"JSON Lines record is malformed: {self.path}") from error
            if not isinstance(payload, dict):
                raise AppendRecoveryError(f"JSON Lines record must be an object: {self.path}")
            objects.append(payload)
        return tuple(objects)

    def _recover_locked(self) -> bool:
        if not self.journal_path.exists():
            return False
        try:
            journal = read_private_json(self.journal_path)
        except (OSError, DurableFileError) as error:
            raise AppendRecoveryError(
                f"append recovery journal is malformed: {self.path}"
            ) from error
        line_text = journal.get("line")
        offset = journal.get("offset")
        prefix_digest = journal.get("prefix_sha256")
        digest = journal.get("line_sha256")
        if (
            journal.get("schema_version") != "heartwood.jsonl-append.v1"
            or not isinstance(line_text, str)
            or not line_text.endswith("\n")
            or "\n" in line_text[:-1]
            or not isinstance(offset, int)
            or offset < 0
            or not isinstance(prefix_digest, str)
            or not isinstance(digest, str)
        ):
            raise AppendRecoveryError(f"append recovery journal is invalid: {self.path}")
        line = line_text.encode("utf-8")
        if hashlib.sha256(line).hexdigest() != digest:
            raise AppendRecoveryError(f"append recovery journal digest does not match: {self.path}")
        if _sha256_prefix(self.path, offset) != prefix_digest:
            raise AppendRecoveryError(f"append recovery prefix does not match: {self.path}")
        suffix = _read_suffix(self.path, offset)
        if suffix == line:
            pass
        elif not suffix or line.startswith(suffix):
            if suffix:
                truncate_private_file(self.path, offset)
            append_private_bytes(self.path, line)
        else:
            raise AppendRecoveryError(
                f"append target does not match its recovery journal: {self.path}"
            )
        unlink_durable(self.journal_path)
        return True


def _prepare_parent(path: Path, *, secure: bool = True) -> None:
    if path.parent.is_symlink():
        raise DurableFileError(f"persisted file parent must not be a symbolic link: {path.parent}")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.parent.is_dir():
        raise DurableFileError(f"persisted file parent must be a directory: {path.parent}")
    if secure:
        path.parent.chmod(0o700)


def _reject_non_regular_target(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(metadata.st_mode):
        raise DurableFileError(f"persisted path must be a regular file: {path}")


def _open_regular(path: Path, flags: int, *, mode: int | None = None) -> int:
    _reject_non_regular_target(path)
    open_flags = flags | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, open_flags) if mode is None else os.open(path, open_flags, mode)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise DurableFileError(f"persisted path must be a regular file: {path}")
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _write_all(descriptor: int, content: bytes, *, operation: str) -> None:
    remaining = memoryview(content)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:  # pragma: no cover - operating-system invariant
            raise OSError(f"durable {operation} made no progress")
        remaining = remaining[written:]


def _regular_size(path: Path) -> int:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return 0
    if not stat.S_ISREG(metadata.st_mode):
        raise DurableFileError(f"persisted path must be a regular file: {path}")
    return metadata.st_size


def _read_suffix(path: Path, offset: int) -> bytes:
    try:
        descriptor = _open_regular(path, os.O_RDONLY)
    except FileNotFoundError:
        if offset == 0:
            return b""
        raise AppendRecoveryError(f"append target is shorter than its journal: {path}") from None
    try:
        size = os.fstat(descriptor).st_size
        if size < offset:
            raise AppendRecoveryError(f"append target is shorter than its journal: {path}")
        os.lseek(descriptor, offset, os.SEEK_SET)
        content = bytearray()
        while chunk := os.read(descriptor, 64 * 1024):
            content.extend(chunk)
        return bytes(content)
    finally:
        os.close(descriptor)


def _sha256_prefix(path: Path, size: int) -> str:
    if size == 0 and not path.exists():
        return hashlib.sha256(b"").hexdigest()
    try:
        descriptor = _open_regular(path, os.O_RDONLY)
    except FileNotFoundError:
        raise AppendRecoveryError(f"append target is shorter than its journal: {path}") from None
    digest = hashlib.sha256()
    remaining = size
    try:
        if os.fstat(descriptor).st_size < size:
            raise AppendRecoveryError(f"append target is shorter than its journal: {path}")
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                raise AppendRecoveryError(f"append target is shorter than its journal: {path}")
            digest.update(chunk)
            remaining -= len(chunk)
        return digest.hexdigest()
    finally:
        os.close(descriptor)
