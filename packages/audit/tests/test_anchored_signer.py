# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Retained history and storage observations, rather than caller claims, gate signing."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from heartwood.audit import CheckpointSignerError, LocalEd25519CheckpointSigner
from heartwood.audit._anchored_signer import AnchoredCheckpointSigner
from heartwood.audit._checkpoint import AuditCheckpointError
from heartwood.audit._continuity import RetainedCheckpoint
from heartwood.audit._log import canonical_audit_jsonl, prepare_audit_event, verify_audit_jsonl
from heartwood.audit._records_s3 import CheckpointAuthorityScope, S3CheckpointRecords
from heartwood.audit._signer import checkpoint_public_key_fingerprint
from heartwood.schemas import (
    AuditCheckpointContext,
    AuditCheckpointSignature,
    AuditCheckpointStatement,
    AuditRetention,
    JsonValue,
)
from heartwood.schemas.experiments import ExperimentExportBinding
from packages.audit.tests.records_fixtures import MemoryS3, StoredObject

_NOW = datetime(2026, 11, 14, tzinfo=UTC)
_PROJECT = UUID("a1bf55fb-d92e-410a-8a39-8d2b23bbdfb7")
_RETENTION = AuditRetention(policy_id="synthetic-retention", retain_until="2033-11-14")


class CountingSigner:
    def __init__(self, signer: LocalEd25519CheckpointSigner) -> None:
        self.delegate = signer
        self.calls = 0

    def sign(self, statement: AuditCheckpointStatement) -> AuditCheckpointSignature:
        self.calls += 1
        return self.delegate.sign(statement)


@dataclass
class Environment:
    client: MemoryS3
    records: S3CheckpointRecords
    signer: CountingSigner
    authority: AnchoredCheckpointSigner


@pytest.fixture
def environment(tmp_path: Path) -> Environment:
    key = Ed25519PrivateKey.generate()
    path = tmp_path / "key.pem"
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    path.chmod(0o600)
    signer = CountingSigner(
        LocalEd25519CheckpointSigner(
            private_key=path, signer_id="synthetic-records", key_id="test", key_version="1"
        )
    )
    client = MemoryS3()
    records = S3CheckpointRecords(
        client=client,
        bucket="synthetic-records",
        prefix="audit",
        scope=CheckpointAuthorityScope("synthetic-deployment", _PROJECT, "session-1", _RETENTION),
    )
    authority = AnchoredCheckpointSigner(
        records=records,
        signer=signer,
        trusted_keys={checkpoint_public_key_fingerprint(key.public_key()): key.public_key()},
        clock=lambda: _NOW,
    )
    return Environment(client, records, signer, authority)


def candidate(
    previous: RetainedCheckpoint | None = None,
    *,
    publication_id: str = "first",
    created: datetime = _NOW,
    decision: str = "approved",
    experiment_content: str | None = None,
) -> tuple[AuditCheckpointStatement, str]:
    events, _ = verify_audit_jsonl(previous.audit_content if previous else "")
    action = prepare_audit_event(
        session_id="session-1",
        sequence=len(events),
        previous_event_hash=events[-1].event_hash if events else None,
        event_type="approval.recorded",
        occurred_at=created.isoformat(),
        payload={"decision": decision},
    )
    context = AuditCheckpointContext(
        project_id=_PROJECT,
        publication_id=publication_id,
        sequence=1 if previous is None else 2,
        previous_checkpoint_sha256=None if previous is None else previous.digest,
    )
    payload: dict[str, JsonValue] = {
        "checkpoint_context": cast(dict[str, JsonValue], context.model_dump(mode="json"))
    }
    if experiment_content is not None:
        payload["experiment_export"] = cast(
            dict[str, JsonValue],
            ExperimentExportBinding.from_content(experiment_content.encode()).model_dump(
                mode="json"
            ),
        )
    exported = prepare_audit_event(
        session_id="session-1",
        sequence=action.sequence + 1,
        previous_event_hash=action.event_hash,
        event_type="audit.export.recorded",
        occurred_at=created.isoformat(),
        payload=payload,
    )
    content = canonical_audit_jsonl((*events, action, exported))
    _, verified = verify_audit_jsonl(content)
    return AuditCheckpointStatement(
        deployment_id="synthetic-deployment",
        session_id="session-1",
        created_at=created.isoformat(),
        audit_event_count=verified.event_count,
        terminal_event_hash=verified.terminal_event_hash,
        audit_content_sha256=verified.content_sha256,
        audit_size_bytes=verified.size_bytes,
        retention=_RETENTION,
    ), content


