# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Owner-authenticated Unix sockets for the explicit local signer fallback."""

from __future__ import annotations

import socket
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import unquote, urlsplit

from heartwood.persistence import private_socket_identity, require_private_socket_parent


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


def connect_local_checkpoint_socket(endpoint: str, timeout: float) -> socket.socket:
    """Validate the listener's protected path before transmitting any request bytes."""
    path = local_checkpoint_socket_path(endpoint)
    expected = private_socket_identity(path)
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.settimeout(timeout)
        connection.connect(str(path))
        if private_socket_identity(path) != expected:
            raise ValueError("local signer socket changed during connection")
        return connection
    except BaseException:
        connection.close()
        raise


@contextmanager
def local_checkpoint_signer_socket(endpoint: str) -> Iterator[socket.socket]:
    """Bind a private socket without replacing a live or stale listener path."""
    path = local_checkpoint_socket_path(endpoint)
    require_private_socket_parent(path)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(path))
        path.chmod(0o600)
        identity = private_socket_identity(path)
        try:
            listener.listen()
            yield listener
        finally:
            try:
                if private_socket_identity(path) == identity:
                    path.unlink()
            except FileNotFoundError:
                pass
