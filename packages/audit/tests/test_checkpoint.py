# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Tests for deployment-owned authoritative audit checkpoints."""

from __future__ import annotations

import base64
import json
import stat
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from heartwood.audit import (
    AUDIT_FILENAME,
    CHECKPOINT_FILENAME,
    AuditCheckpointError,
    AuditLog,
    create_audit_checkpoint,
    verify_audit_checkpoint,
)
from heartwood.persistence import write_private_json_atomic

_CREATED_AT = "2026-08-02T12:00:00Z"


def test_checkpoint_round_trip_binds_origin_retention_and_verified_export(
    tmp_path: Path,
) -> None:
    audit_content = _audit_content(tmp_path)
    private_key, public_key = _write_key_pair(tmp_path)
    bundle = tmp_path / "deployment" / "session-1-checkpoint"

    created = create_audit_checkpoint(
        audit_content=audit_content,
        session_id="session-1",
        output=bundle,
        deployment_id="carina-research",
        retention_policy_id="research-audit-7y",
        retain_until="2033-08-02",
        signing_key=private_key,
        created_at=_CREATED_AT,
    )
    verified = verify_audit_checkpoint(bundle=bundle, public_key=public_key)

    assert verified == created
    assert {path.name for path in bundle.iterdir()} == {
        AUDIT_FILENAME,
        CHECKPOINT_FILENAME,
    }
    assert stat.S_IMODE(bundle.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in bundle.iterdir())
    assert created.checkpoint.statement.deployment_id == "carina-research"
    assert created.checkpoint.statement.retention.policy_id == "research-audit-7y"
    assert created.checkpoint.statement.retention.retain_until == "2033-08-02"
    assert created.checkpoint.statement.audit_content_sha256 == created.audit.content_sha256


def test_checkpoint_excludes_sensitive_audit_content(tmp_path: Path) -> None:
    private_key, _public_key = _write_key_pair(tmp_path)
    bundle = tmp_path / "checkpoint"
    create_audit_checkpoint(
        audit_content=_audit_content(tmp_path),
        session_id="session-1",
        output=bundle,
        deployment_id="terra-research",
        retention_policy_id="research-audit-7y",
        retain_until="2033-08-02",
        signing_key=private_key,
        created_at=_CREATED_AT,
    )

    persisted = (bundle / AUDIT_FILENAME).read_text(encoding="utf-8")
    checkpoint = (bundle / CHECKPOINT_FILENAME).read_text(encoding="utf-8")
    for sensitive in (
        "participant-001",
        "sk-secret",
        "analyze this participant",
        "/project/private.csv",
    ):
        assert sensitive not in persisted
        assert sensitive not in checkpoint


@pytest.mark.parametrize("target", [AUDIT_FILENAME, CHECKPOINT_FILENAME])
def test_checkpoint_verification_rejects_tampering(tmp_path: Path, target: str) -> None:
    bundle, public_key = _checkpoint_bundle(tmp_path)
    path = bundle / target
    if target == AUDIT_FILENAME:
        payload = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        payload["event_type"] = "changed"
        path.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    else:
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["signature"] = base64.b64encode(bytes(64)).decode("ascii")
        path.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")

    with pytest.raises(AuditCheckpointError):
        verify_audit_checkpoint(bundle=bundle, public_key=public_key)


def test_checkpoint_verification_rejects_wrong_trusted_key(tmp_path: Path) -> None:
    bundle, _public_key = _checkpoint_bundle(tmp_path)
    _private_key, wrong_public_key = _write_key_pair(tmp_path / "other")

    with pytest.raises(AuditCheckpointError, match="does not match"):
        verify_audit_checkpoint(bundle=bundle, public_key=wrong_public_key)


