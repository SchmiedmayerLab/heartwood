# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Operator evidence bindings select SDK identity without ambient authority fallback."""

from __future__ import annotations

import socket
import subprocess
import sys
import tomllib
from contextlib import nullcontext
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
import tomli_w
import uvicorn
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from heartwood.audit import checkpoint_public_key_fingerprint
from heartwood.cli import main
from heartwood.gateway import (
    GatewayAsgiApp,
    ProjectContext,
    ProjectStateError,
    load_project_deployment,
)
from heartwood.gateway.checkpoint_deployment import (
    CheckpointEvidenceDeployment,
    _CredentialProcess,
    _sdk_client,
    deployment_checkpoint_evidence,
    validate_evidence_deployment,
)
from heartwood.gateway.worker_deployment import OpenShellWorkerDeployment
from heartwood.schemas import PolicyProfile
from packages.audit.tests.tls_fixture import server_certificates
from packages.gateway.tests import test_worker_native

worker_deployment = test_worker_native.deployment


def settings(root: Path) -> CheckpointEvidenceDeployment:
    key = Ed25519PrivateKey.generate().public_key()
    public = root / "signer.pub"
    public.write_bytes(
        key.public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    public.chmod(0o600)
    registry = root / "signers.toml"
    registry.write_text(
        tomli_w.dumps(
            {
                "schema_version": "heartwood.checkpoint-signer-registry.v1",
                "default_profile": "records",
                "profiles": {
                    "records": {
                        "mode": "production",
                        "endpoint": "https://signer.test/v1/checkpoints/sign",
                        "signer_id": "synthetic",
                        "key_id": "audit",
                        "key_version": "1",
                        "algorithm": "ed25519",
                        "public_key_sha256": checkpoint_public_key_fingerprint(key),
                        "trusted_public_key": str(public),
                    }
                },
            }
        )
    )
    registry.chmod(0o600)
    credentials = root / "credentials"
    credentials.write_text(
        "[records]\naws_access_key_id = synthetic-records\n"
        "aws_secret_access_key = synthetic-secret\naws_session_token = synthetic-token\n"
    )
    credentials.chmod(0o600)
    return CheckpointEvidenceDeployment.model_validate(
        {
            "mode": "required",
            "deployment_id": "synthetic-deployment",
            "endpoint": "https://records.test",
            "region": "us-east-1",
            "bucket": "synthetic-records",
            "prefix": "audit",
            "retention": {"policy_id": "synthetic-retention", "retain_until": "2033-01-01"},
            "signer_registry": registry,
            "signer_profile": "records",
            "credentials_file": credentials,
            "credentials_profile": "records",
        }
    )


@pytest.fixture
def binding(tmp_path: Path) -> tuple[CheckpointEvidenceDeployment, ProjectContext]:
    workspace = tmp_path / "research"
    workspace.mkdir()
    protected = tmp_path / "protected"
    protected.mkdir(mode=0o700)
    identity = uuid4()
    project = ProjectContext(
        workspace, protected_state=protected / str(identity), project_id=identity
    )
    return settings(tmp_path), project


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("endpoint", "http://records.test"),
        ("endpoint", "https://user:secret@records.test"),
        ("endpoint", "https://records.test/path"),
        ("endpoint", "https://records.test?query"),
        ("signer_registry", "relative.toml"),
        ("credentials_profile", None),
        ("credential_process", "/etc/process"),
        ("unknown", "field"),
    ],
)
def test_evidence_manifest_rejects_ambiguous_authority(
    binding: tuple[CheckpointEvidenceDeployment, ProjectContext], field: str, value: object
) -> None:
    configured, _ = binding
    with pytest.raises(ValidationError):
        CheckpointEvidenceDeployment.model_validate({**configured.model_dump(), field: value})


@pytest.mark.parametrize("change", ["workspace", "state", "writable", "symlink", "public-secret"])
def test_evidence_authority_is_outside_research_and_state(
    binding: tuple[CheckpointEvidenceDeployment, ProjectContext], change: str
) -> None:
    configured, project = binding
    path = configured.credentials_file
    assert path is not None
    if change in {"workspace", "state"}:
        parent = project.root if change == "workspace" else project.state_root
        parent.mkdir(exist_ok=True)
        changed = parent / "credentials"
        changed.write_bytes(path.read_bytes())
        changed.chmod(0o600)
        configured = configured.model_copy(update={"credentials_file": changed})
    elif change == "symlink":
        linked = path.parent / "linked-credentials"
        linked.symlink_to(path)
        configured = configured.model_copy(update={"credentials_file": linked})
    else:
        path.chmod(0o660 if change == "writable" else 0o644)
    with pytest.raises(ProjectStateError):
        validate_evidence_deployment(configured, project)
    assert not (project.root / ".heartwood").exists()


