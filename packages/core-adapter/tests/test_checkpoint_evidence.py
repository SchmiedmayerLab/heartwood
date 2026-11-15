# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Required evidence must precede dispatch, including interrupted approval recovery."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from heartwood.audit import LocalEd25519CheckpointSigner, checkpoint_public_key_fingerprint
from heartwood.audit._anchored_signer import AnchoredCheckpointSigner
from heartwood.audit._checkpoint import AuditCheckpointError, _canonical_checkpoint
from heartwood.audit._records_s3 import CheckpointAuthorityScope, S3CheckpointRecords
from heartwood.core_adapter import (
    BackendEvent,
    CommandConflictError,
    DeterministicAgentBackend,
    SessionService,
)
from heartwood.core_adapter._state import SessionRecoveryError
from heartwood.core_adapter.checkpoint_evidence import (
    CheckpointEvidenceUnavailableError,
    SessionCheckpointEvidence,
)
from heartwood.schemas import (
    AuditCheckpoint,
    AuditCheckpointSignature,
    AuditCheckpointStatement,
    AuditRetention,
)
from heartwood.session import CommandKind, EventKind, SessionCommand, SessionEvent
from packages.audit.tests.records_fixtures import MemoryS3, StoredObject


class RecordingBackend(DeterministicAgentBackend):
    def __init__(self) -> None:
        super().__init__()
        self.resolutions: list[bool] = []

    def resolve_confirmation(
        self, *, session_id: str, action_group_id: str, approved: bool
    ) -> tuple[BackendEvent, ...]:
        self.resolutions.append(approved)
        return super().resolve_confirmation(
            session_id=session_id, action_group_id=action_group_id, approved=approved
        )


@dataclass
class Environment:
    service: SessionService
    backend: RecordingBackend
    client: MemoryS3
    evidence: SessionCheckpointEvidence
    authority: AnchoredCheckpointSigner
    signer: LocalEd25519CheckpointSigner


def environment(tmp_path: Path, mode: Literal["required", "buffered"] = "required") -> Environment:
    key = Ed25519PrivateKey.generate()
    path = tmp_path / "signer.pem"
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    path.chmod(0o600)
    signer = LocalEd25519CheckpointSigner(
        private_key=path, signer_id="synthetic", key_id="test", key_version="1"
    )
    client = MemoryS3()
    records = S3CheckpointRecords(
        client=client,
        bucket="synthetic-records",
        prefix="audit",
        scope=CheckpointAuthorityScope(
            "synthetic-deployment",
            uuid4(),
            "session-main",
            AuditRetention(
                policy_id="synthetic-retention",
                retain_until=(datetime.now(UTC) + timedelta(days=7)).date().isoformat(),
            ),
        ),
    )
    trusted = {checkpoint_public_key_fingerprint(key.public_key()): key.public_key()}
    authority = AnchoredCheckpointSigner(records=records, signer=signer, trusted_keys=trusted)
    evidence = SessionCheckpointEvidence(
        records=records, signer=authority, trusted_keys=trusted, mode=mode
    )
    backend = RecordingBackend()
    service = SessionService.local_default(
        tmp_path / "project", backend=backend, env={}, checkpoint_evidence=evidence
    )
    return Environment(service, backend, client, evidence, authority, signer)


def pending_approval(env: Environment, *, approved: bool = True) -> SessionCommand:
    env.service.handle(
        SessionCommand(
            command_id="synthetic-chat",
            session_id="session-main",
            kind=CommandKind.CHAT,
            created_at=datetime.now(UTC).isoformat(),
            payload={"prompt": "Inspect the synthetic data"},
        )
    )
    group = env.backend.pending_action_group(session_id="session-main")
    assert group is not None
    return SessionCommand(
        command_id="synthetic-decision",
        session_id="session-main",
        kind=CommandKind.APPROVE if approved else CommandKind.DENY,
        created_at=datetime.now(UTC).isoformat(),
        payload={"target_type": "action-set", "target_id": group.group_id},
    )


