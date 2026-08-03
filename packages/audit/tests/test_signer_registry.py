# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Tests for deployment-owned checkpoint signer configuration."""

from __future__ import annotations

import base64
import stat
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from heartwood.audit import (
    CheckpointSignerError,
    LocalEd25519CheckpointSigner,
    checkpoint_public_key_fingerprint,
    discover_checkpoint_signer_registry,
    initialize_local_checkpoint_signer,
    load_checkpoint_signer_registry,
)
from heartwood.schemas import AuditCheckpointSignRequest, AuditCheckpointStatement, AuditRetention


class _SigningTransport:
    def __init__(
        self,
        signer: LocalEd25519CheckpointSigner,
        *,
        wrong_identity: bool = False,
        invalid_signature: bool = False,
    ) -> None:
        self.signer = signer
        self.wrong_identity = wrong_identity
        self.invalid_signature = invalid_signature

    def post(
        self,
        endpoint: str,  # noqa: ARG002
        *,
        body: bytes,
        headers: Mapping[str, str],  # noqa: ARG002
        timeout_seconds: float,  # noqa: ARG002
    ) -> object:
        statement = AuditCheckpointSignRequest.model_validate_json(body).statement
        signature = self.signer.sign(statement)
        if self.wrong_identity:
            signature = signature.model_copy(update={"key_version": "wrong"})
        if self.invalid_signature:
            signature = signature.model_copy(
                update={"value": base64.b64encode(bytes(64)).decode("ascii")}
            )
        return signature.model_dump(mode="json")


def test_local_setup_writes_registry_last_without_embedding_private_material(
    tmp_path: Path,
) -> None:
    setup = initialize_local_checkpoint_signer(directory=tmp_path / "operator")
    registry = load_checkpoint_signer_registry(setup.registry)
    profile = registry.profile()

    assert registry.default_profile == "local-development"
    assert profile.mode == "development"
    assert profile.trusted_public_key == setup.public_key
    assert profile.authorization_token_file == setup.authorization_token
    assert stat.S_IMODE(setup.private_key.stat().st_mode) == 0o600
    assert stat.S_IMODE(setup.authorization_token.stat().st_mode) == 0o600
    contents = setup.registry.read_text(encoding="utf-8")
    assert str(setup.private_key) not in contents
    assert "PRIVATE KEY" not in contents
    assert "local-checkpoint-token" in contents


def test_registry_discovery_uses_one_authority_without_merging(tmp_path: Path) -> None:
    user = initialize_local_checkpoint_signer(
        directory=tmp_path / "home" / ".config" / "heartwood",
        profile_id="user-local",
    )
    system = initialize_local_checkpoint_signer(
        directory=tmp_path / "system",
        profile_id="system-local",
        endpoint="http://127.0.0.1:8772/v1/checkpoints/sign",
    )

    discovered = discover_checkpoint_signer_registry(
        {},
        home=tmp_path / "home",
        system_path=system.registry,
    )
    assert discovered.default_profile == "system-local"
    assert {profile.profile_id for profile in discovered.profiles} == {"system-local"}

    explicit = discover_checkpoint_signer_registry(
        {"HEARTWOOD_CHECKPOINT_SIGNER_REGISTRY": str(user.registry)},
        home=tmp_path / "home",
        system_path=system.registry,
    )
    assert explicit.default_profile == "user-local"

    empty = discover_checkpoint_signer_registry(
        {},
        home=tmp_path / "empty",
        system_path=tmp_path / "missing-system",
    )
    assert empty.profiles == ()
    with pytest.raises(CheckpointSignerError, match="not approved"):
        discovered.profile("")
    with pytest.raises(CheckpointSignerError, match="no deployment checkpoint signer"):
        empty.profile()


def test_registry_rejects_modified_trust_and_private_token_permissions(tmp_path: Path) -> None:
    setup = initialize_local_checkpoint_signer(directory=tmp_path / "operator")
    setup.authorization_token.chmod(0o644)
    with pytest.raises(CheckpointSignerError, match="owner-only"):
        load_checkpoint_signer_registry(setup.registry)

    setup.authorization_token.chmod(0o600)
    contents = setup.registry.read_text(encoding="utf-8")
    setup.registry.write_text(contents.replace("sha256:", "sha256:" + "0"), encoding="utf-8")
    setup.registry.chmod(0o600)
    with pytest.raises(CheckpointSignerError, match=r"SHA-256 digest|fingerprint"):
        load_checkpoint_signer_registry(setup.registry)

    setup.registry.write_text(contents, encoding="utf-8")
    setup.registry.chmod(0o600)
    setup.authorization_token.write_text("invalid token\n", encoding="utf-8")
    setup.authorization_token.chmod(0o600)
    registry = load_checkpoint_signer_registry(setup.registry)
    with pytest.raises(CheckpointSignerError, match="authorization token is invalid"):
        registry.profile().authorization_token()

    setup.authorization_token.write_text("valid-token\n", encoding="utf-8")
    setup.authorization_token.chmod(0o600)
    registry = load_checkpoint_signer_registry(setup.registry)
    setup.authorization_token.unlink()
    with pytest.raises(CheckpointSignerError, match="authorization token is unavailable"):
        registry.profile().authorization_token()

    setup.registry.chmod(0o620)
    with pytest.raises(CheckpointSignerError, match="not writable by other users"):
        load_checkpoint_signer_registry(setup.registry)


