# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Qualify checkpoint records against a task-owned S3 server process.

Run inside a network-disabled qualification container with the pinned native
``versitygw`` binary. Only synthetic credentials and records are used. No existing
bucket, service, credential profile or external endpoint is accepted by this harness.
"""

from __future__ import annotations

import argparse
import base64
import importlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
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
from heartwood.audit._checkpoint import _canonical_checkpoint
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
    def list_buckets(self, **kwargs: object) -> Mapping[str, Any]: ...

    def close(self) -> None: ...

    def create_bucket(self, **kwargs: object) -> Mapping[str, Any]: ...

    def put_bucket_policy(self, **kwargs: object) -> Mapping[str, Any]: ...

    def delete_object(self, **kwargs: object) -> Mapping[str, Any]: ...

    def put_object_retention(self, **kwargs: object) -> Mapping[str, Any]: ...


class ObjectStore:
    """Own one versitygw S3 gateway and the standalone IAM service it authorizes against."""

    def __init__(
        self,
        binary: Path,
        state: Path,
        owned: list[ObjectStore],
        *,
        certificates: Path | None = None,
        listen_host: str = "127.0.0.1",
    ) -> None:
        self.access = "synthetic-" + secrets.token_hex(8)
        self.secret = secrets.token_hex(32)
        state.mkdir(mode=0o700)
        for area in ("data", "versions", "metadata", "iam"):
            (state / area).mkdir(mode=0o700)
        # Only the S3 endpoint listens on `listen_host`; administration stays on loopback.
        ports = []
        for host in (listen_host, "127.0.0.1"):
            with socket.socket() as listener:
                listener.bind((host, 0))
                ports.append(listener.getsockname()[1])
        self.certificates = certificates
        scheme = "https" if certificates else "http"
        # Clients use the loopback address, which every listen address includes.
        self.endpoint = f"{scheme}://127.0.0.1:{ports[0]}"
        self.iam_endpoint = f"{scheme}://127.0.0.1:{ports[1]}"
        self._binary, self._state, self._ports = binary, state, ports
        self._listen_host = listen_host
        self._processes: list[subprocess.Popen[bytes]] = []
        owned.append(self)
        self._start()

    def _command(self, port: int, *arguments: str, host: str = "127.0.0.1") -> list[str]:
        tls = self.certificates
        return [
            str(self._binary),
            f"--port={host}:{port}",
            "--quiet",
            *(
                ["--cert", str(tls / "public.crt"), "--key", str(tls / "private.key")]
                if tls
                else []
            ),
            *arguments,
        ]

    def _start(self) -> None:
        # The private endpoint carries signing keys and policy decisions; a
        # task-owned unix socket keeps it off the network.
        self._private = Path(tempfile.mkdtemp(prefix="hw-iam-"))
        socket_path = str(self._private / "iam.sock")
        environment = {
            **os.environ,
            "ROOT_ACCESS_KEY_ID": self.access,
            "ROOT_SECRET_ACCESS_KEY": self.secret,
        }
        self._processes = []
        with (self._state.parent / f"{self._state.name}.log").open("a") as log:
            iam_process = subprocess.Popen(
                self._command(
                    self._ports[1],
                    "iam",
                    "--dir",
                    str(self._state / "iam"),
                    "--private-ports",
                    socket_path,
                ),
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            self._processes.append(iam_process)
            iam = self.iam()
            try:
                self._wait_ready(iam_process, iam.list_users)
            finally:
                iam.close()
            gateway = subprocess.Popen(
                self._command(
                    self._ports[0],
                    "--iam-standalone-endpoint",
                    socket_path,
                    "posix",
                    "--versioning-dir",
                    str(self._state / "versions"),
                    # Sidecar metadata does not depend on extended attributes in the
                    # host-mounted qualification output.
                    "--sidecar",
                    str(self._state / "metadata"),
                    str(self._state / "data"),
                    host=self._listen_host,
                ),
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            self._processes.append(gateway)
        client = self.client()
        try:
            self._wait_ready(gateway, client.list_buckets)
        finally:
            client.close()

    def restart(self) -> None:
        if not self.stopped:
            raise RuntimeError("stop the owned server before restarting it")
        self._start()

    @staticmethod
    def _wait_ready(process: subprocess.Popen[bytes], probe: Callable[[], object]) -> None:
        exceptions = importlib.import_module("botocore.exceptions")
        deadline = time.monotonic() + 30
        while True:
            if process.poll() is not None:
                raise RuntimeError("synthetic object store exited before readiness")
            try:
                probe()
                return
            except (
                exceptions.EndpointConnectionError,
                exceptions.ConnectTimeoutError,
                exceptions.ReadTimeoutError,
            ):
                pass
            if time.monotonic() >= deadline:
                raise TimeoutError("synthetic object store did not become ready") from None
            time.sleep(0.1)

    @property
    def stopped(self) -> bool:
        return all(process.poll() is not None for process in self._processes)

    @property
    def pids(self) -> list[int]:
        return [process.pid for process in self._processes]

    def stop(self) -> None:
        for process in reversed(self._processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
            assert process.poll() is not None
        shutil.rmtree(self._private, ignore_errors=True)

    def _sdk(self, service: str, endpoint: str, access: str, secret: str) -> Any:  # noqa: ANN401
        sdk = importlib.import_module("boto3")
        config = importlib.import_module("botocore.config")
        return sdk.client(
            service,
            endpoint_url=endpoint,
            region_name="us-east-1",
            verify=str(self.certificates / "ca.crt") if self.certificates else True,
            aws_access_key_id=access,
            aws_secret_access_key=secret,
            config=config.Config(
                signature_version="s3v4",
                connect_timeout=3,
                read_timeout=5,
                retries={"total_max_attempts": 1},
                s3={"addressing_style": "path"},
            ),
        )

    def iam(self) -> Any:  # noqa: ANN401
        """Administer synthetic identities through the store's own IAM API."""
        return self._sdk("iam", self.iam_endpoint, self.access, self.secret)

    def identity(self, name: str, statements: list[dict[str, object]]) -> tuple[str, str]:
        """Create an ordinary account whose only permissions are the given policy statements."""
        iam = self.iam()
        try:
            iam.create_user(UserName=name)
            iam.put_user_policy(
                UserName=name,
                PolicyName=name,
                PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": statements}),
            )
            key = iam.create_access_key(UserName=name)["AccessKey"]
        finally:
            iam.close()
        return key["AccessKeyId"], key["SecretAccessKey"]

    def client(
        self, *, wrong_identity: bool = False, identity: tuple[str, str] | None = None
    ) -> QualificationClient:
        access, secret = identity or (self.access, self.secret)
        return cast(
            QualificationClient,
            self._sdk(
                "s3",
                self.endpoint,
                access,
                "invalid-synthetic-identity" if wrong_identity else secret,
            ),
        )


