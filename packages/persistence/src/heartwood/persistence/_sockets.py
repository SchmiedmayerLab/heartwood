# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Filesystem ownership checks for private local service sockets."""

from __future__ import annotations

import os
import socket
import stat
from pathlib import Path


def require_private_socket_parent(path: Path) -> None:
    """Require trusted ancestors and an owner-only immediate directory."""
    if not hasattr(socket, "AF_UNIX") or not hasattr(os, "geteuid"):
        raise ValueError("private Unix requires Unix socket ownership controls")
    uid = os.geteuid()
    for directory in (path.parent, *path.parent.parents):
        metadata = directory.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid not in {0, uid}:
            raise ValueError("private Unix socket ancestors must be trusted real directories")
        if metadata.st_mode & 0o022 and not (
            metadata.st_uid == 0 and metadata.st_mode & stat.S_ISVTX
        ):
            raise ValueError("private Unix socket ancestors must not be writable by other users")
        if directory == path.parent and (metadata.st_uid != uid or metadata.st_mode & 0o077):
            raise ValueError("private Unix socket directory must be owner-only")


def private_socket_identity(path: Path) -> tuple[int, int]:
    """Return the identity of a current-user, owner-only Unix socket."""
    require_private_socket_parent(path)
    metadata = path.lstat()
    if (
        not stat.S_ISSOCK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o077
    ):
        raise ValueError("private Unix socket must be owner-only and owned by the current user")
    return metadata.st_dev, metadata.st_ino
