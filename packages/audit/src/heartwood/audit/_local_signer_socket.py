# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Owner-authenticated Unix sockets for the explicit local signer fallback."""

from __future__ import annotations

import os
import socket
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import unquote, urlsplit


def local_checkpoint_socket_path(endpoint: str) -> Path:
    """Require a canonical absolute Unix socket URL, without alternate routing."""
    parsed = urlsplit(endpoint)
    path = Path(unquote(parsed.path, errors="strict"))
    if (
        parsed.scheme != "unix"
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or not path.is_absolute()
        or path == Path("/")
        or ".." in path.parts
        or any(ord(character) < 0x20 for character in str(path))
        or path.as_uri().replace("file:", "unix:", 1) != endpoint
    ):
        raise ValueError("local signer requires a canonical absolute unix:/// socket endpoint")
    return path


def _private_parent(path: Path) -> None:
    if not hasattr(socket, "AF_UNIX") or not hasattr(os, "geteuid"):
        raise ValueError("local signer requires Unix socket ownership controls")
    uid = os.geteuid()
    for directory in (path.parent, *path.parent.parents):
        metadata = directory.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid not in {0, uid}:
            raise ValueError("local signer socket ancestors must be trusted real directories")
        if metadata.st_mode & 0o022 and not (
            metadata.st_uid == 0 and metadata.st_mode & stat.S_ISVTX
        ):
            raise ValueError("local signer socket ancestors must not be writable by other users")
        if directory == path.parent and (metadata.st_uid != uid or metadata.st_mode & 0o077):
            raise ValueError("local signer socket directory must be owner-only")


def _socket_identity(path: Path) -> tuple[int, int]:
    _private_parent(path)
    metadata = path.lstat()
    if (
        not stat.S_ISSOCK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o077
    ):
        raise ValueError("local signer socket must be owner-only and owned by the current user")
    return metadata.st_dev, metadata.st_ino


def connect_local_checkpoint_socket(endpoint: str, timeout: float) -> socket.socket:
    """Validate the listener's protected path before transmitting any request bytes."""
    path = local_checkpoint_socket_path(endpoint)
    expected = _socket_identity(path)
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.settimeout(timeout)
        connection.connect(str(path))
        if _socket_identity(path) != expected:
            raise ValueError("local signer socket changed during connection")
        return connection
    except BaseException:
        connection.close()
        raise


@contextmanager
def local_checkpoint_signer_socket(endpoint: str) -> Iterator[socket.socket]:
    """Bind a private socket without replacing a live or stale listener path."""
    path = local_checkpoint_socket_path(endpoint)
    _private_parent(path)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(path))
        path.chmod(0o600)
        identity = _socket_identity(path)
        try:
            listener.listen()
            yield listener
        finally:
            try:
                if _socket_identity(path) == identity:
                    path.unlink()
            except FileNotFoundError:
                pass
