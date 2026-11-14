# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Verify an audit extension against independently retained checkpoint history."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from heartwood.audit._checkpoint import (
    AuditCheckpointError,
    _canonical_checkpoint,
    _verify_statement,
)
from heartwood.audit._log import canonical_audit_jsonl, verify_audit_jsonl
from heartwood.audit._signer import CheckpointPublicKey, verify_checkpoint_signature
from heartwood.schemas import AuditCheckpoint, AuditCheckpointContext, AuditCheckpointStatement


@dataclass(frozen=True, slots=True)
class RetainedCheckpoint:
    """Exact checkpoint and audit bytes obtained from the trusted records boundary."""

    checkpoint: AuditCheckpoint
    audit_content: str

    @property
    def digest(self) -> str:
        """Identify the complete canonical checkpoint, including its signature."""
        content = _canonical_checkpoint(self.checkpoint).encode("utf-8")
        return "sha256:" + hashlib.sha256(content).hexdigest()


def checkpoint_context(
    statement: AuditCheckpointStatement, audit_content: str
) -> AuditCheckpointContext:
    """Validate the exact signed export and read its terminal continuity binding."""
    try:
        events, verification = verify_audit_jsonl(audit_content)
        if audit_content != canonical_audit_jsonl(events):
            raise ValueError("checkpoint audit content must be canonical")
        _verify_statement(statement, events=events, verification=verification)
        if not events or events[-1].event_type != "audit.export.recorded":
            raise ValueError("checkpoint context requires a terminal audit-export event")
        raw = events[-1].payload.get("checkpoint_context")
        context = AuditCheckpointContext.model_validate(raw)
        if raw != context.model_dump(mode="json"):
            raise ValueError("checkpoint context must use canonical fields")
        return context
    except ValueError as error:
        raise AuditCheckpointError("checkpoint continuity binding is invalid") from error


def verify_checkpoint_extension(
    *,
    statement: AuditCheckpointStatement,
    audit_content: str,
    project_id: UUID,
    predecessor: RetainedCheckpoint | None,
    trusted_keys: Mapping[str, CheckpointPublicKey],
) -> AuditCheckpointContext:
    """Reject a rewritten prefix or a link that differs from trusted retained history.

    The authority must obtain the predecessor and key map independently of the
    request. This check does not perform retention, publication, or scope admission.
    Those controls must complete before a required-evidence gate is satisfied.
    """
    context = checkpoint_context(statement, audit_content)
    if context.project_id != project_id:
        raise AuditCheckpointError("checkpoint project does not match the authorized project")
    if predecessor is None:
        if context.sequence != 1 or context.previous_checkpoint_sha256 is not None:
            raise AuditCheckpointError("checkpoint predecessor is unavailable")
        return context

    previous = predecessor.checkpoint
    key = trusted_keys.get(previous.signature.public_key_sha256)
    if key is None:
        raise AuditCheckpointError("retained checkpoint signing key is not trusted")
    try:
        verify_checkpoint_signature(
            statement=previous.statement, signature=previous.signature, public_key=key
        )
        previous_context = checkpoint_context(previous.statement, predecessor.audit_content)
    except ValueError as error:
        raise AuditCheckpointError("retained checkpoint failed verification") from error
    if (
        previous.statement.deployment_id != statement.deployment_id
        or previous.statement.session_id != statement.session_id
        or previous_context.project_id != project_id
    ):
        raise AuditCheckpointError("retained checkpoint belongs to a different scope")
    if (
        context.sequence != previous_context.sequence + 1
        or context.previous_checkpoint_sha256 != predecessor.digest
        or context.publication_id == previous_context.publication_id
    ):
        raise AuditCheckpointError("checkpoint does not extend the retained head")
    if len(audit_content) <= len(predecessor.audit_content) or not audit_content.startswith(
        predecessor.audit_content
    ):
        raise AuditCheckpointError("checkpoint rewrites or truncates retained audit history")
    if date.fromisoformat(statement.retention.retain_until) < date.fromisoformat(
        previous.statement.retention.retain_until
    ):
        raise AuditCheckpointError("checkpoint cannot shorten retained history's retention")
    if datetime.fromisoformat(statement.created_at.replace("Z", "+00:00")) < datetime.fromisoformat(
        previous.statement.created_at.replace("Z", "+00:00")
    ):
        raise AuditCheckpointError("checkpoint creation time precedes its retained head")
    return context
