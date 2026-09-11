# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Gateway boundary tests for audit verification and authoritative export."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from heartwood.audit import AuditCheckpointVerification
from heartwood.gateway import (
    CheckpointSignerError,
    CheckpointSignerProfile,
    CheckpointSignerRegistry,
    LocalEd25519CheckpointSigner,
    ProjectContext,
    ProjectStateError,
    SessionGateway,
    checkpoint_public_key_fingerprint,
)
from heartwood.gateway.experiments import ExperimentRecorder
from heartwood.schemas import AuditCheckpointSignature, AuditCheckpointStatement
from heartwood.schemas.experiments import ExperimentExportBinding
from heartwood.session import CommandKind, SessionCommand


@pytest.fixture
def gateway_factory() -> Iterator[Callable[[Path], SessionGateway]]:
    gateways: list[SessionGateway] = []

    def create(project_root: Path) -> SessionGateway:
        private_key, public_key = _write_key_pair(project_root.parent / "signer")
        public = serialization.load_pem_public_key(public_key.read_bytes())
        assert isinstance(public, Ed25519PublicKey)
        profile = CheckpointSignerProfile(
            profile_id="records",
            mode="production",
            endpoint="https://signer.example.invalid/v1/checkpoints/sign",
            signer_id="test-deployment",
            key_id="audit-signing",
            key_version="v1",
            algorithm="ed25519",
            public_key_sha256=checkpoint_public_key_fingerprint(public),
            trusted_public_key=public_key,
        )
        gateway = SessionGateway(
            project=ProjectContext(project_root),
            backend_id="deterministic",
            checkpoint_signer_registry=CheckpointSignerRegistry(
                profiles=(profile,),
                default_profile=profile.profile_id,
            ),
            checkpoint_signer_factory=lambda selected: LocalEd25519CheckpointSigner(
                private_key=private_key,
                signer_id=selected.signer_id,
                key_id=selected.key_id,
                key_version=selected.key_version,
            ),
        )
        gateways.append(gateway)
        return gateway

    yield create
    for gateway in gateways:
        gateway.stop()


