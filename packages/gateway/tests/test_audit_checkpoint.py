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


def test_gateway_rejects_checkpoint_output_inside_agent_project(
    tmp_path: Path,
    gateway_factory: Callable[[Path], SessionGateway],
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
        )

    assert not gateway.project.sessions_dir.exists()


def test_gateway_does_not_mutate_a_session_when_no_signer_is_configured(
    tmp_path: Path,
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
