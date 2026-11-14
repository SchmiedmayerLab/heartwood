# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Operator-bound evidence policy using the existing signer and storage SDK contracts."""

from __future__ import annotations

import importlib
import os
import shlex
import signal
import stat
import subprocess
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, Literal, Protocol, Self, cast
from urllib.parse import urlsplit

import certifi
from pydantic import BaseModel, ConfigDict, Field, model_validator

from heartwood.audit import (
    checkpoint_public_key_fingerprint,
    load_checkpoint_public_key,
    load_checkpoint_signer_registry,
)
from heartwood.audit._records_s3 import (
    CheckpointAuthorityScope,
    S3CheckpointRecords,
    S3RecordsClient,
)
from heartwood.core_adapter.checkpoint_evidence import SessionCheckpointEvidence
from heartwood.gateway._project import ProjectContext, ProjectStateError
from heartwood.schemas import AuditRetention

type _Name = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")]


class CheckpointEvidenceDeployment(BaseModel):
    """One trusted evidence policy, with no project-selected endpoints or credentials."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    mode: Literal["required", "buffered"]
    deployment_id: _Name
    endpoint: str = Field(max_length=2048)
    region: _Name
    bucket: str = Field(pattern=r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
    prefix: str = Field(pattern=r"^[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*$", max_length=512)
    retention: AuditRetention
    signer_registry: Path
    signer_profile: _Name
    previous_signer_profiles: tuple[_Name, ...] = ()
    credentials_file: Path | None = None
    credentials_profile: _Name | None = None
    credential_process: Path | None = None
    ca_bundle: Path | None = None

    @model_validator(mode="after")
    def _locations(self) -> Self:
        endpoint = urlsplit(self.endpoint)
        if (
            endpoint.scheme != "https"
            or not endpoint.hostname
            or endpoint.username is not None
            or endpoint.password is not None
            or endpoint.path not in {"", "/"}
            or endpoint.query
            or endpoint.fragment
        ):
            raise ValueError("records require an explicit HTTPS service origin")
        if (self.credentials_file is None) == (self.credential_process is None):
            raise ValueError("select exactly one records credential source")
        if (self.credentials_file is None) != (self.credentials_profile is None):
            raise ValueError("shared credentials require an explicit profile")
        for path in (
            self.signer_registry,
            self.credentials_file,
            self.credential_process,
            self.ca_bundle,
        ):
            if path is not None and (not path.is_absolute() or ".." in path.parts):
                raise ValueError("evidence authority requires absolute canonical paths")
        if len(set(self.previous_signer_profiles)) != len(self.previous_signer_profiles):
            raise ValueError("previous signer profiles must be unique")
        return self


def validate_evidence_deployment(
    settings: CheckpointEvidenceDeployment, project: ProjectContext
) -> None:
    """Reject authority in research files or state before opening a client or process."""
    from heartwood.gateway._deployment import validate_operator_file

    if project.protected_state is None or project.project_id is None:
        raise ProjectStateError("managed evidence requires a protected project binding")
    if date.fromisoformat(settings.retention.retain_until) < datetime.now(UTC).date():
        raise ProjectStateError("managed evidence retention has expired")
    for path in (
        settings.signer_registry,
        settings.credentials_file,
        settings.credential_process,
        settings.ca_bundle,
    ):
        if path is not None:
            validate_operator_file(path, project)
    if (
        settings.credentials_file is not None
        and stat.S_IMODE(settings.credentials_file.stat().st_mode) & 0o077
    ):
        raise ProjectStateError("records credentials require owner-only access")
    if settings.credential_process is not None and not os.access(
        settings.credential_process, os.X_OK
    ):
        raise ProjectStateError("records credential process must be executable")
    registry = load_checkpoint_signer_registry(settings.signer_registry)
    for name in (settings.signer_profile, *settings.previous_signer_profiles):
        profile = registry.profile(name)
        validate_operator_file(profile.trusted_public_key, project)
        if profile.trusted_ca_bundle is not None:
            validate_operator_file(profile.trusted_ca_bundle, project)
        if profile.authorization_token_file is not None:
            validate_operator_file(profile.authorization_token_file, project)
        if name == settings.signer_profile and profile.mode != "production":
            raise ProjectStateError(
                "managed evidence requires an independently authorized production signer"
            )


class _CredentialProcess(subprocess.Popen[bytes]):
    """Bound the SDK's credential helper and keep its error output private."""

    def communicate(
        self,
        input: bytes | None = None,  # noqa: A002
        timeout: float | None = None,
    ) -> tuple[bytes, bytes]:
        """Terminate only the owned helper on timeout; never expose its stderr."""
        try:
            output, errors = super().communicate(input, timeout=min(timeout or 15, 15))
        except subprocess.TimeoutExpired:
            os.killpg(self.pid, signal.SIGKILL)
            super().communicate()
            raise ProjectStateError("records credential helper timed out") from None
        if self.returncode != 0 or len(output) > 64 * 1024 or len(errors) > 64 * 1024:
            raise ProjectStateError("records credential helper failed or exceeded its output limit")
        return output, b""


