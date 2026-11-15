# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Immutable checkpoint records using an operator-provided S3 SDK client."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any, Protocol
from uuid import UUID

from heartwood.audit._checkpoint import (
    AuditCheckpointError,
    _canonical_checkpoint,
    _verify_experiment_content,
)
from heartwood.audit._continuity import RetainedCheckpoint, checkpoint_context
from heartwood.audit._log import verify_audit_jsonl
from heartwood.schemas import AuditCheckpoint, AuditCheckpointStatement, AuditRetention
from heartwood.schemas.experiments import ExperimentExportBinding

_MAXIMUM_AUDIT_BYTES = 64 * 1024 * 1024
_MAXIMUM_CHECKPOINT_BYTES = 64 * 1024


class RecordsUnavailableError(RuntimeError):
    """The outside records service could not complete an operation."""


class _MissingObjectError(AuditCheckpointError):
    """An expected object has no retained version."""


def _request(operation: Callable[..., Mapping[str, Any]], **arguments: object) -> Mapping[str, Any]:
    try:
        return operation(**arguments)
    except Exception as error:
        raise RecordsUnavailableError("external records service is unavailable") from error


class S3RecordsClient(Protocol):
    """The existing SDK calls used at the external records boundary."""

    def list_object_versions(self, **kwargs: object) -> Mapping[str, Any]: ...

    def get_object(self, **kwargs: object) -> Mapping[str, Any]: ...

    def put_object(self, **kwargs: object) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class CheckpointAuthorityScope:
    """Trusted scope selected after authenticating the requesting deployment.

    Construct this from operator configuration, never from a signing request.
    Each authorization binds one project, session and exact retention declaration.
    """

    deployment_id: str
    project_id: UUID
    session_id: str
    retention: AuditRetention

    def authorize(self, statement: AuditCheckpointStatement) -> None:
        if (
            statement.deployment_id != self.deployment_id
            or statement.session_id != self.session_id
            or statement.retention != self.retention
        ):
            raise AuditCheckpointError("checkpoint statement is outside the authorized scope")

    @property
    def retention_deadline(self) -> datetime:
        """Retain through the complete declared UTC date, rather than its first instant."""
        return datetime.combine(
            datetime.strptime(self.retention.retain_until, "%Y-%m-%d").date() + timedelta(days=1),
            time.min,
            tzinfo=UTC,
        )


