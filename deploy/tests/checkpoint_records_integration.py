# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Qualify checkpoint records against a task-owned S3 server process.

Run inside a network-disabled qualification container with a native ``minio``
binary. Only synthetic credentials and records are used. No existing bucket,
service, credential profile or external endpoint is accepted by this harness.
"""

from __future__ import annotations

import argparse
import base64
import importlib
import json
import os
import secrets
import socket
import ssl
import subprocess
import threading
import time
import urllib.request
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, cast
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from heartwood.audit import CheckpointSignerError, LocalEd25519CheckpointSigner
from heartwood.audit._anchored_signer import AnchoredCheckpointSigner
from heartwood.audit._continuity import RetainedCheckpoint, checkpoint_context
from heartwood.audit._log import canonical_audit_jsonl, prepare_audit_event, verify_audit_jsonl
from heartwood.audit._records_s3 import (
    CheckpointAuthorityScope,
    S3CheckpointRecords,
    S3RecordsClient,
)
from heartwood.audit._signer import CheckpointPublicKey, checkpoint_public_key_fingerprint
from heartwood.core_adapter import SessionService
from heartwood.core_adapter.checkpoint_evidence import (
    CheckpointEvidenceUnavailableError,
    SessionCheckpointEvidence,
)
from heartwood.schemas import (
    AuditCheckpointContext,
    AuditCheckpointSignature,
    AuditCheckpointStatement,
    AuditEvent,
    AuditRetention,
    JsonValue,
)
from heartwood.session import CommandKind, EventKind, SessionCommand


class QualificationClient(S3RecordsClient, Protocol):
    def create_bucket(self, **kwargs: object) -> Mapping[str, Any]: ...

    def put_bucket_policy(self, **kwargs: object) -> Mapping[str, Any]: ...

    def delete_object(self, **kwargs: object) -> Mapping[str, Any]: ...

    def put_object_retention(self, **kwargs: object) -> Mapping[str, Any]: ...


class ObjectStore:
    def __init__(
        self,
        binary: Path,
        state: Path,
        owned: list[ObjectStore],
        *,
        certificates: Path | None = None,
    ) -> None:
        self.access = "synthetic-" + secrets.token_hex(8)
        self.secret = secrets.token_hex(32)
        self._environment = {
            **os.environ,
            "MINIO_ROOT_USER": self.access,
            "MINIO_ROOT_PASSWORD": self.secret,
            "MINIO_BROWSER": "off",
        }
        state.mkdir(mode=0o700)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        self.certificates = certificates
        self.endpoint = f"{'https' if certificates else 'http'}://127.0.0.1:{port}"
        self._binary, self._state, self._port = binary, state, port
        self._start()
        owned.append(self)
        self._wait_ready()

    def _start(self) -> None:
        with (self._state.parent / f"{self._state.name}.log").open("a") as log:
            self.process = subprocess.Popen(
                [
                    str(self._binary),
                    "server",
                    str(self._state),
                    f"--address=127.0.0.1:{self._port}",
                    *(["--certs-dir", str(self.certificates)] if self.certificates else []),
                ],
                env=self._environment,
                stdout=log,
                stderr=subprocess.STDOUT,
            )

    def restart(self) -> None:
        if self.process.poll() is None:
            raise RuntimeError("stop the owned server before restarting it")
        self._start()
        self._wait_ready()

    def _wait_ready(self) -> None:
        deadline = time.monotonic() + 30
        while True:
            if self.process.poll() is not None:
                raise RuntimeError("synthetic object store exited before readiness")
            try:
                with urllib.request.urlopen(
                    self.endpoint + "/minio/health/live",
                    timeout=1,
                    context=ssl.create_default_context(cafile=str(self.certificates / "ca.crt"))
                    if self.certificates
                    else None,
                ) as result:
                    if result.status == 200:
                        break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("synthetic object store did not become ready") from None
                time.sleep(0.1)

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)
        assert self.process.poll() is not None

    def client(self, *, wrong_identity: bool = False) -> QualificationClient:
        sdk = importlib.import_module("boto3")
        config = importlib.import_module("botocore.config")
        return cast(
            QualificationClient,
            sdk.client(
                "s3",
                endpoint_url=self.endpoint,
                region_name="us-east-1",
                verify=str(self.certificates / "ca.crt") if self.certificates else True,
                aws_access_key_id=self.access,
                aws_secret_access_key="invalid-synthetic-identity"
                if wrong_identity
                else self.secret,
                config=config.Config(
                    signature_version="s3v4",
                    connect_timeout=3,
                    read_timeout=5,
                    retries={"total_max_attempts": 1},
                    s3={"addressing_style": "path"},
                ),
            ),
        )


def signer(output: Path, version: str) -> LocalEd25519CheckpointSigner:
    key = Ed25519PrivateKey.generate()
    path = output / f"synthetic-signing-key-{version}.pem"
    with path.open("xb") as destination:
        path.chmod(0o600)
        destination.write(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
    return LocalEd25519CheckpointSigner(
        private_key=path, signer_id="synthetic-records", key_id="qualification", key_version=version
    )


def candidate(
    scope: CheckpointAuthorityScope,
    previous: RetainedCheckpoint | None = None,
    *,
    publication_id: str,
) -> tuple[AuditCheckpointStatement, str]:
    events, _ = verify_audit_jsonl(previous.audit_content if previous else "")
    now = datetime.now(UTC).isoformat()
    action = prepare_audit_event(
        session_id=scope.session_id,
        sequence=len(events),
        previous_event_hash=events[-1].event_hash if events else None,
        event_type="approval.recorded",
        occurred_at=now,
        payload={"decision": "approved", "actor_id": "synthetic-reviewer"},
    )
    sequence = (
        checkpoint_context(previous.checkpoint.statement, previous.audit_content).sequence + 1
        if previous
        else 1
    )
    binding = AuditCheckpointContext(
        project_id=scope.project_id,
        sequence=sequence,
        publication_id=publication_id,
        previous_checkpoint_sha256=previous.digest if previous else None,
    )
    exported = prepare_audit_event(
        session_id=scope.session_id,
        sequence=action.sequence + 1,
        previous_event_hash=action.event_hash,
        event_type="audit.export.recorded",
        occurred_at=now,
        payload={"checkpoint_context": cast(dict[str, JsonValue], binding.model_dump(mode="json"))},
    )
    content = canonical_audit_jsonl((*events, action, exported))
    _, verified = verify_audit_jsonl(content)
    return AuditCheckpointStatement(
        deployment_id=scope.deployment_id,
        session_id=scope.session_id,
        created_at=now,
        audit_event_count=verified.event_count,
        terminal_event_hash=verified.terminal_event_hash,
        audit_content_sha256=verified.content_sha256,
        audit_size_bytes=verified.size_bytes,
        retention=scope.retention,
    ), content


def qualify_session_gate(
    server: ObjectStore,
    bucket: str,
    scope: CheckpointAuthorityScope,
    active_signer: LocalEd25519CheckpointSigner,
    trusted: Mapping[str, CheckpointPublicKey],
    output: Path,
) -> None:
    """Stop the real records service between a proposal and its reviewed decision."""
    session_id = "evidence-session"
    records = S3CheckpointRecords(
        client=server.client(),
        bucket=bucket,
        prefix="sessions",
        scope=CheckpointAuthorityScope(
            scope.deployment_id, scope.project_id, session_id, scope.retention
        ),
    )
    authority = AnchoredCheckpointSigner(
        records=records, signer=active_signer, trusted_keys=trusted
    )
    evidence = SessionCheckpointEvidence(
        records=records, signer=authority, trusted_keys=trusted, mode="required"
    )
    workspace = output / "session-project"
    service = SessionService.local_default(
        workspace, session_id=session_id, env={}, checkpoint_evidence=evidence
    )
    service.handle(
        SessionCommand(
            command_id="chat-before-outage",
            session_id=session_id,
            kind=CommandKind.CHAT,
            created_at=datetime.now(UTC).isoformat(),
            payload={"prompt": "Inspect synthetic data"},
        )
    )
    pending = service.backend.pending_action_group(session_id=session_id)
    assert pending is not None
    command = SessionCommand(
        command_id="decision-during-outage",
        session_id=session_id,
        kind=CommandKind.APPROVE,
        created_at=datetime.now(UTC).isoformat(),
        payload={"target_type": "action-set", "target_id": pending.group_id},
    )
    server.stop()
    with pytest.raises(CheckpointEvidenceUnavailableError):
        service.handle(command)
    with pytest.raises(CheckpointEvidenceUnavailableError):
        service.reconcile()
    assert service.backend.pending_action_group(session_id=session_id) == pending
    assert not any(
        event.kind == EventKind.TOOL_EXECUTION_RECORDED for event in service.replay_events()
    )
    with pytest.raises(CheckpointEvidenceUnavailableError):
        service.close()
    server.restart()
    restored = SessionService.local_default(
        workspace, session_id=session_id, env={}, checkpoint_evidence=evidence
    )
    restored.reconcile()
    execution = [
        event
        for event in restored.replay_events()
        if event.kind == EventKind.TOOL_EXECUTION_RECORDED
    ]
    assert len(execution) == 1
    assert restored.handle(command).replayed
    assert (
        len(
            [
                event
                for event in restored.replay_events()
                if event.kind == EventKind.TOOL_EXECUTION_RECORDED
            ]
        )
        == 1
    )
    retained = records.history()
    assert retained
    decision = next(
        event for event in restored.replay_events() if event.kind == EventKind.APPROVAL_RECORDED
    )
    assert retained[0].checkpoint.statement.audit_event_count > decision.sequence
    assert retained[0].checkpoint.statement.audit_event_count <= execution[0].sequence
    restored.close()
    assert len(records.history()) == 2


class LostReplyClient:
    def __init__(self, client: S3RecordsClient) -> None:
        self.client = client
        self.lost = 0

    def list_object_versions(self, **kwargs: object) -> Mapping[str, Any]:
        return self.client.list_object_versions(**kwargs)

    def get_object(self, **kwargs: object) -> Mapping[str, Any]:
        return self.client.get_object(**kwargs)

    def put_object(self, **kwargs: object) -> Mapping[str, Any]:
        result = self.client.put_object(**kwargs)
        if "/checkpoints/" in str(kwargs["Key"]):
            self.lost += 1
            raise OSError("synthetic response loss after real server acceptance")
        return result


class ConcurrentSigner:
    def __init__(self, delegate: LocalEd25519CheckpointSigner, barrier: threading.Barrier) -> None:
        self.delegate, self.barrier = delegate, barrier

    def sign(self, statement: AuditCheckpointStatement) -> AuditCheckpointSignature:
        self.barrier.wait(timeout=15)
        return self.delegate.sign(statement)


def run(binary: Path, image: str, output: Path) -> None:
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    owned: list[ObjectStore] = []
    checks: list[str] = []
    report: dict[str, object] = {"image": image, "checks": checks, "passed": False}
    try:
        server = ObjectStore(binary, output / "server-original", owned)
        client = server.client()
        bucket = "synthetic-records-" + uuid4().hex
        client.create_bucket(Bucket=bucket, ObjectLockEnabledForBucket=True)
        scope = CheckpointAuthorityScope(
            "synthetic-deployment",
            uuid4(),
            "session-1",
            AuditRetention(
                policy_id="synthetic-retention",
                retain_until=(datetime.now(UTC) + timedelta(days=1)).date().isoformat(),
            ),
        )
        records = S3CheckpointRecords(client=client, bucket=bucket, prefix="audit", scope=scope)
        first_signer = signer(output, "1")
        trusted: dict[str, CheckpointPublicKey] = {
            checkpoint_public_key_fingerprint(first_signer.public_key): first_signer.public_key
        }
        authority = AnchoredCheckpointSigner(
            records=records, signer=first_signer, trusted_keys=trusted
        )
        statement, content = candidate(scope, publication_id="first")
        records.stage_audit(statement, content)
        first_signature = authority.sign(statement)
        assert authority.sign(statement) == first_signature
        first = records.history()[0]
        checks.append("independent publication and exact retry")

        proposed, original_content = candidate(scope, first, publication_id="rewritten-history")
        previous_events, _ = verify_audit_jsonl(original_content)
        rewritten: list[AuditEvent] = []
        for event in previous_events:
            payload = dict(event.payload)
            if event.event_type == "approval.recorded":
                payload["actor_id"] = "forged-reviewer"
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
        rewritten_content = canonical_audit_jsonl(tuple(rewritten))
        _, rewritten_verification = verify_audit_jsonl(rewritten_content)
        proposed = proposed.model_copy(
            update={
                "audit_content_sha256": rewritten_verification.content_sha256,
                "terminal_event_hash": rewritten_verification.terminal_event_hash,
                "audit_size_bytes": rewritten_verification.size_bytes,
            }
        )
        records.stage_audit(proposed, rewritten_content)
        with pytest.raises(CheckpointSignerError):
            authority.sign(proposed)
        assert records.history() == (first,)
        checks.append(
            "validly rehashed history cannot replace independently retained authorization"
        )

        invalid = server.client(wrong_identity=True)
        with pytest.raises(Exception, match="SignatureDoesNotMatch"):
            invalid.list_object_versions(Bucket=bucket, Prefix="audit/")
        checks.append("unauthorized storage access rejected with authorized positive control")

        versions = client.list_object_versions(Bucket=bucket)["Versions"]
        version = next(item for item in versions if "/checkpoints/" in item["Key"])
        with pytest.raises(Exception, match=r"AccessDenied|WORM protected"):
            client.delete_object(Bucket=bucket, Key=version["Key"], VersionId=version["VersionId"])
        with pytest.raises(Exception, match=r"AccessDenied|WORM protected"):
            client.put_object_retention(
                Bucket=bucket,
                Key=version["Key"],
                VersionId=version["VersionId"],
                Retention={
                    "Mode": "COMPLIANCE",
                    "RetainUntilDate": datetime.now(UTC) + timedelta(hours=1),
                },
            )
        assert records.history() == (first,)
        checks.append("server-enforced compliance deletion and retention-shortening denial")

        rotated = signer(output, "2")
        trusted[checkpoint_public_key_fingerprint(rotated.public_key)] = rotated.public_key
        fault = LostReplyClient(client)
        fault_records = S3CheckpointRecords(
            client=fault, bucket=bucket, prefix="audit", scope=scope
        )
        rotated_authority = AnchoredCheckpointSigner(
            records=fault_records, signer=rotated, trusted_keys=trusted
        )
        statement, content = candidate(scope, first, publication_id="second")
        records.stage_audit(statement, content)
        signature = rotated_authority.sign(statement)
        assert fault.lost == 1
        assert rotated_authority.sign(statement) == signature
        head = records.history()[-1]
        assert head.checkpoint.signature.key_version == "2"
        checks.append("key rotation and reconciliation after real publication response loss")

        race_scope = CheckpointAuthorityScope(
            scope.deployment_id, scope.project_id, "race", scope.retention
        )
        race_records = S3CheckpointRecords(
            client=client, bucket=bucket, prefix="race", scope=race_scope
        )
        proposals = [candidate(race_scope, publication_id=name) for name in ("left", "right")]
        for proposed, text in proposals:
            race_records.stage_audit(proposed, text)
        barrier = threading.Barrier(2)
        racing = AnchoredCheckpointSigner(
            records=race_records, signer=ConcurrentSigner(rotated, barrier), trusted_keys=trusted
        )
        outcomes = []
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(racing.sign, proposed) for proposed, _ in proposals]
            for future in futures:
                try:
                    future.result(timeout=20)
                    outcomes.append("retained")
                except CheckpointSignerError:
                    outcomes.append("rejected")
        assert sorted(outcomes) == ["rejected", "retained"]
        assert len(race_records.history()) == 1
        checks.append("concurrent publication preserves exactly one ordinal")

        qualify_session_gate(server, bucket, scope, rotated, trusted, output)
        checks.append(
            "records outage blocks dispatch and recovery; "
            "retained evidence precedes resumed execution"
        )

        backup = []
        for version in client.list_object_versions(Bucket=bucket, Prefix="audit/")["Versions"]:
            response = client.get_object(
                Bucket=bucket, Key=version["Key"], VersionId=version["VersionId"]
            )
            with response["Body"] as body:
                backup.append(
                    {"key": version["Key"], "content": base64.b64encode(body.read()).decode()}
                )
        (output / "backup.json").write_text(json.dumps(backup, indent=2) + "\n")
        (output / "receipt.json").write_text(json.dumps({"checkpoint_sha256": head.digest}) + "\n")
        assert records.history()[-1].digest == head.digest

        server.stop()
        replacement = ObjectStore(binary, output / "server-restored", owned)
        restored_client = replacement.client()
        restored_client.create_bucket(Bucket=bucket, ObjectLockEnabledForBucket=True)
        for item in json.loads((output / "backup.json").read_text()):
            restored_client.put_object(
                Bucket=bucket,
                Key=item["key"],
                Body=base64.b64decode(item["content"]),
                ChecksumAlgorithm="SHA256",
                IfNoneMatch="*",
                ObjectLockMode="COMPLIANCE",
                ObjectLockRetainUntilDate=scope.retention_deadline,
            )
        restored = S3CheckpointRecords(
            client=restored_client, bucket=bucket, prefix="audit", scope=scope
        )
        restored_authority = AnchoredCheckpointSigner(
            records=restored, signer=rotated, trusted_keys=trusted
        )
        assert (
            restored.history()[-1].digest
            == json.loads((output / "receipt.json").read_text())["checkpoint_sha256"]
        )
        assert restored_authority.sign(statement) == signature
        next_statement, next_content = candidate(scope, head, publication_id="after-restore")
        restored.stage_audit(next_statement, next_content)
        restored_authority.sign(next_statement)
        assert len(restored.history()) == 3
        checks.append(
            "independent receipt and full restore into fresh server with continued history"
        )

        checkpoint_version = next(
            item
            for item in restored_client.list_object_versions(Bucket=bucket)["Versions"]
            if "/checkpoints/" in item["Key"]
        )
        marker = restored_client.delete_object(Bucket=bucket, Key=checkpoint_version["Key"])
        with pytest.raises(CheckpointSignerError):
            restored_authority.sign(next_statement)
        checks.append("real delete marker cannot hide retained history from the authority")
        restored_client.delete_object(
            Bucket=bucket, Key=checkpoint_version["Key"], VersionId=marker["VersionId"]
        )
        restored_authority.sign(next_statement)
        candidate_version = next(
            item
            for item in restored_client.list_object_versions(Bucket=bucket)["Versions"]
            if "/candidates/" in item["Key"]
            and first.checkpoint.statement.audit_content_sha256[7:] in item["Key"]
        )
        restored_client.put_object(
            Bucket=bucket,
            Key=candidate_version["Key"],
            Body=first.audit_content.encode(),
            ChecksumAlgorithm="SHA256",
            ObjectLockMode="COMPLIANCE",
            ObjectLockRetainUntilDate=scope.retention_deadline,
        )
        with pytest.raises(CheckpointSignerError):
            restored_authority.sign(next_statement)
        checks.append("unconditional new object version is detected even when bytes are identical")
        report["passed"] = True
    finally:
        for server in owned:
            server.stop()
        report["cleanup"] = {"server_processes_stopped": [server.process.pid for server in owned]}
        report["limitations"] = [
            "Synthetic object-store implementation, not institutional S3 qualification",
            "Synthetic signing key, not a KMS/HSM deployment",
            "No native worker or independently deployed gateway/signing-service integration",
            "No worker IAM-policy qualification or infrastructure telemetry integration",
        ]
        (output / "result.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-binary", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run(args.server_binary, args.image, args.output)
