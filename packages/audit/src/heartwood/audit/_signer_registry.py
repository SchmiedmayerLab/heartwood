# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Deployment-owned checkpoint signer registry."""

from __future__ import annotations

import os
import re
import stat
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from heartwood.audit._signer import (
    CheckpointSigner,
    CheckpointSignerError,
    CheckpointSignerTransport,
    RemoteCheckpointSigner,
    checkpoint_public_key_fingerprint,
    load_checkpoint_public_key,
    validate_checkpoint_signer_token,
    verify_checkpoint_signature,
)
from heartwood.persistence import DurableFileError, read_private_text
from heartwood.schemas import (
    AuditCheckpointSignature,
    AuditCheckpointStatement,
    CheckpointSignatureAlgorithm,
)

_REGISTRY_ENV = "HEARTWOOD_CHECKPOINT_SIGNER_REGISTRY"
_REGISTRY_SCHEMA = "heartwood.checkpoint-signer-registry.v1"
_SYSTEM_REGISTRY = Path("/etc/heartwood/checkpoint-signers.toml")
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SAFE_PROFILE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_MAXIMUM_CONFIG_BYTES = 256 * 1024
_MAXIMUM_TOKEN_BYTES = 64 * 1024

type SignerMode = Literal["production", "development"]


@dataclass(frozen=True, slots=True)
class CheckpointSignerProfile:
    """One deployment-approved signer and independently trusted key."""

    profile_id: str
    mode: SignerMode
    endpoint: str
    signer_id: str
    key_id: str
    key_version: str
    algorithm: CheckpointSignatureAlgorithm
    public_key_sha256: str
    trusted_public_key: Path
    authorization_token_file: Path | None = None
    timeout_seconds: float = 15.0

    def authorization_token(self) -> str | None:
        """Resolve the optional bounded token from deployment-owned storage."""
        return (
            None
            if self.authorization_token_file is None
            else _read_authorization_token(self.authorization_token_file)
        )

    def signer(
        self,
        *,
        transport: CheckpointSignerTransport | None = None,
    ) -> CheckpointSigner:
        """Create a remote client without persisting its authorization token."""
        return self.validating_signer(
            RemoteCheckpointSigner(
                endpoint=self.endpoint,
                authorization_token=self.authorization_token(),
                timeout_seconds=self.timeout_seconds,
                allow_insecure_loopback=self.mode == "development",
                transport=transport,
            )
        )

    def validating_signer(self, signer: CheckpointSigner) -> CheckpointSigner:
        """Bind any backend to this deployment profile and trusted public key."""
        return _PinnedCheckpointSigner(
            profile=self,
            client=signer,
        )

    def validate_signature(self, signature: AuditCheckpointSignature) -> None:
        """Reject a response that differs from the deployment-pinned identity."""
        expected = (
            self.algorithm,
            self.signer_id,
            self.key_id,
            self.key_version,
            self.public_key_sha256,
        )
        actual = (
            signature.algorithm,
            signature.signer_id,
            signature.key_id,
            signature.key_version,
            signature.public_key_sha256,
        )
        if actual != expected:
            raise CheckpointSignerError(
                "checkpoint signer response does not match the deployment profile"
            )


@dataclass(frozen=True, slots=True)
class CheckpointSignerRegistry:
    """Immutable set of deployment-approved checkpoint signer profiles."""

    profiles: tuple[CheckpointSignerProfile, ...] = ()
    default_profile: str | None = None
    source: Path | None = None

    def profile(self, profile_id: str | None = None) -> CheckpointSignerProfile:
        """Resolve a project selection or the deployment default."""
        selected = self.default_profile if profile_id is None else profile_id
        if selected is None:
            raise CheckpointSignerError(
                "no deployment checkpoint signer is configured; install a signer registry"
            )
        profile = next((item for item in self.profiles if item.profile_id == selected), None)
        if profile is None:
            raise CheckpointSignerError(
                f"checkpoint signer profile is not approved by this deployment: {selected}"
            )
        return profile


@dataclass(frozen=True, slots=True)
class _PinnedCheckpointSigner:
    profile: CheckpointSignerProfile
    client: CheckpointSigner

    def sign(self, statement: AuditCheckpointStatement) -> AuditCheckpointSignature:
        signature = self.client.sign(statement)
        self.profile.validate_signature(signature)
        verify_checkpoint_signature(
            statement=statement,
            signature=signature,
            public_key=load_checkpoint_public_key(self.profile.trusted_public_key),
        )
        return signature