@pytest.mark.parametrize("location", ["operator", "workspace", "state"])
def test_signer_tls_trust_stays_outside_project_authority(
    binding: tuple[CheckpointEvidenceDeployment, ProjectContext], location: str
) -> None:
    configured, project = binding
    parent = {
        "operator": project.root.parent,
        "workspace": project.root,
        "state": project.state_root,
    }[location]
    parent.mkdir(parents=True, exist_ok=True)
    ca = server_certificates(parent / "tls")
    document = tomllib.loads(configured.signer_registry.read_text())
    document["profiles"]["records"]["trusted_ca_bundle"] = str(ca)
    configured.signer_registry.write_text(tomli_w.dumps(document))
    if location == "operator":
        validate_evidence_deployment(configured, project)
    else:
        with pytest.raises(ProjectStateError, match="outside"):
            validate_evidence_deployment(configured, project)


def test_sdk_uses_only_explicit_credentials_and_endpoint(
    binding: tuple[CheckpointEvidenceDeployment, ProjectContext], monkeypatch: pytest.MonkeyPatch
) -> None:
    configured, project = binding
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "ambient-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "ambient-secret")
    monkeypatch.setenv("AWS_PROFILE", "ambient-profile")
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://untrusted.test")
    monkeypatch.setenv("AWS_CA_BUNDLE", "/missing/untrusted.pem")
    client = _sdk_client(configured, project)
    try:
        url = cast(Any, client).generate_presigned_url(
            "get_object", Params={"Bucket": configured.bucket, "Key": "synthetic"}
        )
        parsed = urlsplit(url)
        assert parsed.netloc == "records.test"
        assert parse_qs(parsed.query)["X-Amz-Credential"][0].startswith("synthetic-records/")
        assert "ambient-key" not in url
    finally:
        client.close()
    assert configured.credentials_file is not None
    configured.credentials_file.write_text("[other-profile]\n")
    with pytest.raises(ProjectStateError, match="credentials are unavailable"):
        _sdk_client(configured, project)


def test_sdk_process_provider_refreshes_temporary_credentials(
    binding: tuple[CheckpointEvidenceDeployment, ProjectContext],
) -> None:
    configured, project = binding
    program = project.root.parent / "credential-process"
    program.write_text(f"""#!{sys.executable}
import datetime, json
from contextlib import nullcontext
from pathlib import Path
counter = Path("invocations")
n = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(n))
expires = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
    seconds=30 if n == 1 else 3600
)
print(json.dumps({{
    "Version": 1, "AccessKeyId": "synthetic-" + str(n),
    "SecretAccessKey": "synthetic-secret", "SessionToken": "synthetic-token",
    "Expiration": expires.isoformat(),
}}))
""")
    program.chmod(0o500)
    configured = CheckpointEvidenceDeployment.model_validate(
        {
            **configured.model_dump(),
            "credentials_file": None,
            "credentials_profile": None,
            "credential_process": program,
        }
    )
    validate_evidence_deployment(configured, project)
    assert not (program.parent / "invocations").exists()
    client = _sdk_client(configured, project)
    try:
        url = cast(Any, client).generate_presigned_url(
            "get_object", Params={"Bucket": configured.bucket, "Key": "synthetic"}
        )
        assert parse_qs(urlsplit(url).query)["X-Amz-Credential"][0].startswith("synthetic-2/")
        assert (program.parent / "invocations").read_text() == "2"
    finally:
        client.close()
    assert not (project.root / "invocations").exists()


def test_deployment_manifest_binds_evidence_and_requires_worker_isolation(
    worker_deployment: tuple[OpenShellWorkerDeployment, ProjectContext],
) -> None:
    worker, project = worker_deployment
    project.state_root.parent.mkdir(mode=0o700)
    configured = settings(project.root.parent)
    record: dict[str, Any] = {
        "schema_version": "heartwood.project-deployment.v1",
        "project_id": str(project.project_id),
        "workspace": str(project.root),
        "state_root": str(project.state_root),
        "policy": PolicyProfile(policy_id="synthetic", platform_id="generic").model_dump(
            exclude_none=True
        ),
        "worker": worker.model_dump(mode="json", exclude_none=True),
        "evidence": configured.model_dump(mode="json", exclude_none=True),
    }
    manifest = project.root.parent / "deployment.toml"
    manifest.write_text(tomli_w.dumps(record))
    manifest.chmod(0o600)
    loaded = load_project_deployment(manifest, project_root=project.root)
    assert loaded.evidence == configured
    assert loaded.worker == worker
    del record["worker"]
    manifest.write_text(tomli_w.dumps(record))
    with pytest.raises(ProjectStateError, match="complete worker isolation"):
        load_project_deployment(manifest, project_root=project.root)


