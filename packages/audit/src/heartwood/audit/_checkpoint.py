# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Deployment-owned signing and verification for authoritative audit exports."""

from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from heartwood.audit._log import (
    AuditIntegrityError,
    AuditVerification,
    canonical_audit_jsonl,
    verify_audit_jsonl,
)
from heartwood.audit._signer import (
    CheckpointSigner,
    CheckpointSignerError,
    load_checkpoint_public_key,
    verify_checkpoint_signature,
)
from heartwood.persistence import (
    DurableFileError,
    fsync_directory,
    native_file_lock,
    read_private_text,
    write_private_json_atomic,
    write_private_text_atomic,
)
from heartwood.schemas import (
    AuditCheckpoint,
    AuditCheckpointStatement,
    AuditEvent,
    AuditRetention,
)
from heartwood.schemas.experiments import ExperimentExportBinding

AUDIT_FILENAME = "audit.jsonl"
CHECKPOINT_FILENAME = "checkpoint.json"
EXPERIMENTS_FILENAME = "experiments.jsonl"


class AuditCheckpointError(ValueError):
    """Raised when an authoritative audit checkpoint cannot be created or verified."""


@dataclass(frozen=True, slots=True)
class AuditCheckpointVerification:
    """Verified checkpoint metadata and the identity of its canonical audit export."""

    checkpoint: AuditCheckpoint
    audit: AuditVerification
    experiments: ExperimentExportBinding | None = None


def create_audit_checkpoint(
    *,
    audit_content: str,
    session_id: str,
    output: Path,
    deployment_id: str,
    retention_policy_id: str,
    retain_until: str,
    signer: CheckpointSigner,
    created_at: str | None = None,
    experiment_content: str | None = None,
) -> AuditCheckpointVerification:
    """Create one atomically published, signed audit bundle."""
    try:
        events, verification = verify_audit_jsonl(audit_content)
    except AuditIntegrityError as error:
        raise AuditCheckpointError("audit checkpoint input failed full verification") from error
    if events and any(event.session_id != session_id for event in events):
        raise AuditCheckpointError("audit checkpoint session does not match its export")
    experiments = _verify_experiment_content(events, experiment_content)

    canonical = canonical_audit_jsonl(events)
    try:
        statement = AuditCheckpointStatement(
            deployment_id=deployment_id,
            session_id=session_id,
            created_at=created_at or _utc_now(),
            audit_event_count=verification.event_count,
            terminal_event_hash=verification.terminal_event_hash,
            audit_content_sha256=verification.content_sha256,
            audit_size_bytes=verification.size_bytes,
            retention=AuditRetention(
                policy_id=retention_policy_id,
                retain_until=retain_until,
            ),
        )
    except ValidationError as error:
        raise AuditCheckpointError("audit checkpoint metadata is invalid") from error
    try:
        signature = signer.sign(statement)
    except CheckpointSignerError as error:
        raise AuditCheckpointError(str(error)) from error
    checkpoint = AuditCheckpoint(statement=statement, signature=signature)
    _publish_bundle(
        output,
        audit_content=canonical,
        checkpoint=checkpoint,
        experiment_content=experiment_content,
    )
    return AuditCheckpointVerification(
        checkpoint=checkpoint, audit=verification, experiments=experiments
    )


def verify_audit_checkpoint(
    *,
    bundle: Path,
    public_key: Path,
) -> AuditCheckpointVerification:
    """Verify one canonical bundle against a trusted deployment public key."""
    audit_path, checkpoint_path, experiment_path = _bundle_paths(bundle)
    try:
        audit_content = read_private_text(audit_path)
        checkpoint_content = read_private_text(checkpoint_path)
        raw_checkpoint = json.loads(checkpoint_content)
        checkpoint = AuditCheckpoint.model_validate(raw_checkpoint)
        experiment_content = read_private_text(experiment_path) if experiment_path else None
    except (
        DurableFileError,
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValidationError,
    ) as error:
        raise AuditCheckpointError("audit checkpoint bundle is malformed") from error
    if checkpoint_content != _canonical_checkpoint(checkpoint):
        raise AuditCheckpointError("audit checkpoint metadata is not canonical")

    try:
        events, verification = verify_audit_jsonl(audit_content)
    except AuditIntegrityError as error:
        raise AuditCheckpointError("checkpointed audit export failed full verification") from error
    if audit_content != canonical_audit_jsonl(events):
        raise AuditCheckpointError("checkpointed audit export is not canonical")
    _verify_statement(checkpoint.statement, events=events, verification=verification)

    try:
        trusted_key = load_checkpoint_public_key(public_key)
        verify_checkpoint_signature(
            statement=checkpoint.statement,
            signature=checkpoint.signature,
            public_key=trusted_key,
        )
    except CheckpointSignerError as error:
        raise AuditCheckpointError(str(error)) from error
    experiments = _verify_experiment_content(events, experiment_content)
    return AuditCheckpointVerification(
        checkpoint=checkpoint, audit=verification, experiments=experiments
    )