def discover_checkpoint_signer_registry(
    env: Mapping[str, str],
    *,
    home: Path | None = None,
    system_path: Path = _SYSTEM_REGISTRY,
) -> CheckpointSignerRegistry:
    """Load one registry without merging authorities from different owners."""
    explicit = env.get(_REGISTRY_ENV)
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_absolute():
            raise CheckpointSignerError(f"{_REGISTRY_ENV} must contain an absolute path")
        return load_checkpoint_signer_registry(path)
    if system_path.is_file() or system_path.is_symlink():
        return load_checkpoint_signer_registry(system_path)
    user_path = user_checkpoint_signer_registry_path(env, home=home)
    if user_path.is_file() or user_path.is_symlink():
        return load_checkpoint_signer_registry(user_path)
    return CheckpointSignerRegistry()


def user_checkpoint_signer_registry_path(
    env: Mapping[str, str],
    *,
    home: Path | None = None,
) -> Path:
    """Return the standard per-user fallback registry path."""
    config_home = env.get("XDG_CONFIG_HOME")
    if config_home:
        user_root = Path(config_home).expanduser()
        if not user_root.is_absolute():
            raise CheckpointSignerError("XDG_CONFIG_HOME must contain an absolute path")
    else:
        user_root = (home or Path.home()) / ".config"
    return user_root / "heartwood" / "checkpoint-signers.toml"


def load_checkpoint_signer_registry(path: Path) -> CheckpointSignerRegistry:
    """Load and validate one bounded, non-writable-by-others TOML registry."""
    resolved = _deployment_file(
        path,
        label="checkpoint signer registry",
        maximum_bytes=_MAXIMUM_CONFIG_BYTES,
    )
    try:
        value = tomllib.loads(read_private_text(resolved))
    except (DurableFileError, OSError, tomllib.TOMLDecodeError) as error:
        raise CheckpointSignerError("checkpoint signer registry is invalid") from error
    if set(value) != {"schema_version", "default_profile", "profiles"}:
        raise CheckpointSignerError("checkpoint signer registry contains unsupported fields")
    if value.get("schema_version") != _REGISTRY_SCHEMA:
        raise CheckpointSignerError("unsupported checkpoint signer registry schema")
    default_profile = _profile_identifier(value.get("default_profile"), "default_profile")
    raw_profiles = value.get("profiles")
    if not isinstance(raw_profiles, dict) or not raw_profiles:
        raise CheckpointSignerError("checkpoint signer registry must contain profiles")
    profiles = tuple(
        _profile_from_mapping(profile_id, raw) for profile_id, raw in sorted(raw_profiles.items())
    )
    if default_profile not in {profile.profile_id for profile in profiles}:
        raise CheckpointSignerError("checkpoint signer default_profile is not defined")
    return CheckpointSignerRegistry(
        profiles=profiles,
        default_profile=default_profile,
        source=resolved,
    )


def _profile_from_mapping(
    profile_id: object,
    value: object,
) -> CheckpointSignerProfile:
    normalized_id = _profile_identifier(profile_id, "profile id")
    if not isinstance(value, dict):
        raise CheckpointSignerError(f"checkpoint signer profile must be a table: {normalized_id}")
    allowed = {
        "algorithm",
        "authorization_token_file",
        "endpoint",
        "key_id",
        "key_version",
        "mode",
        "public_key_sha256",
        "signer_id",
        "timeout_seconds",
        "trusted_public_key",
    }
    if set(value) - allowed:
        raise CheckpointSignerError(
            f"checkpoint signer profile contains unsupported fields: {normalized_id}"
        )
    mode = _string(value.get("mode"), "mode")
    if mode not in {"production", "development"}:
        raise CheckpointSignerError(f"unsupported checkpoint signer mode: {mode}")
    algorithm = _string(value.get("algorithm"), "algorithm")
    if algorithm not in {"ed25519", "ecdsa-p256-sha256"}:
        raise CheckpointSignerError(f"unsupported checkpoint signature algorithm: {algorithm}")
    public_key_path = _absolute_path(value.get("trusted_public_key"), "trusted_public_key")
    public_key_path = _deployment_file(
        public_key_path,
        label="trusted checkpoint public key",
        maximum_bytes=_MAXIMUM_CONFIG_BYTES,
    )
    public_key = load_checkpoint_public_key(public_key_path)
    if algorithm == "ed25519" and not isinstance(public_key, Ed25519PublicKey):
        raise CheckpointSignerError("checkpoint signer algorithm does not match its public key")
    if algorithm == "ecdsa-p256-sha256" and not isinstance(public_key, ec.EllipticCurvePublicKey):
        raise CheckpointSignerError("checkpoint signer algorithm does not match its public key")
    public_key_sha256 = _digest(value.get("public_key_sha256"), "public_key_sha256")
    if checkpoint_public_key_fingerprint(public_key) != public_key_sha256:
        raise CheckpointSignerError("trusted checkpoint public key fingerprint does not match")
    token_value = value.get("authorization_token_file")
    token_file = (
        None if token_value is None else _absolute_path(token_value, "authorization_token_file")
    )
    if token_file is not None:
        token_file = _deployment_file(
            token_file,
            label="checkpoint signer authorization token",
            maximum_bytes=_MAXIMUM_TOKEN_BYTES,
            private=True,
        )
    endpoint = _string(value.get("endpoint"), "endpoint")
    timeout_value = value.get("timeout_seconds", 15.0)
    if isinstance(timeout_value, bool) or not isinstance(timeout_value, int | float):
        raise CheckpointSignerError("checkpoint signer timeout_seconds must be numeric")
    profile = CheckpointSignerProfile(
        profile_id=normalized_id,
        mode=cast(SignerMode, mode),
        endpoint=endpoint,
        signer_id=_identifier(value.get("signer_id"), "signer_id"),
        key_id=_nonspace(value.get("key_id"), "key_id"),
        key_version=_nonspace(value.get("key_version"), "key_version"),
        algorithm=cast(CheckpointSignatureAlgorithm, algorithm),
        public_key_sha256=public_key_sha256,
        trusted_public_key=public_key_path,
        authorization_token_file=token_file,
        timeout_seconds=float(timeout_value),
    )
    if profile.mode == "development" and profile.authorization_token_file is None:
        raise CheckpointSignerError("development signer profiles require an authorization token")
    if profile.mode == "production" and profile.endpoint.startswith("http://"):
        raise CheckpointSignerError("production signer profiles must use HTTPS")
    RemoteCheckpointSigner(
        endpoint=profile.endpoint,
        timeout_seconds=profile.timeout_seconds,
        allow_insecure_loopback=profile.mode == "development",
    )
    return profile


