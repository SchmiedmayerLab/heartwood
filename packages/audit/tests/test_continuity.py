# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Continuity must reject a validly rehashed rewrite of independently retained history."""

from __future__ import annotations

import base64
from dataclasses import replace
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from heartwood.audit import AuditCheckpointError, LocalEd25519CheckpointSigner
from heartwood.audit._continuity import (
    RetainedCheckpoint,
    checkpoint_context,
    verify_checkpoint_extension,
)
from heartwood.audit._log import canonical_audit_jsonl, prepare_audit_event, verify_audit_jsonl
from heartwood.schemas import (
    AuditCheckpoint,
    AuditCheckpointContext,
    AuditCheckpointStatement,
    AuditEvent,
    AuditRetention,
    JsonValue,
)

_PROJECT = UUID("a1bf55fb-d92e-410a-8a39-8d2b23bbdfb7")
_CREATED = "2026-11-13T00:00:00Z"

type SignedHistory = tuple[RetainedCheckpoint, LocalEd25519CheckpointSigner]


def _export(events: tuple[AuditEvent, ...], context: AuditCheckpointContext) -> str:
    event = prepare_audit_event(
        session_id="session-1",
        sequence=len(events),
        previous_event_hash=events[-1].event_hash if events else None,
        event_type="audit.export.recorded",
        occurred_at=_CREATED,
        payload={"checkpoint_context": cast(dict[str, JsonValue], context.model_dump(mode="json"))},
    )
    return canonical_audit_jsonl((*events, event))


def _statement(content: str, **overrides: object) -> AuditCheckpointStatement:
    _, audit = verify_audit_jsonl(content)
    record = {
        "deployment_id": "synthetic-deployment",
        "session_id": "session-1",
        "created_at": _CREATED,
        "audit_event_count": audit.event_count,
        "terminal_event_hash": audit.terminal_event_hash,
        "audit_content_sha256": audit.content_sha256,
        "audit_size_bytes": audit.size_bytes,
        "retention": AuditRetention(policy_id="synthetic-retention", retain_until="2033-11-13"),
        **overrides,
    }
    return AuditCheckpointStatement.model_validate(record)


@pytest.fixture
def retained(tmp_path: Path) -> tuple[RetainedCheckpoint, LocalEd25519CheckpointSigner]:
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
    signer = LocalEd25519CheckpointSigner(
        private_key=path, signer_id="synthetic", key_id="test", key_version="1"
    )
    first = prepare_audit_event(
        session_id="session-1",
        sequence=0,
        previous_event_hash=None,
        event_type="approval.recorded",
        occurred_at=_CREATED,
        payload={"decision": "approved", "actor_id": "reviewer"},
    )
    content = _export(
        (first,),
        AuditCheckpointContext(
            project_id=_PROJECT,
            publication_id="first",
            sequence=1,
        ),
    )
    statement = _statement(content)
    return RetainedCheckpoint(
        AuditCheckpoint(statement=statement, signature=signer.sign(statement)), content
    ), signer


def _extension(previous: RetainedCheckpoint, **overrides: object) -> str:
    events, _ = verify_audit_jsonl(previous.audit_content)
    context = AuditCheckpointContext.model_validate(
        {
            "project_id": _PROJECT,
            "publication_id": "second",
            "sequence": 2,
            "previous_checkpoint_sha256": previous.digest,
            **overrides,
        }
    )
    return _export(events, context)


def _verify(
    content: str, previous: RetainedCheckpoint, signer: LocalEd25519CheckpointSigner
) -> None:
    result = verify_checkpoint_extension(
        statement=_statement(content),
        audit_content=content,
        project_id=_PROJECT,
        predecessor=previous,
        trusted_keys={previous.checkpoint.signature.public_key_sha256: signer.public_key},
    )
    assert result.sequence == 2


def test_genesis_and_extension_use_independently_retained_history(retained: SignedHistory) -> None:
    previous, signer = retained
    assert (
        verify_checkpoint_extension(
            statement=previous.checkpoint.statement,
            audit_content=previous.audit_content,
            project_id=_PROJECT,
            predecessor=None,
            trusted_keys={},
        ).sequence
        == 1
    )
    _verify(_extension(previous), previous, signer)