def anchor(environment: Environment) -> RetainedCheckpoint:
    statement, content = candidate()
    environment.records.stage_audit(statement, content)
    signature = environment.authority.sign(statement)
    retained = environment.records.history()[0]
    assert signature == retained.checkpoint.signature
    return retained


def test_signature_is_returned_only_after_retained_publication(environment: Environment) -> None:
    first = anchor(environment)
    statement, content = candidate(first, publication_id="second")
    environment.records.stage_audit(statement, content)
    signature = environment.authority.sign(statement)
    assert environment.records.history()[-1].checkpoint.signature == signature
    assert environment.signer.calls == 2
    assert environment.client.writes == 4
    assert environment.authority.sign(statement) == signature
    assert environment.signer.calls == 2


def test_missing_candidate_never_reaches_signing_key(environment: Environment) -> None:
    statement, _ = candidate()
    with pytest.raises(CheckpointSignerError):
        environment.authority.sign(statement)
    assert environment.signer.calls == 0


@pytest.mark.parametrize(("field", "value"), [("deployment_id", "other"), ("session_id", "other")])
def test_scope_is_authorized_before_records_access(
    environment: Environment, field: str, value: str
) -> None:
    statement, _ = candidate()
    statement = AuditCheckpointStatement.model_validate(
        {**statement.model_dump(mode="json"), field: value}
    )
    with pytest.raises(CheckpointSignerError):
        environment.authority.sign(statement)
    assert environment.client.calls == environment.signer.calls == 0


def test_requested_retention_cannot_replace_authority_policy(environment: Environment) -> None:
    statement, _ = candidate()
    shortened = statement.model_copy(
        update={"retention": _RETENTION.model_copy(update={"retain_until": "2027-11-14"})}
    )
    with pytest.raises(CheckpointSignerError):
        environment.authority.sign(shortened)
    assert environment.client.calls == environment.signer.calls == 0


@pytest.mark.parametrize("fault", ["shortened", "governance", "duplicate", "delete-marker"])
def test_retention_and_version_ambiguity_prevent_signing(
    environment: Environment, fault: str
) -> None:
    statement, content = candidate()
    environment.records.stage_audit(statement, content)
    key, values = next(iter(environment.client.objects.items()))
    if fault == "shortened":
        values[0].deadline = _NOW + timedelta(days=1)
    elif fault == "governance":
        values[0].mode = "GOVERNANCE"
    elif fault == "duplicate":
        values.append(StoredObject(content.encode(), values[0].deadline, "hidden-version"))
    else:
        environment.client.delete_markers.append(key)
    with pytest.raises(CheckpointSignerError):
        environment.authority.sign(statement)
    assert environment.signer.calls == 0


def test_retained_head_cannot_be_replaced_with_a_new_genesis(environment: Environment) -> None:
    anchor(environment)
    statement, content = candidate(publication_id="replacement", decision="rejected")
    environment.records.stage_audit(statement, content)
    with pytest.raises(CheckpointSignerError):
        environment.authority.sign(statement)
    assert environment.signer.calls == 1
    assert len(environment.records.history()) == 1


def test_publication_identifier_reuse_with_different_content_is_rejected(
    environment: Environment,
) -> None:
    first = anchor(environment)
    statement, content = candidate(first)  # Reuses the first publication identifier.
    environment.records.stage_audit(statement, content)
    with pytest.raises(CheckpointSignerError):
        environment.authority.sign(statement)
    assert environment.signer.calls == 1


def test_lost_publication_reply_reconciles_observed_retained_bytes(
    environment: Environment,
) -> None:
    environment.client.lost_response = True
    first = anchor(environment)
    assert environment.authority.sign(first.checkpoint.statement) == first.checkpoint.signature
    assert environment.signer.calls == 1