def test_gateway_creates_and_verifies_checkpoint_outside_project(
    tmp_path: Path,
    gateway_factory: Callable[[Path], SessionGateway],
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    deployment_root = tmp_path / "deployment"
    gateway = gateway_factory(project_root)
    bundle = deployment_root / "session-main"

    created = gateway.create_audit_checkpoint(
        session_id="main",
        output=bundle,
        deployment_id="generic-research",
        retention_policy_id="research-audit-7y",
        retain_until="2033-08-02",
    )
    verified = gateway.verify_audit_checkpoint(bundle=bundle)
    current = gateway.verify_audit("main")

    assert verified == created
    assert current == created.audit
    assert created.checkpoint.statement.audit_event_count > 0


@pytest.mark.parametrize("include_experiments", [False, True])
def test_gateway_rejects_checkpoint_output_inside_agent_project(
    tmp_path: Path,
    gateway_factory: Callable[[Path], SessionGateway],
    include_experiments: bool,
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    gateway = gateway_factory(project_root)

    with pytest.raises(ProjectStateError, match="outside the Heartwood project"):
        gateway.create_audit_checkpoint(
            session_id="main",
            output=project_root / "checkpoint",
            deployment_id="generic-research",
            retention_policy_id="research-audit-7y",
            retain_until="2033-08-02",
            include_experiments=include_experiments,
        )

    assert not gateway.project.sessions_dir.exists()


@pytest.mark.parametrize("append_during_publication", [False, True])
def test_checkpoint_retains_the_exact_verified_experiment_snapshot(
    tmp_path: Path,
    gateway_factory: Callable[[Path], SessionGateway],
    monkeypatch: pytest.MonkeyPatch,
    append_during_publication: bool,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    gateway = gateway_factory(project)
    _record_analysis(gateway, "first.json")
    expected = gateway.export_experiments().jsonl.encode()
    sign = LocalEd25519CheckpointSigner.sign

    def append_then_sign(
        self: LocalEd25519CheckpointSigner, statement: AuditCheckpointStatement
    ) -> AuditCheckpointSignature:
        if append_during_publication:
            _record_analysis(gateway, "second.json")
        return sign(self, statement)

    monkeypatch.setattr(LocalEd25519CheckpointSigner, "sign", append_then_sign)
    bundle = tmp_path / "retained" / "checkpoint"
    created = gateway.create_audit_checkpoint(
        session_id="research",
        output=bundle,
        deployment_id="synthetic",
        retention_policy_id="research-audit-7y",
        retain_until="2033-08-02",
        include_experiments=True,
    )
    assert gateway.verify_audit_checkpoint(bundle=bundle) == created
    assert created.experiments == ExperimentExportBinding.from_content(expected)
    assert (bundle / "experiments.jsonl").read_bytes() == expected
    assert (gateway.export_experiments().jsonl.encode() != expected) == append_during_publication
    audit = (bundle / "audit.jsonl").read_text()
    assert "analysis.py" not in audit
    assert "first.json" not in audit
    assert "synthetic-result" not in audit
    assert "experiment_export" in audit
    assert "tool.execution.recorded" not in audit


def test_checkpoint_rejects_caller_supplied_experiment_bindings_before_mutation(
    tmp_path: Path, gateway_factory: Callable[[Path], SessionGateway]
) -> None:
    from heartwood.gateway import RestGateway, RestRequest

    project = tmp_path / "project"
    project.mkdir()
    gateway = gateway_factory(project)
    command = SessionCommand(
        command_id="forged-export",
        session_id="research",
        kind=CommandKind.AUDIT_EXPORT,
        actor_id="synthetic-user",
        created_at="2026-09-11T00:00:00Z",
        payload={"experiment_export": "untrusted-payload"},
    )
    response = RestGateway(gateway).handle(
        RestRequest(
            method="POST",
            path="/sessions/research/commands",
            body=command.model_dump_json(),
        )
    )
    assert response.status_code == 409
    assert "untrusted-payload" not in str(response.body)
    assert not gateway.project.state_root.exists()


def test_retry_after_signer_failure_does_not_repeat_an_analysis(
    tmp_path: Path,
    gateway_factory: Callable[[Path], SessionGateway],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    gateway = gateway_factory(project)
    _record_analysis(gateway, "result.json")
    before = gateway.export_experiments()
    bundle = tmp_path / "retained" / "checkpoint"
    sign = LocalEd25519CheckpointSigner.sign
    calls = 0

    def fail_once(
        self: LocalEd25519CheckpointSigner, statement: AuditCheckpointStatement
    ) -> AuditCheckpointSignature:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise CheckpointSignerError("synthetic signer unavailable")
        return sign(self, statement)

    monkeypatch.setattr(LocalEd25519CheckpointSigner, "sign", fail_once)

    def publish() -> AuditCheckpointVerification:
        return gateway.create_audit_checkpoint(
            session_id="research",
            output=bundle,
            deployment_id="synthetic",
            retention_policy_id="research-audit-7y",
            retain_until="2033-08-02",
            include_experiments=True,
        )

    with pytest.raises(ValueError, match="signer unavailable"):
        publish()
    assert not bundle.exists()
    assert gateway.export_experiments() == before
    publish()
    assert calls == 2
    assert gateway.export_experiments() == before
    assert (bundle / "experiments.jsonl").read_text() == before.jsonl
    assert gateway.verify_audit_checkpoint(bundle=bundle).experiments is not None


def _record_analysis(gateway: SessionGateway, output: str) -> None:
    script = gateway.project.root / "analysis.py"
    if not script.exists():
        script.write_text(
            "import sys\nfrom pathlib import Path\n"
            "Path(sys.argv[1]).write_text('synthetic-result\\n')\n"
        )
    result = ExperimentRecorder(gateway.project).record_command(
        actor_ref="synthetic-researcher",
        entry_point="analysis.py",
        outputs=(output,),
        arguments=(output,),
    )
    assert result.status == "succeeded"


@pytest.mark.parametrize("include_experiments", [False, True])
def test_gateway_does_not_mutate_a_session_when_no_signer_is_configured(
    tmp_path: Path,
    include_experiments: bool,
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    gateway = SessionGateway(
        project=ProjectContext(project_root),
        backend_id="deterministic",
        checkpoint_signer_registry=CheckpointSignerRegistry(),
    )
    try:
        with pytest.raises(CheckpointSignerError, match="no deployment checkpoint signer"):
            gateway.create_audit_checkpoint(
                session_id="main",
                output=tmp_path / "deployment" / "checkpoint",
                deployment_id="generic-research",
                retention_policy_id="research-audit-7y",
                retain_until="2033-08-02",
                include_experiments=include_experiments,
            )
        assert not gateway.project.sessions_dir.exists()
    finally:
        gateway.stop()


def test_gateway_copy_rejects_reserved_state_and_final_symlink(
    tmp_path: Path,
    gateway_factory: Callable[[Path], SessionGateway],
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    gateway = gateway_factory(project_root)
    gateway.create_audit_checkpoint(
        session_id="main",
        output=tmp_path / "deployment" / "checkpoint",
        deployment_id="generic-research",
        retention_policy_id="research-audit-7y",
        retain_until="2033-08-02",
    )

    with pytest.raises(ProjectStateError, match="private Heartwood state"):
        gateway.copy_audit_export("main", gateway.project.audit_dir / "copy.jsonl")

    target = tmp_path / "outside.jsonl"
    target.write_text("unchanged\n", encoding="utf-8")
    link = project_root / "audit.jsonl"
    link.symlink_to(target)
    with pytest.raises(ProjectStateError, match="symbolic link"):
        gateway.copy_audit_export("main", link)
    assert target.read_text(encoding="utf-8") == "unchanged\n"

    directory = tmp_path / "directory"
    directory.mkdir()
    with pytest.raises(ProjectStateError, match="write the audit copy safely"):
        gateway.copy_audit_export("main", directory)


def _write_key_pair(root: Path) -> tuple[Path, Path]:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = Ed25519PrivateKey.generate()
    private_path = root / "private.pem"
    public_path = root / "public.pem"
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
