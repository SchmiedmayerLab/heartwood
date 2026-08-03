# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Explicit local signer material for development and offline deployments."""

from __future__ import annotations

import secrets
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

import tomli_w
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from heartwood.audit._signer import CheckpointSignerError, checkpoint_public_key_fingerprint
from heartwood.audit._signer_registry import load_checkpoint_signer_registry
from heartwood.persistence import (
    DurableFileError,
    unlink_durable,
    write_private_bytes_atomic,
    write_private_text_atomic,
)


@dataclass(frozen=True, slots=True)
class LocalCheckpointSignerSetup:
    """Paths created for one explicit local signer profile."""

    registry: Path
    private_key: Path
    public_key: Path
    authorization_token: Path
    profile_id: str
    endpoint: str


def initialize_local_checkpoint_signer(
    *,
    directory: Path,
    profile_id: str = "local-development",
    endpoint: str = "http://127.0.0.1:8771/v1/checkpoints/sign",
    key_version: str = "v1",
) -> LocalCheckpointSignerSetup:
    """Create local signer material and write the deployment registry last."""
    root = directory.expanduser()
    if root.is_symlink():
        raise CheckpointSignerError("local signer directory must not be a symbolic link")
    registry = root / "checkpoint-signers.toml"
    private_key_path = root / "local-checkpoint-private.pem"
    public_key_path = root / "local-checkpoint-public.pem"
    token_path = root / "local-checkpoint-token"
    targets = (private_key_path, public_key_path, token_path, registry)
    if any(path.exists() or path.is_symlink() for path in targets):
        raise CheckpointSignerError("local signer setup refuses to replace existing files")
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        root.chmod(0o700)
    except OSError as error:
        raise CheckpointSignerError("local signer directory is unavailable") from error

    private_key = Ed25519PrivateKey.generate()
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_key = private_key.public_key()
    public_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    token = secrets.token_urlsafe(32)
    payload = {
        "schema_version": "heartwood.checkpoint-signer-registry.v1",
        "default_profile": profile_id,
        "profiles": {
            profile_id: {
                "mode": "development",
                "endpoint": endpoint,
                "signer_id": profile_id,
                "key_id": "local-checkpoint-signing",
                "key_version": key_version,
                "algorithm": "ed25519",
                "public_key_sha256": checkpoint_public_key_fingerprint(public_key),
                "trusted_public_key": str(public_key_path.resolve()),
                "authorization_token_file": str(token_path.resolve()),
                "timeout_seconds": 15,
            }
        },
    }
    created: list[Path] = []
    try:
        write_private_bytes_atomic(private_key_path, private_pem, secure_parent=False)
        created.append(private_key_path)
        write_private_bytes_atomic(public_key_path, public_pem, secure_parent=False)
        created.append(public_key_path)
        write_private_text_atomic(token_path, f"{token}\n", secure_parent=False)
        created.append(token_path)
        write_private_text_atomic(registry, tomli_w.dumps(payload), secure_parent=False)
        created.append(registry)
        load_checkpoint_signer_registry(registry)
    except (CheckpointSignerError, DurableFileError, OSError) as error:
        for path in reversed(created):
            with suppress(DurableFileError, OSError):
                unlink_durable(path, missing_ok=True)
        raise CheckpointSignerError("unable to initialize local checkpoint signer") from error
    return LocalCheckpointSignerSetup(
        registry=registry.resolve(),
        private_key=private_key_path.resolve(),
        public_key=public_key_path.resolve(),
        authorization_token=token_path.resolve(),
        profile_id=profile_id,
        endpoint=endpoint,
    )