def test_rehashed_rewrite_is_rejected_even_with_correct_predecessor_digest(
    retained: SignedHistory,
) -> None:
    previous, signer = retained
    events, _ = verify_audit_jsonl(previous.audit_content)
    rewritten: list[AuditEvent] = []
    for event in events:
        payload = dict(event.payload)
        if event.sequence == 0:
            payload["actor_id"] = "different-reviewer"
        rewritten.append(
            prepare_audit_event(
                session_id=event.session_id,
                sequence=event.sequence,
                previous_event_hash=rewritten[-1].event_hash if rewritten else None,
                event_type=event.event_type,
                occurred_at=event.occurred_at,
                payload=payload,
            )
        )
    content = _export(
        tuple(rewritten),
        AuditCheckpointContext(
            project_id=_PROJECT,
            publication_id="rewrite",
            sequence=2,
            previous_checkpoint_sha256=previous.digest,
        ),
    )
    verify_audit_jsonl(content)  # A valid local hash chain is insufficient.
    with pytest.raises(AuditCheckpointError, match="rewrites or truncates"):
        _verify(content, previous, signer)


def test_truncation_cannot_start_a_new_history_with_the_retained_link(
    retained: SignedHistory,
) -> None:
    previous, signer = retained
    content = _export(
        (),
        AuditCheckpointContext(
            project_id=_PROJECT,
            publication_id="truncate",
            sequence=2,
            previous_checkpoint_sha256=previous.digest,
        ),
    )
    with pytest.raises(AuditCheckpointError, match="rewrites or truncates"):
        _verify(content, previous, signer)


@pytest.mark.parametrize(
    "overrides",
    [
        {"sequence": 3},
        {"publication_id": "first"},
        {"previous_checkpoint_sha256": "sha256:" + "0" * 64},
        {"project_id": "a1bf55fb-d92e-410a-8a39-8d2b23bbdfb8"},
    ],
)
def test_claimed_link_cannot_replace_retained_authority(
    retained: SignedHistory, overrides: dict[str, object]
) -> None:
    previous, signer = retained
    with pytest.raises(AuditCheckpointError):
        _verify(_extension(previous, **overrides), previous, signer)


def test_missing_predecessor_does_not_reset_continuity(retained: SignedHistory) -> None:
    previous, _ = retained
    content = _extension(previous)
    with pytest.raises(AuditCheckpointError, match="predecessor is unavailable"):
        verify_checkpoint_extension(
            statement=_statement(content),
            audit_content=content,
            project_id=_PROJECT,
            predecessor=None,
            trusted_keys={},
        )


def test_forged_retained_signature_is_rejected(retained: SignedHistory) -> None:
    previous, signer = retained
    signature = previous.checkpoint.signature.model_copy(
        update={
            "value": base64.b64encode(bytes(64)).decode(),
        }
    )
    forged = replace(
        previous, checkpoint=previous.checkpoint.model_copy(update={"signature": signature})
    )
    with pytest.raises(AuditCheckpointError, match="retained checkpoint failed verification"):
        _verify(_extension(forged), forged, signer)


def test_retained_key_must_be_independently_trusted(retained: SignedHistory) -> None:
    previous, _ = retained
    content = _extension(previous)
    with pytest.raises(AuditCheckpointError, match="key is not trusted"):
        verify_checkpoint_extension(
            statement=_statement(content),
            audit_content=content,
            project_id=_PROJECT,
            predecessor=previous,
            trusted_keys={},
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"deployment_id": "other-deployment"},
        {"created_at": "2026-11-12T00:00:00Z"},
        {"retention": AuditRetention(policy_id="synthetic-retention", retain_until="2030-11-13")},
    ],
)
def test_extension_preserves_scope_time_and_retention(
    retained: SignedHistory, overrides: dict[str, object]
) -> None:
    previous, signer = retained
    content = _extension(previous)
    with pytest.raises(AuditCheckpointError):
        verify_checkpoint_extension(
            statement=_statement(content, **overrides),
            audit_content=content,
            project_id=_PROJECT,
            predecessor=previous,
            trusted_keys={previous.checkpoint.signature.public_key_sha256: signer.public_key},
        )


def test_noncanonical_export_is_rejected(retained: SignedHistory) -> None:
    previous, _ = retained
    with pytest.raises(AuditCheckpointError, match="binding is invalid"):
        checkpoint_context(previous.checkpoint.statement, previous.audit_content + "\n")
