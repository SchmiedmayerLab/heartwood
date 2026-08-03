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
from heartwood.audit._local_signer_setup import (
    LocalCheckpointSignerSetup,
    initialize_local_checkpoint_signer,
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
from heartwood.audit._signer import (
    CheckpointSigner,
    CheckpointSignerError,
    LocalEd25519CheckpointSigner,
    RemoteCheckpointSigner,
    checkpoint_public_key_fingerprint,
    checkpoint_signature_payload_bytes,
    load_checkpoint_public_key,
    verify_checkpoint_signature,
)
from heartwood.audit._signer_registry import (
    CheckpointSignerProfile,
    CheckpointSignerRegistry,
    discover_checkpoint_signer_registry,
    load_checkpoint_signer_registry,
    user_checkpoint_signer_registry_path,
)
from heartwood.audit._signer_service import LocalCheckpointSignerApp

__all__ = [
    "AUDIT_FILENAME",
    "CHECKPOINT_FILENAME",
    "AuditCheckpointError",
    "AuditCheckpointVerification",
    "AuditIntegrityError",
    "AuditLog",
    "AuditVerification",
    "CheckpointSigner",
    "CheckpointSignerError",
    "CheckpointSignerProfile",
    "CheckpointSignerRegistry",
    "LocalCheckpointSignerApp",
    "LocalCheckpointSignerSetup",
    "LocalEd25519CheckpointSigner",
    "RemoteCheckpointSigner",
    "__version__",
    "canonical_audit_jsonl",
    "checkpoint_public_key_fingerprint",
    "checkpoint_signature_payload_bytes",
    "compute_event_hash",
    "create_audit_checkpoint",
    "discover_checkpoint_signer_registry",
    "initialize_local_checkpoint_signer",
    "load_checkpoint_public_key",
    "load_checkpoint_signer_registry",
    "prepare_audit_event",
    "scrub_json_value",
    "user_checkpoint_signer_registry_path",
    "verify_audit_checkpoint",
    "verify_audit_events",
    "verify_audit_jsonl",
    "verify_checkpoint_signature",
]

__version__ = "0.3.0-beta.1"