class S3CheckpointRecords:
    """Use versioned objects, compliance retention and conditional publication.

    The operator supplies an authenticated SDK client with a fixed endpoint and
    bucket. Bucket policy must deny deletion and unconditional writes. Worker
    credentials must grant no access here; a gateway publisher needs only its
    candidate prefix, while the signing authority owns the checkpoint prefix.
    """

    def __init__(
        self,
        *,
        client: S3RecordsClient,
        bucket: str,
        prefix: str,
        scope: CheckpointAuthorityScope,
    ) -> None:
        if not bucket or not re.fullmatch(r"[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*", prefix):
            raise ValueError("records bucket and fixed prefix are required")
        identity = json.dumps(
            [scope.deployment_id, str(scope.project_id), scope.session_id],
            separators=(",", ":"),
        ).encode()
        self._client = client
        self._bucket = bucket
        self._prefix = f"{prefix}/{hashlib.sha256(identity).hexdigest()}"
        self.scope = scope

    def stage_audit(
        self,
        statement: AuditCheckpointStatement,
        audit_content: str,
        *,
        experiment_content: str | None = None,
    ) -> None:
        """Retain candidate bytes before submitting the existing signing request."""
        self.scope.authorize(statement)
        context = checkpoint_context(statement, audit_content)
        if context.project_id != self.scope.project_id:
            raise AuditCheckpointError("checkpoint candidate belongs to another project")
        events, _ = verify_audit_jsonl(audit_content)
        binding = _verify_experiment_content(events, experiment_content)
        if binding is not None and experiment_content is not None:
            self._put_immutable(
                self._experiment_key(binding), experiment_content.encode(), _MAXIMUM_AUDIT_BYTES
            )
        content = audit_content.encode("utf-8")
        if len(content) > _MAXIMUM_AUDIT_BYTES:
            raise AuditCheckpointError("checkpoint audit exceeds the records size limit")
        self._put_immutable(self._audit_key(statement), content, _MAXIMUM_AUDIT_BYTES)

    def audit_content(self, statement: AuditCheckpointStatement) -> str:
        """Fetch exact bytes by digest from the fixed records namespace."""
        self.scope.authorize(statement)
        content = self._read_unique(self._audit_key(statement), _MAXIMUM_AUDIT_BYTES)
        if "sha256:" + hashlib.sha256(content).hexdigest() != statement.audit_content_sha256:
            raise AuditCheckpointError("retained audit bytes do not match their digest")
        decoded = content.decode("utf-8")
        events, _ = verify_audit_jsonl(decoded)
        checkpoint_context(statement, decoded)
        raw = events[-1].payload.get("experiment_export")
        experiment_content = None
        if raw is not None:
            binding = ExperimentExportBinding.model_validate(raw)
            experiment_content = self._read_unique(
                self._experiment_key(binding), _MAXIMUM_AUDIT_BYTES
            ).decode("utf-8")
        _verify_experiment_content(events, experiment_content)
        return decoded

    def history(self) -> tuple[RetainedCheckpoint, ...]:
        """Read every retained ordinal, rejecting gaps, hidden versions and delete markers."""
        prefix = f"{self._prefix}/checkpoints/"
        versions = self._versions(prefix)
        history: list[RetainedCheckpoint] = []
        for ordinal, key in enumerate(sorted(versions), start=1):
            if key != self._checkpoint_key(ordinal):
                raise AuditCheckpointError(
                    "retained checkpoint history has a gap or unknown object"
                )
            content = self._read_version(key, versions[key], _MAXIMUM_CHECKPOINT_BYTES)
            checkpoint = AuditCheckpoint.model_validate_json(content)
            if content != _canonical_checkpoint(checkpoint).encode("utf-8"):
                raise AuditCheckpointError("retained checkpoint is not canonical")
            self.scope.authorize(checkpoint.statement)
            audit_content = self.audit_content(checkpoint.statement)
            context = checkpoint_context(checkpoint.statement, audit_content)
            if context.sequence != ordinal or context.project_id != self.scope.project_id:
                raise AuditCheckpointError("retained checkpoint scope or ordinal is invalid")
            history.append(RetainedCheckpoint(checkpoint, audit_content))
        return tuple(history)

    def publish(self, checkpoint: AuditCheckpoint) -> RetainedCheckpoint:
        """Publish one ordinal atomically, then independently read back its retained bytes.

        A concurrent writer or lost response can leave the ordinal occupied.
        Reconcile the exact statement; never replace an existing version.
        The authority must verify the returned signature before acknowledging it.
        """
        self.scope.authorize(checkpoint.statement)
        audit_content = self.audit_content(checkpoint.statement)
        context = checkpoint_context(checkpoint.statement, audit_content)
        key = self._checkpoint_key(context.sequence)
        content = _canonical_checkpoint(checkpoint).encode("utf-8")
        self._put_immutable(key, content, _MAXIMUM_CHECKPOINT_BYTES, reconcile_statement=True)
        retained = AuditCheckpoint.model_validate_json(
            self._read_unique(key, _MAXIMUM_CHECKPOINT_BYTES)
        )
        if retained.statement != checkpoint.statement:
            raise AuditCheckpointError("checkpoint publication conflicts with retained history")
        return RetainedCheckpoint(retained, audit_content)

    def _checkpoint_key(self, sequence: int) -> str:
        return f"{self._prefix}/checkpoints/{sequence:020d}.json"

    def _audit_key(self, statement: AuditCheckpointStatement) -> str:
        digest = statement.audit_content_sha256.removeprefix("sha256:")
        return f"{self._prefix}/candidates/{digest}.jsonl"

    def _experiment_key(self, binding: ExperimentExportBinding) -> str:
        return f"{self._prefix}/experiments/{binding.sha256}.jsonl"

    def _versions(self, prefix: str) -> dict[str, str]:
        versions: dict[str, str] = {}
        request: dict[str, object] = {"Bucket": self._bucket, "Prefix": prefix}
        seen_markers: set[tuple[str, str]] = set()
        while True:
            response = _request(self._client.list_object_versions, **request)
            if response.get("DeleteMarkers"):
                raise AuditCheckpointError("records namespace contains a delete marker")
            for item in response.get("Versions", []):
                key, version = item.get("Key"), item.get("VersionId")
                if (
                    not isinstance(key, str)
                    or not key.startswith(prefix)
                    or key in versions
                    or not isinstance(version, str)
                    or not version
                    or version == "null"
                    or item.get("IsLatest") is not True
                ):
                    raise AuditCheckpointError("records namespace has ambiguous object versions")
                versions[key] = version
            if response.get("IsTruncated") is False:
                return versions
            key_marker = response.get("NextKeyMarker")
            version_marker = response.get("NextVersionIdMarker")
            if (
                response.get("IsTruncated") is not True
                or not isinstance(key_marker, str)
                or not isinstance(version_marker, str)
                or not key_marker
                or (key_marker, version_marker) in seen_markers
            ):
                raise AuditCheckpointError("records version pagination is invalid")
            seen_markers.add((key_marker, version_marker))
            request.update(KeyMarker=key_marker, VersionIdMarker=version_marker)

    def _read_unique(self, key: str, maximum_bytes: int) -> bytes:
        versions = self._versions(key)
        if not versions:
            raise _MissingObjectError("required retained object is unavailable")
        if set(versions) != {key}:
            raise AuditCheckpointError("required retained object is unavailable or ambiguous")
        return self._read_version(key, versions[key], maximum_bytes)

    def _read_version(self, key: str, version: str, maximum_bytes: int) -> bytes:
        response = _request(
            self._client.get_object, Bucket=self._bucket, Key=key, VersionId=version
        )
        body = response["Body"]
        try:
            deadline = response.get("ObjectLockRetainUntilDate")
            if (
                response.get("VersionId") != version
                or response.get("ObjectLockMode") != "COMPLIANCE"
                or not isinstance(deadline, datetime)
                or deadline.tzinfo is None
                or deadline < self.scope.retention_deadline
            ):
                raise AuditCheckpointError("records retention is absent, shortened or unverified")
            try:
                content = body.read(maximum_bytes + 1)
            except Exception as error:
                raise RecordsUnavailableError("external records read was interrupted") from error
            if (
                not isinstance(content, bytes)
                or len(content) > maximum_bytes
                or response.get("ContentLength") != len(content)
            ):
                raise AuditCheckpointError("retained object is oversized or incomplete")
            return content
        finally:
            body.close()

    def _put_immutable(
        self, key: str, content: bytes, maximum_bytes: int, *, reconcile_statement: bool = False
    ) -> None:
        if len(content) > maximum_bytes:
            raise AuditCheckpointError("checkpoint object exceeds the records size limit")
        # An SDK exception can follow server acceptance. Only verified retained
        # bytes may reconcile it; an absent, conflicting or unlocked object fails.
        with suppress(RecordsUnavailableError):
            _request(
                self._client.put_object,
                Bucket=self._bucket,
                Key=key,
                Body=content,
                ContentType="application/json" if reconcile_statement else "application/x-ndjson",
                ChecksumAlgorithm="SHA256",
                IfNoneMatch="*",
                ObjectLockMode="COMPLIANCE",
                ObjectLockRetainUntilDate=self.scope.retention_deadline,
            )
        try:
            retained = self._read_unique(key, maximum_bytes)
        except _MissingObjectError as error:
            raise RecordsUnavailableError(
                "external records publication was not observed"
            ) from error
        if retained == content:
            return
        if reconcile_statement:
            previous = AuditCheckpoint.model_validate_json(retained)
            proposed = AuditCheckpoint.model_validate_json(content)
            if (
                retained == _canonical_checkpoint(previous).encode("utf-8")
                and previous.statement == proposed.statement
            ):
                return
        raise AuditCheckpointError("immutable publication conflicts with retained bytes")