def test_registry_and_local_setup_fail_closed_on_unsafe_paths(tmp_path: Path) -> None:
    setup = initialize_local_checkpoint_signer(directory=tmp_path / "operator")
    with pytest.raises(CheckpointSignerError, match="replace existing"):
        initialize_local_checkpoint_signer(directory=setup.registry.parent)

    link = tmp_path / "registry-link.toml"
    link.symlink_to(setup.registry)
    with pytest.raises(CheckpointSignerError, match="symbolic link"):
        load_checkpoint_signer_registry(link)
    with pytest.raises(CheckpointSignerError, match="bounded regular file"):
        load_checkpoint_signer_registry(tmp_path)

    with pytest.raises(CheckpointSignerError, match="absolute path"):
        discover_checkpoint_signer_registry(
            {"HEARTWOOD_CHECKPOINT_SIGNER_REGISTRY": "relative.toml"}
        )
    with pytest.raises(CheckpointSignerError, match="registry is unavailable"):
        discover_checkpoint_signer_registry(
            {"HEARTWOOD_CHECKPOINT_SIGNER_REGISTRY": str(tmp_path / "missing.toml")}
        )
    with pytest.raises(CheckpointSignerError, match="XDG_CONFIG_HOME"):
        discover_checkpoint_signer_registry(
            {"XDG_CONFIG_HOME": "relative"},
            system_path=tmp_path / "missing-system",
        )

    with pytest.raises(CheckpointSignerError, match="initialize local checkpoint signer"):
        initialize_local_checkpoint_signer(
            directory=tmp_path / "invalid-profile",
            profile_id="Invalid Profile",
        )
    assert not (tmp_path / "invalid-profile" / "checkpoint-signers.toml").exists()


def test_registry_rejects_malformed_or_incomplete_documents(tmp_path: Path) -> None:
    setup = initialize_local_checkpoint_signer(directory=tmp_path / "operator")
    documents = (
        ("profiles = [\n", "registry is invalid"),
        (
            "\n".join(
                (
                    'schema_version = "heartwood.checkpoint-signer-registry.v1"',
                    'default_profile = "local-development"',
                    "profiles = {}",
                    "",
                )
            ),
            "must contain profiles",
        ),
        (
            "\n".join(
                (
                    'schema_version = "heartwood.checkpoint-signer-registry.v1"',
                    'default_profile = "local-development"',
                    'profiles = { local-development = "not-a-table" }',
                    "",
                )
            ),
            "profile must be a table",
        ),
    )

    for document, message in documents:
        setup.registry.write_text(document, encoding="utf-8")
        setup.registry.chmod(0o600)
        with pytest.raises(CheckpointSignerError, match=message):
            load_checkpoint_signer_registry(setup.registry)


def test_profile_pins_remote_identity_and_verifies_response_before_use(tmp_path: Path) -> None:
    setup = initialize_local_checkpoint_signer(directory=tmp_path / "operator")
    profile = load_checkpoint_signer_registry(setup.registry).profile()
    local = LocalEd25519CheckpointSigner(
        private_key=setup.private_key,
        signer_id=profile.signer_id,
        key_id=profile.key_id,
        key_version=profile.key_version,
    )

    signature = profile.signer(transport=_SigningTransport(local)).sign(_statement())

    assert signature.key_version == profile.key_version
    with pytest.raises(CheckpointSignerError, match="deployment profile"):
        profile.signer(transport=_SigningTransport(local, wrong_identity=True)).sign(_statement())
    with pytest.raises(CheckpointSignerError, match="signature is invalid"):
        profile.signer(transport=_SigningTransport(local, invalid_signature=True)).sign(
            _statement()
        )