def test_checkpoint_verification_rejects_malformed_bundle_and_statement(
    tmp_path: Path,
) -> None:
    bundle, public_key = _checkpoint_bundle(tmp_path)
    checkpoint_path = bundle / CHECKPOINT_FILENAME
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    checkpoint["statement"]["audit_size_bytes"] += 1
    checkpoint_path.write_text(
        json.dumps(checkpoint, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(AuditCheckpointError, match="does not match its export"):
        verify_audit_checkpoint(bundle=bundle, public_key=public_key)

    checkpoint_path.write_text("{\n", encoding="utf-8")
    with pytest.raises(AuditCheckpointError, match="bundle is malformed"):
        verify_audit_checkpoint(bundle=bundle, public_key=public_key)


def test_checkpoint_verification_rejects_non_base64_signature(tmp_path: Path) -> None:
    bundle, public_key = _checkpoint_bundle(tmp_path)
    checkpoint_path = bundle / CHECKPOINT_FILENAME
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    checkpoint["signature"] = "not-base64!"
    checkpoint_path.write_text(
        json.dumps(checkpoint, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(AuditCheckpointError, match="bundle is malformed"):
        verify_audit_checkpoint(bundle=bundle, public_key=public_key)


def test_checkpoint_requires_private_key_permissions(tmp_path: Path) -> None:
    private_key, _public_key = _write_key_pair(tmp_path)
    private_key.chmod(0o640)

    with pytest.raises(AuditCheckpointError, match="owner-only"):
        create_audit_checkpoint(
            audit_content=_audit_content(tmp_path),
            session_id="session-1",
            output=tmp_path / "checkpoint",
            deployment_id="generic-research",
            retention_policy_id="research-audit-7y",
            retain_until="2033-08-02",
            signing_key=private_key,
            created_at=_CREATED_AT,
        )


def test_checkpoint_rejects_symlink_key_and_existing_output(tmp_path: Path) -> None:
    private_key, _public_key = _write_key_pair(tmp_path)
    key_link = tmp_path / "key-link.pem"
    key_link.symlink_to(private_key)

    with pytest.raises(AuditCheckpointError, match="regular file"):
        create_audit_checkpoint(
            audit_content=_audit_content(tmp_path),
            session_id="session-1",
            output=tmp_path / "checkpoint",
            deployment_id="generic-research",
            retention_policy_id="research-audit-7y",
            retain_until="2033-08-02",
            signing_key=key_link,
            created_at=_CREATED_AT,
        )

    output = tmp_path / "existing"
    output.mkdir()
    with pytest.raises(AuditCheckpointError, match="already exists"):
        create_audit_checkpoint(
            audit_content=_audit_content(tmp_path),
            session_id="session-1",
            output=output,
            deployment_id="generic-research",
            retention_policy_id="research-audit-7y",
            retain_until="2033-08-02",
            signing_key=private_key,
            created_at=_CREATED_AT,
        )


def test_checkpoint_rejects_invalid_input_identity_and_key_material(tmp_path: Path) -> None:
    private_key, _public_key = _write_key_pair(tmp_path)
    audit_content = _audit_content(tmp_path)

    with pytest.raises(AuditCheckpointError, match="session does not match"):
        create_audit_checkpoint(
            audit_content=audit_content,
            session_id="session-2",
            output=tmp_path / "wrong-session",
            deployment_id="generic-research",
            retention_policy_id="research-audit-7y",
            retain_until="2033-08-02",
            signing_key=private_key,
            created_at=_CREATED_AT,
        )

    missing_key = tmp_path / "missing.pem"
    with pytest.raises(AuditCheckpointError, match="signing key is unavailable"):
        create_audit_checkpoint(
            audit_content=audit_content,
            session_id="session-1",
            output=tmp_path / "missing-key",
            deployment_id="generic-research",
            retention_policy_id="research-audit-7y",
            retain_until="2033-08-02",
            signing_key=missing_key,
            created_at=_CREATED_AT,
        )

    ec_private, ec_public = _write_ec_key_pair(tmp_path / "ec")
    with pytest.raises(AuditCheckpointError, match="must use Ed25519"):
        create_audit_checkpoint(
            audit_content=audit_content,
            session_id="session-1",
            output=tmp_path / "ec-key",
            deployment_id="generic-research",
            retention_policy_id="research-audit-7y",
            retain_until="2033-08-02",
            signing_key=ec_private,
            created_at=_CREATED_AT,
        )
    bundle, _ = _checkpoint_bundle(tmp_path / "ed25519")
    with pytest.raises(AuditCheckpointError, match="must use Ed25519"):
        verify_audit_checkpoint(bundle=bundle, public_key=ec_public)
    with pytest.raises(AuditCheckpointError, match="failed full verification"):
        create_audit_checkpoint(
            audit_content=audit_content.replace("command.received", "changed"),
            session_id="session-1",
            output=tmp_path / "corrupt-input",
            deployment_id="generic-research",
            retention_policy_id="research-audit-7y",
            retain_until="2033-08-02",
            signing_key=private_key,
            created_at=_CREATED_AT,
        )

    invalid_key = tmp_path / "invalid.pem"
    invalid_key.write_text("not a private key\n", encoding="utf-8")
    invalid_key.chmod(0o600)
    with pytest.raises(AuditCheckpointError, match="valid unencrypted PEM"):
        create_audit_checkpoint(
            audit_content=audit_content,
            session_id="session-1",
            output=tmp_path / "invalid-key",
            deployment_id="generic-research",
            retention_policy_id="research-audit-7y",
            retain_until="2033-08-02",
            signing_key=invalid_key,
            created_at=_CREATED_AT,
        )


def test_checkpoint_rejects_invalid_retention_and_unexpected_files(tmp_path: Path) -> None:
    private_key, _public_key = _write_key_pair(tmp_path)
    with pytest.raises(AuditCheckpointError, match="metadata is invalid"):
        create_audit_checkpoint(
            audit_content=_audit_content(tmp_path),
            session_id="session-1",
            output=tmp_path / "invalid-retention",
            deployment_id="generic-research",
            retention_policy_id="research-audit-7y",
            retain_until="2026-08-01",
            signing_key=private_key,
            created_at=_CREATED_AT,
        )

    bundle, bundle_public_key = _checkpoint_bundle(tmp_path / "valid")
    (bundle / "unexpected.txt").write_text("not part of the checkpoint", encoding="utf-8")
    with pytest.raises(AuditCheckpointError, match="unexpected files"):
        verify_audit_checkpoint(bundle=bundle, public_key=bundle_public_key)


def test_checkpoint_rejects_unsafe_output_parents_and_bundle_paths(tmp_path: Path) -> None:
    private_key, public_key = _write_key_pair(tmp_path / "keys")
    actual_parent = tmp_path / "actual"
    actual_parent.mkdir()
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(actual_parent, target_is_directory=True)
    with pytest.raises(AuditCheckpointError, match="parent must not be a symbolic link"):
        create_audit_checkpoint(
            audit_content=_audit_content(tmp_path),
            session_id="session-1",
            output=linked_parent / "checkpoint",
            deployment_id="generic-research",
            retention_policy_id="research-audit-7y",
            retain_until="2033-08-02",
            signing_key=private_key,
            created_at=_CREATED_AT,
        )

    parent_file = tmp_path / "parent-file"
    parent_file.write_text("not a directory\n", encoding="utf-8")
    with pytest.raises(AuditCheckpointError, match="parent is unavailable"):
        create_audit_checkpoint(
            audit_content=_audit_content(tmp_path / "second"),
            session_id="session-1",
            output=parent_file / "checkpoint",
            deployment_id="generic-research",
            retention_policy_id="research-audit-7y",
            retain_until="2033-08-02",
            signing_key=private_key,
            created_at=_CREATED_AT,
        )

    not_a_bundle = tmp_path / "not-a-bundle"
    not_a_bundle.write_text("file\n", encoding="utf-8")
    with pytest.raises(AuditCheckpointError, match="regular directory"):
        verify_audit_checkpoint(bundle=not_a_bundle, public_key=public_key)


@pytest.mark.parametrize("target", [AUDIT_FILENAME, CHECKPOINT_FILENAME])
def test_checkpoint_verification_rejects_noncanonical_bundle_files(
    tmp_path: Path,
    target: str,
) -> None:
    bundle, public_key = _checkpoint_bundle(tmp_path)
    path = bundle / target
    path.write_text("\n" + path.read_text(encoding="utf-8"), encoding="utf-8")

    with pytest.raises(AuditCheckpointError, match="not canonical"):
        verify_audit_checkpoint(bundle=bundle, public_key=public_key)


def test_interrupted_checkpoint_publish_leaves_no_partial_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_key, _public_key = _write_key_pair(tmp_path)
    bundle = tmp_path / "deployment" / "checkpoint"

    def interrupt_checkpoint(path: Path, payload: Mapping[str, object]) -> None:
        if path.name == CHECKPOINT_FILENAME:
            raise OSError("synthetic checkpoint interruption")
        write_private_json_atomic(path, payload)

    monkeypatch.setattr(
        "heartwood.audit._checkpoint.write_private_json_atomic",
        interrupt_checkpoint,
    )

    with pytest.raises(AuditCheckpointError, match="unable to publish"):
        create_audit_checkpoint(
            audit_content=_audit_content(tmp_path),
            session_id="session-1",
            output=bundle,
            deployment_id="generic-research",
            retention_policy_id="research-audit-7y",
            retain_until="2033-08-02",
            signing_key=private_key,
            created_at=_CREATED_AT,
        )

    assert not bundle.exists()
    assert not tuple(bundle.parent.glob(".checkpoint-*"))


def test_concurrent_checkpoint_publish_has_one_verified_winner(tmp_path: Path) -> None:
    private_key, public_key = _write_key_pair(tmp_path)
    bundle = tmp_path / "deployment" / "checkpoint"
    audit_content = _audit_content(tmp_path)

    def publish() -> str:
        try:
            create_audit_checkpoint(
                audit_content=audit_content,
                session_id="session-1",
                output=bundle,
                deployment_id="generic-research",
                retention_policy_id="research-audit-7y",
                retain_until="2033-08-02",
                signing_key=private_key,
                created_at=_CREATED_AT,
            )
        except AuditCheckpointError as error:
            return str(error)
        return "created"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(lambda _index: publish(), range(2)))

    assert sorted(outcomes) == ["audit checkpoint output already exists", "created"]
    assert verify_audit_checkpoint(bundle=bundle, public_key=public_key).audit.event_count == 1


def _audit_content(tmp_path: Path) -> str:
    log = AuditLog(tmp_path / "source" / "audit.jsonl")
    log.append(
        session_id="session-1",
        event_type="command.received",
        occurred_at="2026-08-02T11:59:00Z",
        payload={
            "prompt": "analyze this participant",
            "participant_id": "participant-001",
            "token": "sk-secret",
            "path": "/project/private.csv",
            "command_id": "command-1",
        },
    )
    return log.export_jsonl()


def _checkpoint_bundle(tmp_path: Path) -> tuple[Path, Path]:
    private_key, public_key = _write_key_pair(tmp_path)
    bundle = tmp_path / "checkpoint"
    create_audit_checkpoint(
        audit_content=_audit_content(tmp_path),
        session_id="session-1",
        output=bundle,
        deployment_id="generic-research",
        retention_policy_id="research-audit-7y",
        retain_until="2033-08-02",
        signing_key=private_key,
        created_at=_CREATED_AT,
    )
    return bundle, public_key


def _write_key_pair(root: Path) -> tuple[Path, Path]:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = Ed25519PrivateKey.generate()
    private_path = root / "audit-private.pem"
    public_path = root / "audit-public.pem"
    private_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    private_path.chmod(0o600)
    public_path.write_bytes(
        key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    public_path.chmod(0o644)
    return private_path, public_path


def _write_ec_key_pair(root: Path) -> tuple[Path, Path]:
    root.mkdir(mode=0o700, parents=True)
    key = ec.generate_private_key(ec.SECP256R1())
    private_path = root / "audit-private.pem"
    public_path = root / "audit-public.pem"
    private_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    private_path.chmod(0o600)
    public_path.write_bytes(
        key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return private_path, public_path
