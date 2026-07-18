# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Hash-chained audit logging for Heartwood sessions."""

from __future__ import annotations

from heartwood.audit._log import AuditIntegrityError, AuditLog, compute_event_hash, scrub_json_value

__all__ = [
    "AuditIntegrityError",
    "AuditLog",
    "__version__",
    "compute_event_hash",
    "scrub_json_value",
]

__version__ = "0.2.0-beta.3"