PROBE_PREFIX = "conditional-write-probe/"


def conditional_write_policy(server: ObjectStore, bucket: str, report: dict[str, Any]) -> bool:
    """Deny unconditional writes in the bucket policy and observe whether the store enforces it.

    An accepted policy is not evidence: a dedicated ordinary account that may write
    only below the probe prefix must be refused an unconditional write and allowed a
    conditional one.
    """
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Deny",
                "Principal": "*",
                "Action": "s3:PutObject",
                "Resource": f"arn:aws:s3:::{bucket}/*",
                "Condition": {"Null": {"s3:if-none-match": "true"}},
            }
        ],
    }
    report["conditional_bucket_policy"] = policy
    report["conditional_write_enforcement"] = False
    root = server.client()
    try:
        root.put_bucket_policy(Bucket=bucket, Policy=json.dumps(policy))
    except Exception as error:
        report["conditional_policy_rejection"] = getattr(error, "response", {}).get("Error", {})
        return False
    finally:
        root.close()
    probe = server.client(
        identity=server.identity(
            "conditional-write-probe",
            [
                {
                    "Effect": "Allow",
                    "Action": ["s3:PutObject"],
                    "Resource": [f"arn:aws:s3:::{bucket}/{PROBE_PREFIX}*"],
                }
            ],
        )
    )
    try:
        key = PROBE_PREFIX + uuid4().hex
        try:
            probe.put_object(Bucket=bucket, Key=key, Body=b"unconditional")
        except Exception as error:
            if getattr(error, "response", {}).get("Error", {}).get("Code") != "AccessDenied":
                raise
            # The positive control shows the refusal comes from the condition alone.
            probe.put_object(Bucket=bucket, Key=key, Body=b"conditional", IfNoneMatch="*")
            report["conditional_write_enforcement"] = True
    finally:
        probe.close()
    return bool(report["conditional_write_enforcement"])


class RecordsPolicyNotEnforcedError(AssertionError):
    """The records server accepted writes that the create-only policy must deny."""


def require_create_only_enforcement(report: Mapping[str, Any]) -> None:
    """Fail qualification unless the store itself enforces create-only publication."""
    if report.get("conditional_write_enforcement") is not True:
        raise RecordsPolicyNotEnforcedError(
            "records server does not enforce the conditional-write policy; "
            "create-only publication is not qualified"
        )


