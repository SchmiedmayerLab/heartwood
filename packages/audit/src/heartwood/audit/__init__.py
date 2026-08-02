# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Hash-chained audit logging for Heartwood sessions."""

from __future__ import annotations

from heartwood.audit._checkpoint import (
    AUDIT_FILENAME,
    CHECKPOINT_FILENAME,
    AuditCheckpointError,
    AuditCheckpointVerification,
    create_audit_checkpoint,
    verify_audit_checkpoint,
)
from heartwood.audit._log import (
    AuditIntegrityError,
    AuditLog,
    AuditVerification,
    canonical_audit_jsonl,
    compute_event_hash,
    prepare_audit_event,
    scrub_json_value,
    verify_audit_events,
    verify_audit_jsonl,
)

__all__ = [
    "AUDIT_FILENAME",
    "CHECKPOINT_FILENAME",
    "AuditCheckpointError",
    "AuditCheckpointVerification",
    "AuditIntegrityError",
    "AuditLog",
    "AuditVerification",
    "__version__",
    "canonical_audit_jsonl",
    "compute_event_hash",
    "create_audit_checkpoint",
    "prepare_audit_event",
    "scrub_json_value",
    "verify_audit_checkpoint",
    "verify_audit_events",
    "verify_audit_jsonl",
]

__version__ = "0.2.0"