class _ManagedRecordsClient(S3RecordsClient, Protocol):
    def close(self) -> None: ...


def _sdk_client(
    settings: CheckpointEvidenceDeployment, project: ProjectContext
) -> _ManagedRecordsClient:
    """Use SDK credential loading and refresh without an ambient credential chain."""
    credentials = importlib.import_module("botocore.credentials")
    session_module = importlib.import_module("botocore.session")
    config_module = importlib.import_module("botocore.config")
    session = session_module.Session()
    session.set_config_variable("config_file", os.devnull)
    session.set_config_variable("credentials_file", os.devnull)
    config_provider = importlib.import_module("botocore.configprovider")
    for name in ("profile", "data_path"):
        session.get_component("config_store").set_config_provider(
            name, config_provider.ConstantProvider(None)
        )
    if settings.credentials_file is not None:
        provider = credentials.SharedCredentialProvider(
            str(settings.credentials_file), profile_name=settings.credentials_profile
        )
    else:
        assert settings.credential_process is not None
        program = settings.credential_process

        def start_process(args: list[str], *, stdout: int, stderr: int) -> subprocess.Popen[bytes]:
            from heartwood.gateway._deployment import validate_operator_file

            validate_operator_file(program, project)
            return _CredentialProcess(
                args, cwd=program.parent, stdout=stdout, stderr=stderr, start_new_session=True
            )

        provider = credentials.ProcessProvider(
            profile_name="records",
            load_config=lambda: {
                "profiles": {"records": {"credential_process": shlex.quote(str(program))}}
            },
            popen=start_process,
        )
    session.register_component("credential_provider", credentials.CredentialResolver([provider]))
    if session.get_credentials() is None:
        raise ProjectStateError("configured records credentials are unavailable")
    client = session.create_client(
        "s3",
        endpoint_url=settings.endpoint,
        region_name=settings.region,
        verify=str(settings.ca_bundle) if settings.ca_bundle else certifi.where(),
        config=config_module.Config(
            signature_version="s3v4",
            proxies={},
            connect_timeout=5,
            read_timeout=15,
            retries={"mode": "standard", "total_max_attempts": 2},
            s3={"addressing_style": "path"},
            ignore_configured_endpoint_urls=True,
        ),
    )

    return cast(_ManagedRecordsClient, client)


@contextmanager
def deployment_checkpoint_evidence(
    settings: CheckpointEvidenceDeployment, project: ProjectContext
) -> Iterator[Callable[[str], SessionCheckpointEvidence]]:
    """Own one outside records client for the managed gateway's lifetime."""
    client = None
    try:
        validate_evidence_deployment(settings, project)
        project_id = project.project_id
        assert project_id is not None
        registry = load_checkpoint_signer_registry(settings.signer_registry)
        profile = registry.profile(settings.signer_profile)
        trusted = {}
        for name in (settings.signer_profile, *settings.previous_signer_profiles):
            key = load_checkpoint_public_key(registry.profile(name).trusted_public_key)
            trusted[checkpoint_public_key_fingerprint(key)] = key
        signer = profile.signer()
        client = _sdk_client(settings, project)
    except Exception:
        if client is not None:
            client.close()
        raise ProjectStateError("managed checkpoint evidence is unavailable or invalid") from None

    def bind(session_id: str) -> SessionCheckpointEvidence:
        return SessionCheckpointEvidence(
            records=S3CheckpointRecords(
                client=cast(S3RecordsClient, client),
                bucket=settings.bucket,
                prefix=settings.prefix,
                scope=CheckpointAuthorityScope(
                    settings.deployment_id, project_id, session_id, settings.retention
                ),
            ),
            signer=signer,
            trusted_keys=trusted,
            mode=settings.mode,
        )

    try:
        yield bind
    finally:
        client.close()
