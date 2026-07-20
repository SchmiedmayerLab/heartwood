# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Credential bindings that keep secret values outside project state."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol, cast

from heartwood.adapters import PlatformCapabilities


class CredentialStoreError(ValueError):
    """Report an unavailable or failed credential operation."""


class KeyringBackend(Protocol):
    """Narrow subset of the system keyring API used by Heartwood."""

    priority: float

    def get_password(self, service: str, username: str) -> str | None:
        """Resolve one password."""

    def set_password(self, service: str, username: str, password: str) -> None:
        """Save one password."""

    def delete_password(self, service: str, username: str) -> None:
        """Delete one password."""


@dataclass(frozen=True, slots=True)
class CredentialStoreAvailability:
    """Non-secret credential storage capabilities for one project."""

    backends: tuple[str, ...]
    default_backend: str
    persistence_available: bool
    persistence_description: str

    def safe_dict(self) -> dict[str, object]:
        """Return a browser- and notebook-safe representation."""
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CredentialBindingStatus:
    """Non-secret status for one provider credential reference."""

    binding_id: str
    configured: bool
    source: str | None
    error: str | None = None

    def safe_dict(self) -> dict[str, object]:
        """Return status without resolving or exposing the secret."""
        return asdict(self)


class CredentialStore:
    """Resolve process, environment, and optional system-keyring credentials."""

    _SERVICE_NAME = "org.schmiedmayerlab.heartwood"

    def __init__(
        self,
        *,
        project_root: Path,
        capabilities: PlatformCapabilities,
        env: Mapping[str, str],
        keyring_backend: KeyringBackend | None = None,
        use_system_keyring: bool = True,
    ) -> None:
        self._project_key = hashlib.sha256(str(project_root.resolve()).encode("utf-8")).hexdigest()[
            :24
        ]
        self._env = dict(env)
        self._process_values: dict[str, str] = {}
        self._keyring = keyring_backend if "keyring" in capabilities.credential_backends else None
        if (
            self._keyring is None
            and use_system_keyring
            and "keyring" in capabilities.credential_backends
        ):
            self._keyring = _system_keyring_backend()
        if self._keyring is not None and self._keyring.priority <= 0:
            self._keyring = None

    def availability(self) -> CredentialStoreAvailability:
        """Describe safe storage options without accessing a secret."""
        if self._keyring is not None:
            return CredentialStoreAvailability(
                backends=("process", "keyring"),
                default_backend="process",
                persistence_available=True,
                persistence_description="System credential store for this Heartwood environment",
            )
        return CredentialStoreAvailability(
            backends=("process",),
            default_backend="process",
            persistence_available=False,
            persistence_description="Current Heartwood process only",
        )

    def save(self, binding_id: str, value: str, *, remember: bool = False) -> None:
        """Keep a credential for this process and optionally in the system keyring."""
        normalized = _validate_binding(binding_id)
        if not value.strip():
            raise CredentialStoreError("credential must not be empty")
        if remember:
            if self._keyring is None:
                raise CredentialStoreError(
                    "secure credential persistence is unavailable in this environment"
                )
            try:
                self._keyring.set_password(
                    self._SERVICE_NAME,
                    self._account(normalized),
                    value,
                )
            except Exception as error:  # pragma: no cover - backend-specific failures
                raise CredentialStoreError(
                    "the system credential store rejected the value"
                ) from error
        self._process_values[normalized] = value

    def resolve(self, binding_id: str) -> str | None:
        """Resolve a credential in process, environment, then keyring order."""
        normalized = _validate_binding(binding_id)
        process_value = self._process_values.get(normalized)
        if process_value:
            return process_value
        environment_value = self._env.get(normalized)
        if environment_value:
            return environment_value
        if self._keyring is None:
            return None
        try:
            value = self._keyring.get_password(self._SERVICE_NAME, self._account(normalized))
        except Exception as error:  # pragma: no cover - backend-specific failures
            raise CredentialStoreError("the system credential store could not be read") from error
        return value if value and value.strip() else None

    def forget(self, binding_id: str) -> None:
        """Remove process and persisted values for one project binding."""
        normalized = _validate_binding(binding_id)
        self._process_values.pop(normalized, None)
        if self._keyring is None:
            return
        try:
            if (
                self._keyring.get_password(self._SERVICE_NAME, self._account(normalized))
                is not None
            ):
                self._keyring.delete_password(self._SERVICE_NAME, self._account(normalized))
        except Exception as error:  # pragma: no cover - backend-specific failures
            raise CredentialStoreError(
                "the system credential store could not forget the value"
            ) from error

    def discard_process_value(self, binding_id: str) -> None:
        """Remove only a value entered for the current process."""
        self._process_values.pop(_validate_binding(binding_id), None)

    def clear_process_values(self) -> None:
        """Release every value entered into this gateway process."""
        self._process_values.clear()

    def status(self, binding_id: str) -> CredentialBindingStatus:
        """Return whether and where one credential can be resolved."""
        normalized = _validate_binding(binding_id)
        if self._process_values.get(normalized):
            return CredentialBindingStatus(normalized, True, "process")
        if self._env.get(normalized):
            return CredentialBindingStatus(normalized, True, "environment")
        if self._keyring is not None:
            try:
                if self._keyring.get_password(self._SERVICE_NAME, self._account(normalized)):
                    return CredentialBindingStatus(normalized, True, "keyring")
            except Exception:  # pragma: no cover - backend-specific failures
                return CredentialBindingStatus(
                    normalized,
                    False,
                    "unavailable",
                    "System credential store could not be read",
                )
        return CredentialBindingStatus(normalized, False, None)

    def environment(
        self,
        binding_ids: Sequence[str],
        *,
        tolerate_backend_errors: bool = False,
    ) -> dict[str, str]:
        """Materialize only requested bindings for an agent or provider call."""
        resolved = dict(self._env)
        for binding_id in binding_ids:
            try:
                value = self.resolve(binding_id)
            except CredentialStoreError:
                if not tolerate_backend_errors:
                    raise
                continue
            if value is not None:
                resolved[binding_id] = value
        return resolved

    def _account(self, binding_id: str) -> str:
        return f"{self._project_key}:{binding_id}"


def _validate_binding(binding_id: str) -> str:
    normalized = binding_id.strip()
    if not normalized or not normalized.replace("_", "").isalnum():
        raise CredentialStoreError("credential binding must be an environment-style identifier")
    return normalized


def _system_keyring_backend() -> KeyringBackend | None:
    try:
        import keyring
    except ImportError:  # pragma: no cover - optional native dependency
        return None
    return cast(KeyringBackend, keyring.get_keyring())
