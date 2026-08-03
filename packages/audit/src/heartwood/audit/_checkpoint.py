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

AUDIT_FILENAME = "audit.jsonl"
CHECKPOINT_FILENAME = "checkpoint.json"


class AuditCheckpointError(ValueError):
    """Raised when an authoritative audit checkpoint cannot be created or verified."""


@dataclass(frozen=True, slots=True)
class AuditCheckpointVerification:
    """Verified checkpoint metadata and the identity of its canonical audit export."""

    checkpoint: AuditCheckpoint
    audit: AuditVerification


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
) -> AuditCheckpointVerification:
    """Create one atomically published, signed audit bundle."""
    try:
        events, verification = verify_audit_jsonl(audit_content)
    except AuditIntegrityError as error:
        raise AuditCheckpointError("audit checkpoint input failed full verification") from error
    if events and any(event.session_id != session_id for event in events):
        raise AuditCheckpointError("audit checkpoint session does not match its export")

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
    _publish_bundle(output, audit_content=canonical, checkpoint=checkpoint)
    return AuditCheckpointVerification(checkpoint=checkpoint, audit=verification)


def verify_audit_checkpoint(
    *,
    bundle: Path,
    public_key: Path,
) -> AuditCheckpointVerification:
    """Verify one canonical bundle against a trusted deployment public key."""
    audit_path, checkpoint_path = _bundle_paths(bundle)
    try:
        audit_content = read_private_text(audit_path)
        checkpoint_content = read_private_text(checkpoint_path)
        raw_checkpoint = json.loads(checkpoint_content)
        checkpoint = AuditCheckpoint.model_validate(raw_checkpoint)
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
    return AuditCheckpointVerification(checkpoint=checkpoint, audit=verification)


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


def _bundle_paths(bundle: Path) -> tuple[Path, Path]:
    if bundle.is_symlink() or not bundle.is_dir():
        raise AuditCheckpointError("audit checkpoint bundle must be a regular directory")
    expected = {AUDIT_FILENAME, CHECKPOINT_FILENAME}
    try:
        entries = {path.name for path in bundle.iterdir()}
    except OSError as error:
        raise AuditCheckpointError("audit checkpoint bundle is unavailable") from error
    if entries != expected:
        raise AuditCheckpointError("audit checkpoint bundle contains unexpected files")
    return bundle / AUDIT_FILENAME, bundle / CHECKPOINT_FILENAME


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
