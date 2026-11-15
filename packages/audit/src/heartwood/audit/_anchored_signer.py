# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Authorize and retain a continuous history before returning a checkpoint signature."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta

from heartwood.audit._checkpoint import AuditCheckpointError
from heartwood.audit._continuity import checkpoint_context, verify_checkpoint_extension
from heartwood.audit._records_s3 import S3CheckpointRecords, VerifiedHistory
from heartwood.audit._signer import (
    CheckpointPublicKey,
    CheckpointSigner,
    CheckpointSignerError,
    verify_checkpoint_signature,
)
from heartwood.schemas import AuditCheckpoint, AuditCheckpointSignature, AuditCheckpointStatement


class AnchoredCheckpointSigner:
    """Wrap the existing signer at an independently authenticated records boundary.

    Neither caller-provided predecessor history nor caller-selected signing keys
    are accepted. The fixed records scope must come from operator authorization.
    A gateway must not instantiate this authority with credentials available to
    its worker or treat a plain signature response as proof of this deployment.

    The first request verifies the complete retained history. Later requests extend
    that verification and reject any change to it; a failed request discards it.
    """

    def __init__(
        self,
        *,
        records: S3CheckpointRecords,
        signer: CheckpointSigner,
        trusted_keys: Mapping[str, CheckpointPublicKey],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._records = records
        self._signer = signer
        self._trusted_keys = dict(trusted_keys)
        self._clock = clock
        self._verified: VerifiedHistory | None = None
        self._lock = threading.Lock()

    def sign(self, statement: AuditCheckpointStatement) -> AuditCheckpointSignature:
        """Return a verified signature only after observing immutable publication."""
        try:
            return self._sign(statement)
        except Exception as error:
            # Preserve a bounded public error without leaking SDK endpoints,
            # request headers, storage metadata or provider exception messages.
            raise CheckpointSignerError("checkpoint could not be independently anchored") from error

    def _sign(self, statement: AuditCheckpointStatement) -> AuditCheckpointSignature:
        scope = self._records.scope
        scope.authorize(statement)
        now = self._clock()
        if now.tzinfo is None or scope.retention_deadline <= now:
            raise AuditCheckpointError("authorized records retention has expired")
        content = self._records.audit_content(statement)
        context = checkpoint_context(statement, content)
        if context.project_id != scope.project_id:
            raise AuditCheckpointError("checkpoint candidate belongs to another project")

        history = self._history()
        for previous, binding in zip(history.checkpoints, history.contexts, strict=True):
            if binding.publication_id == context.publication_id:
                # The statement binds the digest of the exact audit bytes.
                if previous.statement != statement:
                    raise AuditCheckpointError(
                        "publication identity conflicts with retained history"
                    )
                return previous.signature

        created = datetime.fromisoformat(statement.created_at.replace("Z", "+00:00"))
        # The timestamp identifies the prepared snapshot, not publication time.
        # A delayed retry must retain that exact statement after an outage.
        if created > now + timedelta(minutes=5):
            raise AuditCheckpointError("checkpoint creation time is outside the authorized window")
        verify_checkpoint_extension(
            statement=statement,
            audit_content=content,
            project_id=scope.project_id,
            predecessor=history.head,
            trusted_keys=self._trusted_keys,
        )
        checkpoint = AuditCheckpoint(statement=statement, signature=self._signer.sign(statement))
        self._verify_signature(checkpoint)
        retained = self._records.publish(checkpoint)
        self._verify_signature(retained.checkpoint)
        return retained.checkpoint.signature

    def _history(self) -> VerifiedHistory:
        since = self._verified
        try:
            history = self._records.verified_history(self._trusted_keys, since)
        except Exception:
            with self._lock:
                self._verified = None
            raise
        with self._lock:
            # Concurrent requests may finish in any order; keep the longest verified history.
            if self._verified is None or len(history.checkpoints) >= len(
                self._verified.checkpoints
            ):
                self._verified = history
        return history

    def _verify_signature(self, checkpoint: AuditCheckpoint) -> None:
        key = self._trusted_keys.get(checkpoint.signature.public_key_sha256)
        if key is None:
            raise AuditCheckpointError("checkpoint signing key is not independently trusted")
        verify_checkpoint_signature(
            statement=checkpoint.statement, signature=checkpoint.signature, public_key=key
        )