def test_failed_publication_does_not_return_signature(environment: Environment) -> None:
    statement, content = candidate()
    environment.records.stage_audit(statement, content)
    environment.client.fail_before_write = True
    with pytest.raises(CheckpointSignerError):
        environment.authority.sign(statement)
    assert environment.signer.calls == 1
    assert environment.records.history() == ()


@pytest.mark.parametrize("offset", [timedelta(minutes=6), timedelta(days=1)])
def test_new_checkpoint_time_is_checked_against_authority_clock(
    environment: Environment, offset: timedelta
) -> None:
    statement, content = candidate(created=_NOW + offset)
    environment.records.stage_audit(statement, content)
    with pytest.raises(CheckpointSignerError):
        environment.authority.sign(statement)
    assert environment.signer.calls == 0


def test_retry_can_reconcile_an_old_publication_without_resigning(environment: Environment) -> None:
    first = anchor(environment)
    key = environment.signer.delegate.public_key
    recovered = AnchoredCheckpointSigner(
        records=environment.records,
        signer=environment.signer,
        trusted_keys={checkpoint_public_key_fingerprint(key): key},
        clock=lambda: _NOW + timedelta(days=1),
    )
    assert recovered.sign(first.checkpoint.statement) == first.checkpoint.signature
    assert environment.signer.calls == 1


def test_prepared_snapshot_can_be_published_after_a_long_outage(environment: Environment) -> None:
    statement, content = candidate(created=_NOW - timedelta(days=1))
    environment.records.stage_audit(statement, content)
    signature = environment.authority.sign(statement)
    assert environment.records.history()[0].checkpoint.signature == signature


def test_unchanged_bytes_are_not_enough_without_independent_signing_trust(
    environment: Environment,
) -> None:
    first = anchor(environment)
    recovered = AnchoredCheckpointSigner(
        records=environment.records, signer=environment.signer, trusted_keys={}, clock=lambda: _NOW
    )
    with pytest.raises(CheckpointSignerError):
        recovered.sign(first.checkpoint.statement)
    assert environment.signer.calls == 1


def test_deleted_checkpoint_marker_is_not_treated_as_empty_history(
    environment: Environment,
) -> None:
    first = anchor(environment)
    environment.client.delete_markers.append(
        next(key for key in environment.client.objects if "/checkpoints/" in key)
    )
    with pytest.raises(CheckpointSignerError):
        environment.authority.sign(first.checkpoint.statement)


def test_same_ordinal_cannot_publish_a_different_statement(environment: Environment) -> None:
    first = anchor(environment)
    statement, content = candidate(publication_id="different", decision="denied")
    environment.records.stage_audit(statement, content)
    conflicting = first.checkpoint.model_copy(
        update={"statement": statement, "signature": environment.signer.sign(statement)}
    )
    with pytest.raises(AuditCheckpointError, match="conflicts"):
        environment.records.publish(conflicting)
    assert environment.records.history() == (first,)


def test_bound_experiment_export_must_be_independently_retained(environment: Environment) -> None:
    statement, content = candidate(experiment_content="")
    with pytest.raises(AuditCheckpointError, match="missing its bound experiment"):
        environment.records.stage_audit(statement, content)
    environment.records.stage_audit(statement, content, experiment_content="")
    environment.authority.sign(statement)
    assert environment.signer.calls == 1
    key = next(key for key in environment.client.objects if "/experiments/" in key)
    environment.client.objects[key][0].content = b"altered export"
    with pytest.raises(CheckpointSignerError):
        environment.authority.sign(statement)
    assert environment.signer.calls == 1


def test_exact_statement_reconciliation_keeps_the_retained_signature(
    environment: Environment, tmp_path: Path
) -> None:
    first = anchor(environment)
    rotated = LocalEd25519CheckpointSigner(
        private_key=tmp_path / "key.pem",
        signer_id="synthetic-records",
        key_id="test",
        key_version="2",
    )
    other_signature = rotated.sign(first.checkpoint.statement)
    assert other_signature != first.checkpoint.signature
    retained = environment.records.publish(
        first.checkpoint.model_copy(update={"signature": other_signature})
    )
    assert retained == first
    assert len(environment.records.history()) == 1
