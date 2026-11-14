# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Synthetic HTTPS records/signing qualification with distinct storage identities."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import ssl
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import tomli_w
from cryptography.hazmat.primitives import serialization

from deploy.tests.checkpoint_records_integration import ObjectStore, QualificationClient, signer
from heartwood.audit import (
    AuditCheckpointError,
    CheckpointSignerError,
    LocalEd25519CheckpointSigner,
    RemoteCheckpointSigner,
)
from heartwood.audit._anchored_signer import AnchoredCheckpointSigner
from heartwood.audit._records_s3 import CheckpointAuthorityScope, S3CheckpointRecords
from heartwood.audit._signer import checkpoint_public_key_fingerprint, load_checkpoint_public_key
from heartwood.core_adapter.checkpoint_evidence import CheckpointEvidenceUnavailableError
from heartwood.gateway import ProjectContext, SessionGateway
from heartwood.gateway.checkpoint_deployment import (
    CheckpointEvidenceDeployment,
    _sdk_client,
    deployment_checkpoint_evidence,
)
from heartwood.schemas import AuditCheckpointSignRequest
from heartwood.session import CommandKind, EventKind, SessionCommand
from packages.audit.tests.tls_fixture import server_certificates


def write_private(path: Path, content: str) -> None:
    with path.open("w") as stream:
        path.chmod(0o600)
        stream.write(content)


def project_from(record: dict[str, Any]) -> ProjectContext:
    return ProjectContext(
        Path(record["workspace"]),
        protected_state=Path(record["state_root"]),
        project_id=UUID(record["project_id"]),
    )