def test_production_registry_supports_p256_without_project_or_private_key_material(
    tmp_path: Path,
) -> None:
    operator = tmp_path / "operator"
    operator.mkdir(mode=0o700)
    public_key = ec.generate_private_key(ec.SECP256R1()).public_key()
    public_path = operator / "public.pem"
    public_path.write_bytes(
        public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    public_path.chmod(0o644)
    registry_path = operator / "checkpoint-signers.toml"
    registry_path.write_text(
        "\n".join(
            (
                'schema_version = "heartwood.checkpoint-signer-registry.v1"',
                'default_profile = "production-records"',
                "",
                "[profiles.production-records]",
                'mode = "production"',
                'endpoint = "https://signer.example/v1/checkpoints/sign"',
                'signer_id = "research-records"',
                'key_id = "heartwood-audit"',
                'key_version = "2026-08"',
                'algorithm = "ecdsa-p256-sha256"',
                f'public_key_sha256 = "{checkpoint_public_key_fingerprint(public_key)}"',
                f'trusted_public_key = "{public_path}"',
                "timeout_seconds = 15",
                "",
            )
        ),
        encoding="utf-8",
    )
    registry_path.chmod(0o600)

    profile = load_checkpoint_signer_registry(registry_path).profile()

    assert profile.mode == "production"
    assert profile.algorithm == "ecdsa-p256-sha256"
    assert profile.authorization_token() is None

    registry_path.write_text(
        registry_path.read_text(encoding="utf-8").replace(
            'algorithm = "ecdsa-p256-sha256"',
            'algorithm = "ed25519"',
        ),
        encoding="utf-8",
    )
    registry_path.chmod(0o600)
    with pytest.raises(CheckpointSignerError, match="algorithm does not match its public key"):
        load_checkpoint_signer_registry(registry_path)


@pytest.mark.parametrize(
    ("transform", "message"),
    [
        (
            lambda text: text.replace('mode = "development"', 'mode = "production"'),
            "HTTPS",
        ),
        (
            lambda text: text.replace('mode = "development"', 'mode = "unsupported"'),
            "unsupported checkpoint signer mode",
        ),
        (
            lambda text: text.replace('mode = "development"', 'mode = ""'),
            "mode must be a non-empty string",
        ),
        (
            lambda text: text.replace('algorithm = "ed25519"', 'algorithm = "rsa"'),
            "unsupported checkpoint signature algorithm",
        ),
        (
            lambda text: text.replace(
                'algorithm = "ed25519"',
                'algorithm = "ecdsa-p256-sha256"',
            ),
            "algorithm does not match its public key",
        ),
        (
            lambda text: text.replace("timeout_seconds = 15", "timeout_seconds = nan"),
            "signer timeout",
        ),
        (
            lambda text: text.replace("timeout_seconds = 15", 'timeout_seconds = "15"'),
            "timeout_seconds must be numeric",
        ),
        (
            lambda text: text.replace(
                'signer_id = "local-development"',
                'signer_id = "unsafe signer"',
            ),
            "safe identifier",
        ),
        (
            lambda text: (
                "\n".join(
                    'trusted_public_key = "relative.pem"'
                    if line.startswith("trusted_public_key =")
                    else line
                    for line in text.splitlines()
                )
                + "\n"
            ),
            "trusted_public_key must be an absolute path",
        ),
        (
            lambda text: (
                "\n".join(
                    f'public_key_sha256 = "sha256:{"0" * 64}"'
                    if line.startswith("public_key_sha256 =")
                    else line
                    for line in text.splitlines()
                )
                + "\n"
            ),
            "fingerprint does not match",
        ),
        (
            lambda text: text.replace('key_version = "v1"', 'key_version = "version 1"'),
            "visible ASCII characters",
        ),
        (
            lambda text: text + "unexpected = true\n",
            "unsupported fields",
        ),
        (
            lambda text: text.replace(
                'schema_version = "heartwood.checkpoint-signer-registry.v1"',
                'schema_version = "unsupported"',
            ),
            "unsupported checkpoint signer registry schema",
        ),
        (
            lambda text: text.replace(
                "[profiles.local-development]",
                "unexpected = true\n\n[profiles.local-development]",
            ),
            "registry contains unsupported fields",
        ),
        (
            lambda text: text.replace(
                'default_profile = "local-development"',
                'default_profile = "missing"',
            ),
            "default_profile is not defined",
        ),
        (
            lambda text: (
                "\n".join(
                    line
                    for line in text.splitlines()
                    if not line.startswith("authorization_token_file =")
                )
                + "\n"
            ),
            "require an authorization token",
        ),
    ],
)
def test_registry_rejects_ambiguous_or_unsafe_profile_configuration(
    tmp_path: Path,
    transform: Callable[[str], str],
    message: str,
) -> None:
    setup = initialize_local_checkpoint_signer(directory=tmp_path / "operator")
    setup.registry.write_text(
        transform(setup.registry.read_text(encoding="utf-8")), encoding="utf-8"
    )
    setup.registry.chmod(0o600)

    with pytest.raises(CheckpointSignerError, match=message):
        load_checkpoint_signer_registry(setup.registry)


def _statement() -> AuditCheckpointStatement:
    return AuditCheckpointStatement(
        deployment_id="development",
        session_id="session-1",
        created_at="2026-08-02T12:00:00Z",
        audit_event_count=0,
        terminal_event_hash=None,
        audit_content_sha256=f"sha256:{'a' * 64}",
        audit_size_bytes=0,
        retention=AuditRetention(policy_id="development", retain_until="2026-08-02"),
    )