@pytest.mark.parametrize("approved", [True, False])
def test_required_boundary_precedes_backend_and_recovery(tmp_path: Path, approved: bool) -> None:
    env = environment(tmp_path)
    command = pending_approval(env, approved=approved)
    env.client.unavailable = True
    with pytest.raises(CheckpointEvidenceUnavailableError):
        env.service.handle(command)
    assert env.backend.resolutions == []
    assert env.service.store.unresolved_command_ids() == (command.command_id,)
    with pytest.raises(CheckpointEvidenceUnavailableError):
        env.service.reconcile()
    assert env.backend.resolutions == []
    assert any(
        event.kind == EventKind.AUDIT_EVIDENCE_RECORDED and event.payload["status"] == "pending"
        for event in env.service.replay_events()
    )
    env.client.unavailable = False
    env.service.reconcile()
    assert env.backend.resolutions == [approved]
    assert not env.service.store.unresolved_command_ids()
    history = env.evidence.records.history()
    assert len(history) == 1
    approval = next(
        event for event in env.service.replay_events() if event.kind == EventKind.APPROVAL_RECORDED
    )
    assert history[0].checkpoint.statement.audit_event_count > approval.sequence
    env.service.handle(command)
    assert env.backend.resolutions == [approved]
    env.service.close()
    assert len(env.evidence.records.history()) == 2


@pytest.mark.parametrize("mode", ["required", "buffered"])
def test_signature_without_observed_retention_never_grants_dispatch(
    tmp_path: Path, mode: Literal["required", "buffered"]
) -> None:
    env = environment(tmp_path, mode)
    command = pending_approval(env)
    env.evidence.signer = env.signer
    with pytest.raises(CheckpointEvidenceUnavailableError):
        env.service.handle(command)
    assert env.backend.resolutions == []
    assert env.evidence.records.history() == ()
    env.evidence.signer = env.authority
    env.service.reconcile()
    assert env.backend.resolutions == [True]
    env.service.close()


def test_buffered_mode_reports_gap_and_later_anchors_the_history(tmp_path: Path) -> None:
    env = environment(tmp_path, "buffered")
    command = pending_approval(env)
    env.client.unavailable = True
    result = env.service.handle(command)
    assert env.backend.resolutions == [True]
    gaps = [event for event in result.events if event.kind == EventKind.AUDIT_EVIDENCE_RECORDED]
    assert gaps[-1].payload["status"] == "pending"
    assert gaps[-1].payload["mode"] == "buffered"
    env.client.unavailable = False
    env.service.close()
    assert len(env.evidence.records.history()) == 1
    assert (
        env.evidence.records.history()[0].checkpoint.statement.audit_event_count > gaps[-1].sequence
    )


def test_buffered_mode_cannot_satisfy_an_explicit_required_boundary(tmp_path: Path) -> None:
    env = environment(tmp_path, "buffered")
    pending_approval(env)
    boundary = env.service.replay_events()[-1]
    env.client.unavailable = True
    with pytest.raises(CheckpointEvidenceUnavailableError):
        env.service._checkpoint_boundary(boundary, required=True)
    assert env.backend.resolutions == []
    assert env.service.replay_events()[-1].payload["mode"] == "required"
    env.client.unavailable = False
    env.service.close()


def test_untrusted_command_cannot_supply_checkpoint_context(tmp_path: Path) -> None:
    env = environment(tmp_path)
    with pytest.raises(CommandConflictError):
        env.service.handle(
            SessionCommand(
                command_id="forged-context",
                session_id="session-main",
                kind=CommandKind.AUDIT_EXPORT,
                created_at=datetime.now(UTC).isoformat(),
                payload={"checkpoint_context": {}},
            )
        )
    assert env.service.replay_events() == ()
    env.service.close()