def _read_authorization_token(path: Path) -> str:
    resolved = _deployment_file(
        path,
        label="checkpoint signer authorization token",
        maximum_bytes=_MAXIMUM_TOKEN_BYTES,
        private=True,
    )
    try:
        token = read_private_text(resolved).strip()
    except (DurableFileError, OSError) as error:
        raise CheckpointSignerError(
            "checkpoint signer authorization token is unavailable"
        ) from error
    return validate_checkpoint_signer_token(token)


def _deployment_file(
    path: Path,
    *,
    label: str,
    maximum_bytes: int,
    private: bool = False,
) -> Path:
    if path.is_symlink():
        raise CheckpointSignerError(f"{label} must not be a symbolic link")
    try:
        metadata = path.stat()
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise CheckpointSignerError(f"{label} is unavailable") from error
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum_bytes:
        raise CheckpointSignerError(f"{label} must be a bounded regular file")
    forbidden = stat.S_IWGRP | stat.S_IWOTH
    if private:
        forbidden |= stat.S_IRGRP | stat.S_IROTH
    if metadata.st_mode & forbidden:
        requirement = "owner-only" if private else "not writable by other users"
        raise CheckpointSignerError(f"{label} must be {requirement}")
    if hasattr(os, "geteuid") and metadata.st_uid not in {0, os.geteuid()}:
        raise CheckpointSignerError(f"{label} must be owned by root or the current user")
    return resolved


def _absolute_path(value: object, field: str) -> Path:
    path = Path(_string(value, field)).expanduser()
    if not path.is_absolute():
        raise CheckpointSignerError(f"checkpoint signer {field} must be an absolute path")
    return path


def _identifier(value: object, field: str) -> str:
    normalized = _string(value, field)
    if _SAFE_IDENTIFIER.fullmatch(normalized) is None:
        raise CheckpointSignerError(f"checkpoint signer {field} must be a safe identifier")
    return normalized


def _profile_identifier(value: object, field: str) -> str:
    normalized = _string(value, field)
    if _SAFE_PROFILE_ID.fullmatch(normalized) is None:
        raise CheckpointSignerError(f"checkpoint signer {field} must be a lowercase identifier")
    return normalized


def _digest(value: object, field: str) -> str:
    normalized = _string(value, field)
    if re.fullmatch(r"sha256:[0-9a-f]{64}", normalized) is None:
        raise CheckpointSignerError(f"checkpoint signer {field} must be a SHA-256 digest")
    return normalized


def _nonspace(value: object, field: str) -> str:
    normalized = _string(value, field)
    if len(normalized) > 512 or any(not 0x21 <= ord(character) <= 0x7E for character in normalized):
        raise CheckpointSignerError(
            f"checkpoint signer {field} must contain visible ASCII characters"
        )
    return normalized


def _string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CheckpointSignerError(f"checkpoint signer {field} must be a non-empty string")
    return value.strip()