def refused(operation: Any, code: str, **kwargs: object) -> None:  # noqa: ANN401
    """Require the store itself to refuse a write with the expected error code."""
    with pytest.raises(Exception, match=code) as failure:
        operation(**kwargs)
    response = getattr(failure.value, "response", {})
    assert response.get("Error", {}).get("Code") == code, str(failure.value)


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
    writer: tuple[str, str],
    bucket: str,
    scope: CheckpointAuthorityScope,
    active_signer: LocalEd25519CheckpointSigner,
    trusted: Mapping[str, CheckpointPublicKey],
    output: Path,
) -> None:
    """Stop the real records service between a proposal and its reviewed decision."""
    session_id = "evidence-session"
    records = S3CheckpointRecords(
        client=server.client(identity=writer),
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


def records_writer(server: ObjectStore, bucket: str) -> tuple[str, str]:
    """Create the ordinary account that publishes records; the root account bypasses policy."""
    return server.identity(
        "records-writer",
        [
            {
                "Effect": "Allow",
                "Action": ["s3:ListBucketVersions"],
                "Resource": [f"arn:aws:s3:::{bucket}"],
            },
            {
                "Effect": "Allow",
                "Action": [
                    "s3:GetObject",
                    "s3:GetObjectVersion",
                    "s3:GetObjectRetention",
                    "s3:PutObject",
                    "s3:PutObjectRetention",
                ],
                "Resource": [f"arn:aws:s3:::{bucket}/*"],
            },
        ],
    )


def run(binary: Path, image: str, output: Path) -> None:
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    owned: list[ObjectStore] = []
    checks: list[str] = []
    report: dict[str, Any] = {"image": image, "store": binary.name, "checks": checks}
    report["passed"] = False
    try:
        server = ObjectStore(binary, output / "server-original", owned)
        admin = server.client()
        bucket = "synthetic-records-" + uuid4().hex
        admin.create_bucket(Bucket=bucket, ObjectLockEnabledForBucket=True)
        enforced = conditional_write_policy(server, bucket, report)
        writer = records_writer(server, bucket)
        client = server.client(identity=writer)
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

        invalid = server.client(wrong_identity=True, identity=writer)
        with pytest.raises(Exception, match="SignatureDoesNotMatch"):
            invalid.list_object_versions(Bucket=bucket, Prefix="audit/")
        checks.append("unauthorized storage access rejected with authorized positive control")

        versions = client.list_object_versions(Bucket=bucket)["Versions"]
        version = next(item for item in versions if "/checkpoints/" in item["Key"])
        with pytest.raises(Exception, match=r"AccessDenied|WORM protected"):
            admin.delete_object(Bucket=bucket, Key=version["Key"], VersionId=version["VersionId"])
        with pytest.raises(Exception, match=r"AccessDenied|WORM protected"):
            admin.put_object_retention(
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

        published = {
            "Bucket": bucket,
            "Key": version["Key"],
            "Body": _canonical_checkpoint(first.checkpoint).encode(),
            "ChecksumAlgorithm": "SHA256",
            "ObjectLockMode": "COMPLIANCE",
            "ObjectLockRetainUntilDate": scope.retention_deadline,
        }
        refused(client.put_object, "PreconditionFailed", **published, IfNoneMatch="*")
        if enforced:
            refused(client.put_object, "AccessDenied", **published)
            checks.append("store refuses conditional and unconditional checkpoint overwrites")
        else:
            checks.append("store refuses a conditional checkpoint overwrite")
        assert records.history() == (first,)

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

        qualify_session_gate(server, writer, bucket, scope, rotated, trusted, output)
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
        restored_admin = replacement.client()
        restored_admin.create_bucket(Bucket=bucket, ObjectLockEnabledForBucket=True)
        assert conditional_write_policy(replacement, bucket, {}) == enforced
        restored_client = replacement.client(identity=records_writer(replacement, bucket))
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
        marker = restored_admin.delete_object(Bucket=bucket, Key=checkpoint_version["Key"])
        with pytest.raises(CheckpointSignerError):
            restored_authority.sign(next_statement)
        checks.append("real delete marker cannot hide retained history from the authority")
        restored_admin.delete_object(
            Bucket=bucket, Key=checkpoint_version["Key"], VersionId=marker["VersionId"]
        )
        restored_authority.sign(next_statement)
        candidate_version = next(
            item
            for item in restored_client.list_object_versions(Bucket=bucket)["Versions"]
            if "/candidates/" in item["Key"]
            and first.checkpoint.statement.audit_content_sha256[7:] in item["Key"]
        )
        replacement_write = {
            "Bucket": bucket,
            "Key": candidate_version["Key"],
            "Body": first.audit_content.encode(),
            "ChecksumAlgorithm": "SHA256",
            "ObjectLockMode": "COMPLIANCE",
            "ObjectLockRetainUntilDate": scope.retention_deadline,
        }
        if enforced:
            refused(restored_client.put_object, "AccessDenied", **replacement_write)
            restored_authority.sign(next_statement)
            checks.append("restored store refuses an unconditional candidate overwrite")
        # The root account is exempt from the bucket policy, as an owner may be.
        restored_admin.put_object(**replacement_write)
        with pytest.raises(CheckpointSignerError):
            restored_authority.sign(next_statement)
        checks.append(
            "a policy-exempt replacement version is detected even when bytes are identical"
        )
        report["records_policy_qualified"] = enforced
        require_create_only_enforcement(report)
        report["passed"] = True
    finally:
        for server in owned:
            server.stop()
        report["cleanup"] = {
            "server_processes_stopped": [pid for server in owned for pid in server.pids],
            "records_stopped": all(server.stopped for server in owned),
        }
        report["limitations"] = [
            *(
                []
                if report.get("conditional_write_enforcement")
                else [
                    "Records server does not enforce the conditional-write bucket policy; "
                    "create-only publication relies on client conditions and "
                    "duplicate-version detection"
                ]
            ),
            "The store's root account is exempt from the bucket policy and bound only by "
            "Object Lock; replacement versions it writes are detected, not refused",
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
