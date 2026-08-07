# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Durable file and schema-migration primitives for Heartwood state."""

from __future__ import annotations

from heartwood.persistence._files import (
    AppendRecoveryError,
    DurableFileError,
    LockedJsonlStore,
    NativeLockUnavailableError,
    append_private_bytes,
    fsync_directory,
    native_file_lock,
    read_private_bytes,
    read_private_json,
    read_private_text,
    truncate_private_file,
    unlink_durable,
    write_private_bytes_atomic,
    write_private_json_atomic,
    write_private_text_atomic,
)
from heartwood.persistence._migrations import (
    AUDIT_EVENT_KIND,
    AUDIT_EVENT_VERSION,
    OPENHANDS_STATE_KIND,
    OPENHANDS_STATE_VERSION,
    PERSISTENCE_MIGRATIONS,
    PROJECT_CONFIG_KIND,
    PROJECT_CONFIG_VERSION,
    PROJECT_STATE_FORMATS,
    PROJECT_STATE_KIND,
    PROJECT_STATE_VERSION,
    SESSION_COMMAND_RECEIPT_KIND,
    SESSION_COMMAND_RECEIPT_VERSION,
    SESSION_COMMIT_KIND,
    SESSION_COMMIT_VERSION,
    SESSION_EVENT_KIND,
    SESSION_EVENT_VERSION,
    SESSION_METADATA_KIND,
    SESSION_METADATA_VERSION,
    SESSION_WRITER_KIND,
    SESSION_WRITER_VERSION,
    SKILL_INSTALLATIONS_VERSION,
    MigrationError,
    MigrationRegistry,
    MigrationResult,
)

__all__ = [
    "AUDIT_EVENT_KIND",
    "AUDIT_EVENT_VERSION",
    "OPENHANDS_STATE_KIND",
    "OPENHANDS_STATE_VERSION",
    "PERSISTENCE_MIGRATIONS",
    "PROJECT_CONFIG_KIND",
    "PROJECT_CONFIG_VERSION",
    "PROJECT_STATE_FORMATS",
    "PROJECT_STATE_KIND",
    "PROJECT_STATE_VERSION",
    "SESSION_COMMAND_RECEIPT_KIND",
    "SESSION_COMMAND_RECEIPT_VERSION",
    "SESSION_COMMIT_KIND",
    "SESSION_COMMIT_VERSION",
    "SESSION_EVENT_KIND",
    "SESSION_EVENT_VERSION",
    "SESSION_METADATA_KIND",
    "SESSION_METADATA_VERSION",
    "SESSION_WRITER_KIND",
    "SESSION_WRITER_VERSION",
    "SKILL_INSTALLATIONS_VERSION",
    "AppendRecoveryError",
    "DurableFileError",
    "LockedJsonlStore",
    "MigrationError",
    "MigrationRegistry",
    "MigrationResult",
    "NativeLockUnavailableError",
    "__version__",
    "append_private_bytes",
    "fsync_directory",
    "native_file_lock",
    "read_private_bytes",
    "read_private_json",
    "read_private_text",
    "truncate_private_file",
    "unlink_durable",
    "write_private_bytes_atomic",
    "write_private_json_atomic",
    "write_private_text_atomic",
]

__version__ = "0.3.0-beta.4"