def _verify_experiment_content(
    events: tuple[AuditEvent, ...], content: str | None
) -> ExperimentExportBinding | None:
    raw = (
        events[-1].payload.get("experiment_export")
        if events and events[-1].event_type == "audit.export.recorded"
        else None
    )
    if raw is None:
        if content is not None:
            raise AuditCheckpointError("experiment export is not bound by the terminal audit event")
        return None
    try:
        binding = ExperimentExportBinding.model_validate(raw)
    except ValidationError:
        raise AuditCheckpointError("experiment export binding is invalid") from None
    if content is None:
        raise AuditCheckpointError("checkpoint is missing its bound experiment export")
    if binding != ExperimentExportBinding.from_content(content.encode("utf-8")):
        raise AuditCheckpointError("experiment export does not match its signed binding")
    return binding


def _verify_statement(
    statement: AuditCheckpointStatement,
    *,
    events: tuple[AuditEvent, ...],
    verification: AuditVerification,
) -> None:
    actual_session_id = events[0].session_id if events else statement.session_id
    if (
        statement.session_id != actual_session_id
        or statement.audit_event_count != verification.event_count
        or statement.terminal_event_hash != verification.terminal_event_hash
        or statement.audit_content_sha256 != verification.content_sha256
        or statement.audit_size_bytes != verification.size_bytes
    ):
        raise AuditCheckpointError("audit checkpoint statement does not match its export")


def _publish_bundle(
    output: Path,
    *,
    audit_content: str,
    checkpoint: AuditCheckpoint,
    experiment_content: str | None,
) -> None:
    output = output.expanduser()
    parent = output.parent
    if output.exists() or output.is_symlink():
        raise AuditCheckpointError("audit checkpoint output already exists")
    if parent.is_symlink():
        raise AuditCheckpointError("audit checkpoint parent must not be a symbolic link")
    try:
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as error:
        raise AuditCheckpointError("audit checkpoint parent is unavailable") from error
    if not parent.is_dir():
        raise AuditCheckpointError("audit checkpoint parent must be a directory")
    lock_path = parent / f".{output.name}.lock"
    staging: Path | None = None
    try:
        with native_file_lock(lock_path, secure_parent=False):
            if output.exists() or output.is_symlink():
                raise AuditCheckpointError("audit checkpoint output already exists")
            staging = Path(tempfile.mkdtemp(prefix=f".{output.name}-", dir=parent))
            staging.chmod(0o700)
            write_private_text_atomic(staging / AUDIT_FILENAME, audit_content)
            if experiment_content is not None:
                write_private_text_atomic(staging / EXPERIMENTS_FILENAME, experiment_content)
            write_private_json_atomic(
                staging / CHECKPOINT_FILENAME,
                checkpoint.model_dump(mode="json"),
            )
            fsync_directory(staging)
            staging.replace(output)
            output.chmod(0o700)
            fsync_directory(parent)
            staging = None
    except (AuditCheckpointError, DurableFileError, OSError) as error:
        if isinstance(error, AuditCheckpointError):
            raise
        raise AuditCheckpointError("unable to publish the audit checkpoint bundle") from error
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)


def _bundle_paths(bundle: Path) -> tuple[Path, Path, Path | None]:
    if bundle.is_symlink() or not bundle.is_dir():
        raise AuditCheckpointError("audit checkpoint bundle must be a regular directory")
    expected = {AUDIT_FILENAME, CHECKPOINT_FILENAME}
    try:
        entries = {path.name for path in bundle.iterdir()}
    except OSError as error:
        raise AuditCheckpointError("audit checkpoint bundle is unavailable") from error
    if entries not in (expected, expected | {EXPERIMENTS_FILENAME}):
        raise AuditCheckpointError("audit checkpoint bundle contains unexpected files")
    return (
        bundle / AUDIT_FILENAME,
        bundle / CHECKPOINT_FILENAME,
        bundle / EXPERIMENTS_FILENAME if EXPERIMENTS_FILENAME in entries else None,
    )


def _canonical_checkpoint(checkpoint: AuditCheckpoint) -> str:
    return (
        json.dumps(
            checkpoint.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        + "\n"
    )


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