@pytest.mark.parametrize("fail", [False, True])
def test_bound_factory_uses_fixed_scope_and_closes_client(
    binding: tuple[CheckpointEvidenceDeployment, ProjectContext],
    monkeypatch: pytest.MonkeyPatch,
    fail: bool,
) -> None:
    configured, project = binding

    class Client:
        closed = False

        def close(self) -> None:
            self.closed = True

    client = Client()
    monkeypatch.setattr(
        "heartwood.gateway.checkpoint_deployment._sdk_client", lambda *_args: client
    )
    expectation = (
        pytest.raises(RuntimeError, match="synthetic startup failure") if fail else nullcontext()
    )
    with expectation, deployment_checkpoint_evidence(configured, project) as bind:
        first, second = bind("session-a"), bind("session-b")
        assert first.records.scope.project_id == project.project_id
        assert first.records.scope.session_id == "session-a"
        assert second.records.scope.session_id == "session-b"
        assert first.records.scope.retention == configured.retention
        assert first.mode == "required"
        assert len(first.trusted_keys) == 1
        if fail:
            raise RuntimeError("synthetic startup failure")
    assert client.closed


def test_gateway_cli_owns_the_evidence_client_and_binds_sessions(
    worker_deployment: tuple[OpenShellWorkerDeployment, ProjectContext],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker, project = worker_deployment
    project.state_root.parent.mkdir(mode=0o700)
    configured = settings(project.root.parent)
    manifest = project.root.parent / "deployment.toml"
    manifest.write_text(
        tomli_w.dumps(
            {
                "schema_version": "heartwood.project-deployment.v1",
                "project_id": str(project.project_id),
                "workspace": str(project.root),
                "state_root": str(project.state_root),
                "policy": PolicyProfile(policy_id="synthetic", platform_id="generic").model_dump(
                    exclude_none=True
                ),
                "worker": worker.model_dump(mode="json", exclude_none=True),
                "evidence": configured.model_dump(mode="json", exclude_none=True),
            }
        )
    )
    manifest.chmod(0o600)
    web = project.root.parent / "web"
    web.mkdir()
    (web / "index.html").write_text("<main>Synthetic</main>")

    class Client:
        closed = False

        def close(self) -> None:
            self.closed = True

    client = Client()
    monkeypatch.setattr(
        "heartwood.gateway.checkpoint_deployment._sdk_client", lambda *_args: client
    )
    monkeypatch.setattr(
        "heartwood.gateway.worker_native.native_worker_plan_factory",
        lambda *_args: lambda _request: pytest.fail("no worker should start during launch"),
    )
    observed = []

    def run(server: uvicorn.Server, **_kwargs: object) -> None:
        app = server.config.app
        assert isinstance(app, GatewayAsgiApp)
        factory = app.gateway._checkpoint_evidence_factory
        assert factory is not None
        controller = factory("synthetic-session")
        assert controller.records.scope.project_id == project.project_id
        assert controller.records.scope.session_id == "synthetic-session"
        assert controller.mode == "required"
        observed.append(controller)
        app.gateway.stop()

    monkeypatch.setattr("heartwood.cli.uvicorn.Server.run", run)
    monkeypatch.setattr(
        "heartwood.cli.uvicorn.Config.bind_socket",
        lambda _config: socket.create_server(("127.0.0.1", 0)),
    )
    monkeypatch.chdir(project.root)
    monkeypatch.setenv("HEARTWOOD_PLATFORM", "generic")
    assert (
        main(
            [
                "gateway",
                "serve",
                "--deployment",
                str(manifest),
                "--web-root",
                str(web),
                "--ingress-mode",
                "direct-loopback",
            ]
        )
        == 0
    )
    assert len(observed) == 1
    assert client.closed
    assert not (project.root / ".heartwood").exists()


def test_credential_helper_timeout_is_bounded_and_output_is_private() -> None:
    child = _CredentialProcess(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    with pytest.raises(ProjectStateError, match="timed out"):
        child.communicate(timeout=0.05)
    assert child.poll() is not None
    child = _CredentialProcess(
        [sys.executable, "-c", "import sys; sys.stderr.write('synthetic-secret'); sys.exit(1)"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    with pytest.raises(ProjectStateError, match="helper failed") as failure:
        child.communicate()
    assert "synthetic-secret" not in str(failure.value)
    assert child.poll() is not None


def test_records_client_startup_failure_does_not_expose_provider_details(
    binding: tuple[CheckpointEvidenceDeployment, ProjectContext],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured, project = binding

    def unavailable(*_args: object) -> None:
        raise RuntimeError("synthetic provider secret in SDK error")

    monkeypatch.setattr("heartwood.gateway.checkpoint_deployment._sdk_client", unavailable)
    with (
        pytest.raises(ProjectStateError, match="unavailable or invalid") as failure,
        deployment_checkpoint_evidence(configured, project),
    ):
        pytest.fail("an unavailable records client must not yield a session binding")
    assert "synthetic provider secret" not in str(failure.value)
    assert failure.value.__suppress_context__