def serve_authority(path: Path) -> None:
    config = json.loads(path.read_text())
    settings = CheckpointEvidenceDeployment.model_validate(config["settings"])
    project = project_from(config)
    client = _sdk_client(settings, project)
    key = load_checkpoint_public_key(Path(config["public_key"]))
    scope = CheckpointAuthorityScope(
        settings.deployment_id, UUID(config["project_id"]), config["session_id"], settings.retention
    )
    authority = AnchoredCheckpointSigner(
        records=S3CheckpointRecords(
            client=client, bucket=settings.bucket, prefix=settings.prefix, scope=scope
        ),
        signer=LocalEd25519CheckpointSigner(
            private_key=Path(config["private_key"]),
            signer_id="synthetic-records",
            key_id="qualification",
            key_version="tls",
        ),
        trusted_keys={checkpoint_public_key_fingerprint(key): key},
    )
    token = Path(config["token_file"]).read_text()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass

        def do_POST(self) -> None:
            self.connection.settimeout(5)
            if (
                self.path != "/v1/checkpoints/sign"
                or self.headers.get("Authorization") != "Bearer " + token
            ):
                self.send_error(403)
                return
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 64 * 1024:
                self.send_error(413)
                return
            try:
                request = AuditCheckpointSignRequest.model_validate_json(self.rfile.read(length))
                response = authority.sign(request.statement).model_dump_json().encode()
            except Exception:
                self.send_error(403)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    tls = Path(config["tls"])
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(tls / "public.crt", tls / "private.key")
    server.socket = context.wrap_socket(server.socket, server_side=True)
    write_private(
        Path(config["ready"]),
        json.dumps({"endpoint": f"https://127.0.0.1:{server.server_port}/v1/checkpoints/sign"}),
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()
        client.close()


def denied(operation: Any, **kwargs: object) -> None:  # noqa: ANN401
    with pytest.raises(Exception, match="AccessDenied") as failure:
        operation(**kwargs)
    response = getattr(failure.value, "response", {})
    assert response.get("Error", {}).get("Code") == "AccessDenied", str(failure.value)


@dataclass(frozen=True)
class RecordsTransport:
    """Synthetic outside authorities shared by transport and full-worker checks."""

    server: ObjectStore
    settings: CheckpointEvidenceDeployment
    project: ProjectContext
    root: QualificationClient
    credentials: dict[str, Path]
    endpoint: str
    ca: Path
    token: Path
    forbidden_values: tuple[str, ...]
    report: dict[str, Any]


@contextmanager
def records_transport(
    binary: Path,
    admin: Path,
    image: str,
    output: Path,
    *,
    project: ProjectContext | None = None,
    session_id: str = "synthetic-session",
) -> Iterator[RecordsTransport]:
    """Own one outside records deployment and independent signing process."""
    output.mkdir(mode=0o700)
    owned: list[ObjectStore] = []
    authority_process: subprocess.Popen[bytes] | None = None
    report: dict[str, Any] = {"image": image, "checks": [], "passed": False}
    checks = report["checks"]
    try:
        ca = server_certificates(output / "tls")
        server = ObjectStore(binary, output / "store", owned, certificates=ca.parent)
        root = server.client()
        bucket, prefix = "synthetic-evidence", "audit"
        root.create_bucket(Bucket=bucket, ObjectLockEnabledForBucket=True)
        conditional_policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Deny",
                    "Principal": "*",
                    "Action": "s3:PutObject",
                    "Resource": f"arn:aws:s3:::{bucket}/{prefix}/*",
                    "Condition": {"Null": {"s3:if-none-match": "true"}},
                }
            ],
        }
        (output / "conditional-bucket-policy.json").write_text(json.dumps(conditional_policy))
        conditional_enforcement = True
        try:
            root.put_bucket_policy(Bucket=bucket, Policy=json.dumps(conditional_policy))
        except Exception as error:
            response = getattr(error, "response", {}).get("Error", {})
            if "invalid condition key 's3:if-none-match'" not in response.get("Message", ""):
                raise
            conditional_enforcement = False
            report["conditional_policy_rejection"] = response
        report["conditional_write_enforcement"] = conditional_enforcement
        report["records_policy_qualified"] = False
        admin_home = output / "admin"
        admin_home.mkdir(mode=0o700)
        trust = admin_home / "certs/CAs"
        trust.mkdir(parents=True, mode=0o700)
        (trust / "synthetic.crt").write_bytes(ca.read_bytes())

        def mc(*args: str) -> None:
            result = subprocess.run(
                [str(admin), "--config-dir", str(admin_home), "--json", *args],
                env={**os.environ, "SSL_CERT_FILE": str(ca)},
                capture_output=True,
                timeout=30,
            )
            if result.returncode:
                raise RuntimeError(
                    "synthetic records administration failed: "
                    + result.stdout.decode()[-1200:]
                    + result.stderr.decode()[-1200:]
                )

        mc("alias", "set", "synthetic", server.endpoint, server.access, server.secret)
        identities = {
            name: ("synthetic-" + name, secrets.token_hex(24))
            for name in ("gateway", "signer", "worker")
        }
        for name, (access, secret) in identities.items():
            mc("admin", "user", "add", "synthetic", access, secret)
            statements: list[dict[str, object]]
            if name == "worker":
                statements = [
                    {
                        "Effect": "Allow",
                        "Action": ["s3:GetObjectVersion"],
                        "Resource": [f"arn:aws:s3:::{bucket}/worker-control/*"],
                    }
                ]
            else:
                writes = ["candidates", "experiments"] if name == "gateway" else ["checkpoints"]
                statements = [
                    {
                        "Effect": "Allow",
                        "Action": ["s3:ListBucketVersions"],
                        "Resource": [f"arn:aws:s3:::{bucket}"],
                        "Condition": {"StringLike": {"s3:prefix": [prefix + "/*"]}},
                    },
                    {
                        "Effect": "Allow",
                        "Action": ["s3:GetObjectVersion", "s3:GetObjectRetention"],
                        "Resource": [f"arn:aws:s3:::{bucket}/{prefix}/*"],
                    },
                    {
                        "Effect": "Allow",
                        "Action": ["s3:PutObject", "s3:PutObjectRetention"],
                        "Resource": [
                            f"arn:aws:s3:::{bucket}/{prefix}/*/{area}/*" for area in writes
                        ],
                    },
                ]
            policy = output / f"{name}-policy.json"
            policy.write_text(json.dumps({"Version": "2012-10-17", "Statement": statements}))
            mc("admin", "policy", "create", "synthetic", name, str(policy))
            mc("admin", "policy", "attach", "synthetic", name, "--user", access)
        checks.append(
            "distinct gateway, signer and worker identities have server-enforced policies"
        )

        if project is None:
            workspace = output / "research"
            workspace.mkdir()
            state = output / "state"
            state.mkdir(mode=0o700)
            identity = uuid4()
            project = ProjectContext(
                workspace, protected_state=state / str(identity), project_id=identity
            )
        else:
            assert project.project_id is not None
            assert project.protected_state is not None
            workspace, identity = project.root, project.project_id
        signing = signer(output, "tls")
        public = output / "signer.pub"
        public.write_bytes(
            signing.public_key.public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            )
        )
        public.chmod(0o600)
        token = output / "signer-token"
        write_private(token, secrets.token_hex(32))
        registry = output / "signers.toml"
        credentials = {}
        for name, (access, secret) in identities.items():
            credentials[name] = output / f"{name}-credentials"
            write_private(
                credentials[name],
                f"[records]\naws_access_key_id = {access}\naws_secret_access_key = {secret}\n",
            )
        common = {
            "mode": "required",
            "deployment_id": "synthetic-deployment",
            "endpoint": server.endpoint,
            "region": "us-east-1",
            "bucket": bucket,
            "prefix": prefix,
            "retention": {
                "policy_id": "synthetic",
                "retain_until": (datetime.now(UTC) + timedelta(days=7)).date().isoformat(),
            },
            "signer_registry": str(registry),
            "signer_profile": "records",
            "ca_bundle": str(ca),
            "credentials_profile": "records",
            "credentials_file": str(credentials["gateway"]),
        }
        ready = output / "signer-ready.json"
        authority_config = output / "authority.json"
        write_private(
            authority_config,
            json.dumps(
                {
                    "settings": {**common, "credentials_file": str(credentials["signer"])},
                    "workspace": str(workspace),
                    "state_root": str(project.state_root),
                    "project_id": str(identity),
                    "session_id": session_id,
                    "public_key": str(public),
                    "private_key": str(output / "synthetic-signing-key-tls.pem"),
                    "token_file": str(token),
                    "tls": str(ca.parent),
                    "ready": str(ready),
                }
            ),
        )
        with (output / "authority.log").open("w") as log:
            authority_process = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--serve-authority",
                    str(authority_config),
                ],
                stdout=log,
                stderr=subprocess.STDOUT,
                env={
                    **os.environ,
                    "PYTHONPATH": os.pathsep.join(
                        [str(Path(__file__).resolve().parents[2]), os.environ.get("PYTHONPATH", "")]
                    ),
                },
            )
        deadline = time.monotonic() + 30
        while not ready.exists():
            if authority_process.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError("synthetic signing authority did not start")
            time.sleep(0.1)
        endpoint = json.loads(ready.read_text())["endpoint"]
        write_private(
            registry,
            tomli_w.dumps(
                {
                    "schema_version": "heartwood.checkpoint-signer-registry.v1",
                    "default_profile": "records",
                    "profiles": {
                        "records": {
                            "mode": "production",
                            "endpoint": endpoint,
                            "signer_id": "synthetic-records",
                            "key_id": "qualification",
                            "key_version": "tls",
                            "algorithm": "ed25519",
                            "public_key_sha256": checkpoint_public_key_fingerprint(
                                signing.public_key
                            ),
                            "trusted_public_key": str(public),
                            "trusted_ca_bundle": str(ca),
                            "authorization_token_file": str(token),
                        }
                    },
                }
            ),
        )
        settings = CheckpointEvidenceDeployment.model_validate(common)
        yield RecordsTransport(
            server=server,
            settings=settings,
            project=project,
            root=root,
            credentials=credentials,
            endpoint=endpoint,
            ca=ca,
            token=token,
            forbidden_values=(
                server.secret,
                token.read_text(),
                *(secret for _, secret in identities.values()),
            ),
            report=report,
        )
    finally:
        if authority_process is not None:
            authority_process.terminate()
            try:
                authority_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                authority_process.kill()
                authority_process.wait(timeout=10)
        for server in owned:
            server.stop()
        report["cleanup"] = {
            "authority_stopped": authority_process is None or authority_process.poll() is not None,
            "records_stopped": all(server.process.poll() is not None for server in owned),
        }
        report["limitations"] = [
            "Synthetic identities, TLS authority and signing key; "
            "not institutional IAM/KMS qualification",
        ]
        if report.get("conditional_write_enforcement") is False:
            report["limitations"].append(
                "Records server rejects conditional-write policy; storage policy qualification "
                "is incomplete despite fail-closed duplicate-version detection"
            )
        (output / "transport-result.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))


def run(binary: Path, admin: Path, image: str, output: Path) -> None:
    """Qualify storage authorization, retained evidence and failure recovery."""
    with records_transport(binary, admin, image, output) as transport:
        server, project, root = transport.server, transport.project, transport.root
        settings, credentials = transport.settings, transport.credentials
        endpoint, ca, token = transport.endpoint, transport.ca, transport.token
        report = transport.report
        checks = report["checks"]
        conditional_enforcement = report["conditional_write_enforcement"]
        bucket, prefix = settings.bucket, settings.prefix
        common = settings.model_dump(mode="json", exclude_none=True)
        with deployment_checkpoint_evidence(settings, project) as factory:
            gateway = SessionGateway(
                project=project,
                env={},
                backend_id="deterministic",
                checkpoint_evidence_factory=factory,
            )
            gateway.config_store.save(gateway.config_store.load())
            session_id = "synthetic-session"
            gateway.handle(
                SessionCommand(
                    command_id="chat",
                    session_id=session_id,
                    kind=CommandKind.CHAT,
                    created_at=datetime.now(UTC).isoformat(),
                    payload={"prompt": "Inspect synthetic data"},
                )
            )
            service = gateway._service(session_id)
            pending = service.backend.pending_action_group(session_id=session_id)
            assert pending is not None
            command = SessionCommand(
                command_id="approve",
                session_id=session_id,
                kind=CommandKind.APPROVE,
                created_at=datetime.now(UTC).isoformat(),
                payload={"target_type": "action-set", "target_id": pending.group_id},
            )
            server.stop()
            with pytest.raises(CheckpointEvidenceUnavailableError):
                gateway.handle(command)
            assert not any(
                event.kind == EventKind.TOOL_EXECUTION_RECORDED for event in service.replay_events()
            )
            server.restart()
            gateway.handle(command)
            assert gateway.handle(command).replayed
            assert (
                len(
                    [
                        event
                        for event in service.replay_events()
                        if event.kind == EventKind.TOOL_EXECUTION_RECORDED
                    ]
                )
                == 1
            )
            records = factory(session_id).records
            retained = records.history()
            assert len(retained) == 1
            checks.append(
                "configured gateway resumes exactly once after a real TLS records outage "
                "and external signer publication"
            )
            with pytest.raises(CheckpointSignerError):
                RemoteCheckpointSigner(
                    endpoint=endpoint,
                    trusted_ca_bundle=ca,
                    authorization_token="wrong-synthetic-token",
                ).sign(retained[0].checkpoint.statement)
            with pytest.raises(CheckpointSignerError):
                RemoteCheckpointSigner(
                    endpoint=endpoint, trusted_ca_bundle=ca, authorization_token=token.read_text()
                ).sign(
                    retained[0].checkpoint.statement.model_copy(
                        update={"deployment_id": "different-deployment"}
                    )
                )
            checks.append(
                "independent HTTPS signer rejects wrong identity and out-of-scope statements"
            )
            clients = {
                name: _sdk_client(
                    CheckpointEvidenceDeployment.model_validate(
                        {**common, "credentials_file": str(path)}
                    ),
                    project,
                )
                for name, path in credentials.items()
            }
            try:
                versions = root.list_object_versions(Bucket=bucket, Prefix=prefix + "/")["Versions"]
                candidate = next(item for item in versions if "/candidates/" in item["Key"])
                namespace = candidate["Key"].split("/candidates/")[0]
                common_write = {
                    "Bucket": bucket,
                    "Body": b"synthetic",
                    "IfNoneMatch": "*",
                    "ObjectLockMode": "COMPLIANCE",
                    "ObjectLockRetainUntilDate": records.scope.retention_deadline,
                    "ChecksumAlgorithm": "SHA256",
                }
                denied(
                    clients["gateway"].put_object,
                    **common_write,
                    Key=namespace + "/checkpoints/99999999999999999999.json",
                )
                denied(
                    clients["signer"].put_object,
                    **common_write,
                    Key=namespace + "/candidates/forbidden.jsonl",
                )
                if conditional_enforcement:
                    denied(
                        clients["gateway"].put_object,
                        **{k: v for k, v in common_write.items() if k != "IfNoneMatch"},
                        Key=namespace + "/candidates/unconditional.jsonl",
                    )
                control = root.put_object(
                    Bucket=bucket, Key="worker-control/probe", Body=b"synthetic positive control"
                )
                allowed = clients["worker"].get_object(
                    Bucket=bucket, Key="worker-control/probe", VersionId=control["VersionId"]
                )
                with allowed["Body"] as body:
                    assert body.read() == b"synthetic positive control"
                denied(
                    clients["worker"].list_object_versions, Bucket=bucket, Prefix=namespace + "/"
                )
                denied(
                    clients["worker"].get_object,
                    Bucket=bucket,
                    Key=candidate["Key"],
                    VersionId=candidate["VersionId"],
                )
                assert len(records.history()) == 1
                checks.append(
                    "gateway cannot publish checkpoints; signer cannot alter candidates; "
                    "worker ledger access is denied with a working identity positive control"
                )
            finally:
                for client in clients.values():
                    client.close()
            gateway.stop()
            assert len(records.history()) == 2
            checks.append(
                "gateway shutdown retains its final checkpoint before the SDK client closes"
            )
            if not conditional_enforcement:
                replacement_client = _sdk_client(settings, project)
                try:
                    replacement_client.put_object(
                        **{k: v for k, v in common_write.items() if k != "IfNoneMatch"},
                        Key=candidate["Key"],
                    )
                finally:
                    replacement_client.close()
                with pytest.raises(AuditCheckpointError, match="ambiguous object versions"):
                    records.history()
                with pytest.raises(CheckpointSignerError):
                    RemoteCheckpointSigner(
                        endpoint=endpoint,
                        trusted_ca_bundle=ca,
                        authorization_token=token.read_text(),
                    ).sign(retained[0].checkpoint.statement)
                checks.append(
                    "unsupported conditional-write policy is reported; an actual replacement "
                    "version fails independent history verification and HTTPS signing"
                )
            report["records_policy_qualified"] = conditional_enforcement
        report["passed"] = True
        report["integration_scope"] = "deterministic backend; no native worker or telemetry"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve-authority", type=Path)
    parser.add_argument("--server-binary", type=Path)
    parser.add_argument("--admin-binary", type=Path)
    parser.add_argument("--image")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.serve_authority:
        serve_authority(args.serve_authority)
    else:
        run(args.server_binary, args.admin_binary, args.image, args.output)