@pytest.mark.parametrize("change", ["missing", "buffered"])
@pytest.mark.parametrize("before_gate", [False, True])
def test_pending_required_evidence_cannot_be_downgraded(
    tmp_path: Path, change: str, before_gate: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = environment(tmp_path)
    command = pending_approval(env)
    env.client.unavailable = True
    if before_gate:

        def interrupted(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("interrupted before the records call")

        with monkeypatch.context() as patch:
            patch.setattr(env.evidence, "ensure", interrupted)
            with pytest.raises(RuntimeError, match="before the records call"):
                env.service.handle(command)
        assert not any(
            event.kind == EventKind.AUDIT_EVIDENCE_RECORDED for event in env.service.replay_events()
        )
    else:
        with pytest.raises(CheckpointEvidenceUnavailableError):
            env.service.handle(command)
    if change == "missing":
        env.service._checkpoint_evidence = None
    else:
        env.evidence.mode = "buffered"
    with pytest.raises(SessionRecoveryError):
        env.service.reconcile()
    assert env.backend.resolutions == []
    assert env.service.store.unresolved_command_ids() == (command.command_id,)
    env.service._checkpoint_evidence = env.evidence
    env.client.unavailable = False
    env.service.reconcile()
    assert env.backend.resolutions == [True]
    env.service.close()


@pytest.mark.parametrize("mode", ["required", "buffered"])
@pytest.mark.parametrize("fault", ["unknown-key", "invalid-signature"])
def test_invalid_signer_response_blocks_dispatch_until_retention_is_reconciled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: Literal["required", "buffered"],
    fault: str,
) -> None:
    env = environment(tmp_path, mode)
    command = pending_approval(env)
    sign = env.authority.sign

    def invalid_response(statement: AuditCheckpointStatement) -> AuditCheckpointSignature:
        signature = sign(statement)
        return signature.model_copy(
            update=(
                {"public_key_sha256": "sha256:" + "0" * 64}
                if fault == "unknown-key"
                else {"value": base64.b64encode(bytes(64)).decode("ascii")}
            )
        )

    with monkeypatch.context() as patch:
        patch.setattr(env.authority, "sign", invalid_response)
        with pytest.raises(CheckpointEvidenceUnavailableError):
            env.service.handle(command)
    assert env.backend.resolutions == []
    assert env.service.store.unresolved_command_ids() == (command.command_id,)
    assert len(env.evidence.records.history()) == 1
    env.service.reconcile()
    env.service.handle(command)
    assert env.backend.resolutions == [True]
    assert len(env.evidence.records.history()) == 1
    env.service.close()


@pytest.mark.parametrize("mode", ["required", "buffered"])
@pytest.mark.parametrize("fault", ["unknown-key", "invalid-signature", "local-rollback"])
def test_retained_integrity_failure_blocks_a_later_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: Literal["required", "buffered"],
    fault: str,
) -> None:
    env = environment(tmp_path, mode)
    command = pending_approval(env)
    before_approval = env.service.store.verified_audit_export()
    env.service._checkpoint_boundary(env.service.replay_events()[-1])
    retained = next(
        values[0] for key, values in env.client.objects.items() if "/checkpoints/" in key
    )
    original = retained.content
    with monkeypatch.context() as patch:
        if fault == "local-rollback":
            patch.setattr(env.service.store, "verified_audit_export", lambda: before_approval)
        else:
            checkpoint = AuditCheckpoint.model_validate_json(original)
            signature = checkpoint.signature.model_copy(
                update=(
                    {"public_key_sha256": "sha256:" + "0" * 64}
                    if fault == "unknown-key"
                    else {"value": base64.b64encode(bytes(64)).decode("ascii")}
                )
            )
            retained.content = _canonical_checkpoint(
                checkpoint.model_copy(update={"signature": signature})
            ).encode()
        with pytest.raises(CheckpointEvidenceUnavailableError):
            env.service.handle(command)
        assert env.backend.resolutions == []
        assert env.service.store.unresolved_command_ids() == (command.command_id,)
    retained.content = original
    env.service.reconcile()
    env.service.handle(command)
    assert env.backend.resolutions == [True]
    assert len(env.evidence.records.history()) == 2
    env.service.close()


@pytest.mark.parametrize("mode", ["required", "buffered"])
def test_outage_retries_record_one_pending_event_per_boundary(
    tmp_path: Path, mode: Literal["required", "buffered"]
) -> None:
    env = environment(tmp_path, mode)
    command = pending_approval(env)
    boundary = env.service.replay_events()[-1]
    env.client.unavailable = True
    if mode == "required":
        with pytest.raises(CheckpointEvidenceUnavailableError):
            env.service.handle(command)
        for _ in range(3):
            with pytest.raises(CheckpointEvidenceUnavailableError):
                env.service.reconcile()
            with pytest.raises(CheckpointEvidenceUnavailableError):
                env.service.handle(command)
    else:
        for _ in range(3):
            env.service._checkpoint_boundary(boundary)
        # A stricter requirement for the same boundary is still recorded.
        with pytest.raises(CheckpointEvidenceUnavailableError):
            env.service._checkpoint_boundary(boundary, required=True)
    pending = [
        event.payload["mode"]
        for event in env.service.replay_events()
        if event.kind == EventKind.AUDIT_EVIDENCE_RECORDED
    ]
    assert pending == (["required"] if mode == "required" else ["buffered", "required"])
    assert len(env.service.replay_events()) == len(env.service.audit_log.read())
    env.client.unavailable = False
    if mode == "required":
        env.service.reconcile()
        assert env.backend.resolutions == [True]
    else:
        env.service._checkpoint_boundary(boundary, required=True)
    statuses = [
        event.payload["status"]
        for event in env.service.replay_events()
        if event.kind == EventKind.AUDIT_EVIDENCE_RECORDED
    ]
    assert statuses == [*["pending"] * len(pending), "retained"]
    env.service.close()


