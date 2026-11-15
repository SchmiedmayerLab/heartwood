# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Checkpoint trusted session boundaries using the existing journal and records sink."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from typing import Literal, cast

from heartwood.audit import AuditIntegrityError, canonical_audit_jsonl, verify_audit_jsonl
from heartwood.audit._continuity import (
    RetainedCheckpoint,
    checkpoint_context,
    verify_checkpoint_extension,
)
from heartwood.audit._records_s3 import RecordsUnavailableError, S3CheckpointRecords
from heartwood.audit._signer import (
    CheckpointPublicKey,
    CheckpointSigner,
    CheckpointSignerError,
    verify_checkpoint_signature,
)
from heartwood.core_adapter._state import FileSessionStore, SessionRecoveryError
from heartwood.schemas import AuditCheckpointContext, AuditCheckpointStatement, JsonValue
from heartwood.session import EventKind, SessionEvent

type RecordEvent = Callable[[EventKind, dict[str, JsonValue]], SessionEvent]


class CheckpointEvidenceUnavailableError(SessionRecoveryError):
    """A required boundary remains durable but must not dispatch without its evidence."""


class SessionCheckpointEvidence:
    """Operator-selected records and signer bindings; never constructed from project content.

    The gateway's records client can stage candidates and read retained history.
    Only the independently authorized signer may publish checkpoint ordinals.
    Required mode returns only after reading the externally retained signature;
    buffered mode records a visible gap when the records boundary is unavailable.
    """

    def __init__(
        self,
        *,
        records: S3CheckpointRecords,
        signer: CheckpointSigner,
        trusted_keys: Mapping[str, CheckpointPublicKey],
        mode: Literal["required", "buffered"],
    ) -> None:
        """Bind trusted records, signing authority and an explicit evidence policy."""
        if mode not in {"required", "buffered"}:
            raise ValueError("checkpoint evidence mode must be explicit")
        self.records = records
        self.signer = signer
        self.trusted_keys = dict(trusted_keys)
        self.mode = mode

    def ensure(
        self,
        store: FileSessionStore,
        boundary: SessionEvent,
        record: RecordEvent,
        *,
        required: bool = False,
    ) -> tuple[SessionEvent, ...]:
        """Anchor the exact durable boundary, reconciling prepared publication on retry."""
        first_sequence = store.next_sequence()
        retained_requirement = boundary.payload.get("evidence_mode") == "required" or any(
            event.kind == EventKind.AUDIT_EVIDENCE_RECORDED
            and event.payload.get("boundary_event_id") == boundary.event_id
            and event.payload.get("mode") == "required"
            for event in store.replay_events()
        )
        effective_mode = "required" if required or retained_requirement else self.mode
        publication_id = "boundary-" + hashlib.sha256(boundary.event_id.encode()).hexdigest()
        try:
            retained = self._ensure(store, boundary, record, publication_id)
        except Exception as error:
            record(
                EventKind.AUDIT_EVIDENCE_RECORDED,
                {
                    "boundary_event_id": boundary.event_id,
                    "boundary_sequence": boundary.sequence,
                    "mode": effective_mode,
                    "status": "pending",
                    "code": "audit.evidence.unavailable",
                },
            )
            bufferable = isinstance(
                error, (OSError, CheckpointSignerError, RecordsUnavailableError)
            )
            if effective_mode == "required" or not bufferable:
                raise CheckpointEvidenceUnavailableError(
                    "Required external audit evidence is unavailable; the action was not dispatched"
                ) from error
        else:
            record(
                EventKind.AUDIT_EVIDENCE_RECORDED,
                {
                    "boundary_event_id": boundary.event_id,
                    "boundary_sequence": boundary.sequence,
                    "mode": effective_mode,
                    "status": "retained",
                    "checkpoint_sha256": retained.digest,
                    "covered_through_sequence": retained.checkpoint.statement.audit_event_count - 1,
                },
            )
        return tuple(event for event in store.replay_events() if event.sequence >= first_sequence)

    def _history(self) -> tuple[RetainedCheckpoint, ...]:
        previous: RetainedCheckpoint | None = None
        history = self.records.history()
        publications: set[str] = set()
        for retained in history:
            key = self.trusted_keys.get(retained.checkpoint.signature.public_key_sha256)
            if key is None:
                raise ValueError("retained signing key is not independently trusted")
            try:
                verify_checkpoint_signature(
                    statement=retained.checkpoint.statement,
                    signature=retained.checkpoint.signature,
                    public_key=key,
                )
            except CheckpointSignerError as error:
                raise AuditIntegrityError("retained checkpoint signature is invalid") from error
            context = verify_checkpoint_extension(
                statement=retained.checkpoint.statement,
                audit_content=retained.audit_content,
                project_id=self.records.scope.project_id,
                predecessor=previous,
                trusted_keys=self.trusted_keys,
            )
            if context.publication_id in publications:
                raise ValueError("retained publication identity was reused")
            publications.add(context.publication_id)
            previous = retained
        return history

    def _ensure(
        self,
        store: FileSessionStore,
        boundary: SessionEvent,
        record: RecordEvent,
        publication_id: str,
    ) -> RetainedCheckpoint:
        if store.session_id != self.records.scope.session_id:
            raise ValueError("checkpoint evidence belongs to another session")
        events = store.replay_events()
        if boundary not in events:
            raise ValueError("checkpoint boundary is not durably recorded")
        history = self._history()
        content, _ = store.verified_audit_export()
        if history and not content.startswith(history[-1].audit_content):
            raise ValueError("local history does not extend the independently retained checkpoint")

        # Finish every prepared export in order before creating another ordinal.
        # This preserves the exact statement after uncertain remote publication.
        for event in events:
            raw = event.payload.get("checkpoint_context")
            if event.kind != EventKind.AUDIT_EXPORT_RECORDED or raw is None:
                continue
            context = AuditCheckpointContext.model_validate(raw)
            if context.project_id != self.records.scope.project_id:
                raise ValueError("prepared checkpoint belongs to another project")
            if context.sequence > len(history):
                audit_events, _ = verify_audit_jsonl(content)
                snapshot = canonical_audit_jsonl(audit_events[: event.sequence + 1])
                self._publish(snapshot, event.occurred_at)
                history = self._history()
            retained = history[context.sequence - 1]
            actual = checkpoint_context(retained.checkpoint.statement, retained.audit_content)
            if actual != context:
                raise ValueError("prepared checkpoint conflicts with retained history")
            if context.publication_id == publication_id:
                if retained.checkpoint.statement.audit_event_count <= boundary.sequence:
                    raise ValueError("checkpoint does not cover the authorization boundary")
                return retained

        previous = history[-1] if history else None
        context = AuditCheckpointContext(
            project_id=self.records.scope.project_id,
            publication_id=publication_id,
            sequence=len(history) + 1,
            previous_checkpoint_sha256=previous.digest if previous else None,
        )
        exported = record(
            EventKind.AUDIT_EXPORT_RECORDED,
            {
                "event_count": store.next_sequence() + 1,
                "scrubbed": True,
                "checkpoint_context": cast(dict[str, JsonValue], context.model_dump(mode="json")),
            },
        )
        content, _ = store.verified_audit_export()
        self._publish(content, exported.occurred_at)
        retained = self._history()[-1]
        if checkpoint_context(retained.checkpoint.statement, retained.audit_content) != context:
            raise ValueError("retained checkpoint differs from the requested boundary")
        return retained

    def _publish(self, content: str, created_at: str) -> None:
        _, verification = verify_audit_jsonl(content)
        statement = AuditCheckpointStatement(
            deployment_id=self.records.scope.deployment_id,
            session_id=self.records.scope.session_id,
            created_at=created_at,
            audit_event_count=verification.event_count,
            terminal_event_hash=verification.terminal_event_hash,
            audit_content_sha256=verification.content_sha256,
            audit_size_bytes=verification.size_bytes,
            retention=self.records.scope.retention,
        )
        self.records.stage_audit(statement, content)
        signature = self.signer.sign(statement)
        key = self.trusted_keys.get(signature.public_key_sha256)
        if key is None:
            raise ValueError("checkpoint signer is not independently trusted")
        try:
            verify_checkpoint_signature(statement=statement, signature=signature, public_key=key)
        except CheckpointSignerError as error:
            raise AuditIntegrityError("checkpoint signer returned an invalid signature") from error
        history = self._history()
        context = checkpoint_context(statement, content)
        if (
            len(history) < context.sequence
            or history[context.sequence - 1].checkpoint.statement != statement
            or history[context.sequence - 1].checkpoint.signature != signature
        ):
            raise ValueError("signer response is not independently retained")
