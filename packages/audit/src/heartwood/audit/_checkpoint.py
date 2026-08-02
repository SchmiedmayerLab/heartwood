# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Deployment-owned signing and verification for authoritative audit exports."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import ValidationError

from heartwood.audit._log import (
    AuditIntegrityError,
    AuditVerification,
    canonical_audit_jsonl,
    verify_audit_jsonl,
)
from heartwood.persistence import (
    DurableFileError,
    fsync_directory,
    native_file_lock,
    read_private_bytes,
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
_CHECKPOINT_DOMAIN = b"heartwood.audit-checkpoint.v1\x00"
_MAXIMUM_KEY_FILE_BYTES = 64 * 1024


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
    signing_key: Path,
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
    private_key = _load_private_key(signing_key)
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
    signature = private_key.sign(_statement_bytes(statement))
    checkpoint = AuditCheckpoint(
        statement=statement,
        signing_key_id=_key_id(private_key.public_key()),
        signature=base64.b64encode(signature).decode("ascii"),
    )
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

    trusted_key = _load_public_key(public_key)
    if checkpoint.signing_key_id != _key_id(trusted_key):
        raise AuditCheckpointError("audit checkpoint signing key does not match the trusted key")
    try:
        signature = base64.b64decode(checkpoint.signature, validate=True)
    except binascii.Error as error:
        raise AuditCheckpointError("audit checkpoint signature is invalid") from error
    try:
        trusted_key.verify(signature, _statement_bytes(checkpoint.statement))
    except (InvalidSignature, ValueError) as error:
        raise AuditCheckpointError("audit checkpoint signature is invalid") from error
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


def _load_private_key(path: Path) -> Ed25519PrivateKey:
    metadata = _key_metadata(path, private=True)
    if metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise AuditCheckpointError("audit signing key permissions must be owner-only")
    if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
        raise AuditCheckpointError("audit signing key must be owned by the current user")
    try:
        key = serialization.load_pem_private_key(read_private_bytes(path), password=None)
    except (DurableFileError, OSError, TypeError, ValueError) as error:
        raise AuditCheckpointError(
            "audit signing key is not a valid unencrypted PEM key"
        ) from error
    if not isinstance(key, Ed25519PrivateKey):
        raise AuditCheckpointError("audit signing key must use Ed25519")
    return key


def _load_public_key(path: Path) -> Ed25519PublicKey:
    _key_metadata(path, private=False)
    try:
        key = serialization.load_pem_public_key(read_private_bytes(path))
    except (DurableFileError, OSError, ValueError) as error:
        raise AuditCheckpointError("trusted audit public key is not a valid PEM key") from error
    if not isinstance(key, Ed25519PublicKey):
        raise AuditCheckpointError("trusted audit public key must use Ed25519")
    return key


def _key_metadata(path: Path, *, private: bool) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as error:
        label = "signing key" if private else "public key"
        raise AuditCheckpointError(f"audit {label} is unavailable") from error
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAXIMUM_KEY_FILE_BYTES:
        label = "signing key" if private else "public key"
        raise AuditCheckpointError(f"audit {label} must be a bounded regular file")
    return metadata


def _key_id(key: Ed25519PublicKey) -> str:
    raw = key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _statement_bytes(statement: AuditCheckpointStatement) -> bytes:
    payload = json.dumps(
        statement.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return _CHECKPOINT_DOMAIN + payload


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