def _boundary(env: Environment) -> tuple[SessionEvent, ...]:
    return env.service._checkpoint_boundary(env.service._record_event(EventKind.SESSION_PAUSED, {}))


def test_boundary_reads_a_bounded_number_of_records(tmp_path: Path) -> None:
    env = environment(tmp_path)
    env.service.handle(pending_approval(env))
    reads = []
    for _ in range(12):
        before = env.client.reads
        _boundary(env)
        reads.append(env.client.reads - before)
    # The gateway reads back its staged candidate and then the new checkpoint and its
    # candidate; the signing authority reads six records. Neither depends on history length.
    assert reads == [9] * 12
    assert len(env.evidence.records.history()) == 13
    env.service.close()


def test_failed_boundary_verifies_the_complete_history_again(tmp_path: Path) -> None:
    env = environment(tmp_path)
    env.service.handle(pending_approval(env))
    for _ in range(4):
        _boundary(env)
    env.client.unavailable = True
    with pytest.raises(CheckpointEvidenceUnavailableError):
        _boundary(env)
    env.client.unavailable = False
    reads = []
    for _ in range(2):
        before = env.client.reads
        _boundary(env)
        reads.append(env.client.reads - before)
    # The outage failed the gateway's first listing, so only the gateway discards its
    # verification and rereads the five retained checkpoints and their candidates.
    assert reads == [9 + 2 * 5, 9]
    env.service.close()


def _retained_key(env: Environment, ordinal: int) -> str:
    return next(key for key in env.client.objects if key.endswith(f"/{ordinal:020d}.json"))


@pytest.mark.parametrize("ordinal", [1, 2, 3], ids=["genesis", "middle", "head"])
@pytest.mark.parametrize(
    "fault", ["modified", "replacement-version", "different-version", "missing", "delete-marker"]
)
def test_changed_verified_history_blocks_the_next_boundary(
    tmp_path: Path, ordinal: int, fault: str
) -> None:
    env = environment(tmp_path)
    command = pending_approval(env)
    env.service.handle(command)
    _boundary(env)
    _boundary(env)
    key = _retained_key(env, ordinal)
    stored = env.client.objects[key][0]
    if fault == "modified":
        stored.content += b"\n"
    elif fault == "replacement-version":
        env.client.objects[key].append(StoredObject(stored.content, stored.deadline, "hidden"))
    elif fault == "different-version":
        env.client.objects[key] = [StoredObject(stored.content, stored.deadline, "different")]
    elif fault == "missing":
        del env.client.objects[key]
    else:
        env.client.delete_markers.append(key)
    with pytest.raises(CheckpointEvidenceUnavailableError):
        _boundary(env)
    assert sum("/checkpoints/" in key for key in env.client.objects) == 3 - (fault == "missing")
    assert env.service.replay_events()[-1].payload["status"] == "pending"


def test_restart_and_audit_verification_read_the_complete_history(tmp_path: Path) -> None:
    env = environment(tmp_path)
    env.service.handle(pending_approval(env))
    for _ in range(5):
        _boundary(env)
    env.service.close()
    restarted = SessionCheckpointEvidence(
        records=env.evidence.records,
        signer=env.authority,
        trusted_keys=env.evidence.trusted_keys,
        mode="required",
    )
    before = env.client.reads
    assert len(restarted.verify().checkpoints) == 7
    # Each retained checkpoint and its audit candidate.
    assert env.client.reads - before == 14

    # Bytes changed behind unchanged version metadata are found only by rereading them.
    stored = env.client.objects[_retained_key(env, 2)][0]
    checkpoint = AuditCheckpoint.model_validate_json(stored.content)
    stored.etag = stored.entity_tag
    stored.content = _canonical_checkpoint(
        checkpoint.model_copy(
            update={
                "signature": checkpoint.signature.model_copy(
                    update={"value": base64.b64encode(bytes(64)).decode("ascii")}
                )
            }
        )
    ).encode()
    with pytest.raises(AuditCheckpointError):
        restarted.verify()
    service = SessionService.local_default(
        tmp_path / "project", backend=env.backend, env={}, checkpoint_evidence=restarted
    )
    service.store.acquire_writer()
    with pytest.raises(CheckpointEvidenceUnavailableError):
        service._checkpoint_boundary(service._record_event(EventKind.SESSION_PAUSED, {}))
    assert sum("/checkpoints/" in key for key in env.client.objects) == 7
