# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Trusted worker identities and their transitions in the existing session journal."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from threading import RLock
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, model_validator

from heartwood.schemas.experiments import Digest, Reference
from heartwood.session import EventKind, SessionEvent, validate_session_id

ImageDigest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]


class WorkerAttemptError(ValueError):
    """Worker identity or lifecycle cannot authorize the requested operation."""


class _WorkerRecord(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, revalidate_instances="always", hide_input_in_errors=True
    )


class WorkerAttemptIntent(_WorkerRecord):
    """Gateway-issued identity retained before contacting the sandbox control plane."""

    project_id: UUID
    session_id: str
    attempt_id: UUID
    conversation_id: UUID
    image_digest: ImageDigest
    gateway_policy_sha256: Digest
    sandbox_policy_sha256: Digest
    input_manifest_sha256: Digest

    @model_validator(mode="after")
    def _session_identity(self) -> Self:
        validate_session_id(self.session_id)
        return self


class WorkerRuntimeBinding(_WorkerRecord):
    """Runtime identity observed by the trusted launcher, never by the worker."""

    sandbox_id: Reference
    generation: Reference | None = None
    endpoint: AnyHttpUrl
    image_digest: ImageDigest
    sandbox_policy_sha256: Digest

    @model_validator(mode="after")
    def _origin(self) -> Self:
        url = self.endpoint
        if url.username or url.password or url.path != "/" or url.query or url.fragment:
            raise ValueError("worker endpoint must be a complete origin without credentials")
        return self


class WorkerRevocationEvidence(_WorkerRecord):
    """Reference to an outside observer's retained service-revocation evidence."""

    attempt_id: UUID
    sandbox_id: Reference | None
    observer_id: Reference
    evidence_sha256: Digest
    outcome: Literal["revoked", "not-created"]


class WorkerAttemptRecord(_WorkerRecord):
    """One complete lifecycle observation, containing no worker credential."""

    schema_version: Literal["heartwood.worker-attempt.v1"] = "heartwood.worker-attempt.v1"
    phase: Literal["prepared", "active", "fenced", "revoked"]
    intent: WorkerAttemptIntent
    runtime: WorkerRuntimeBinding | None = None
    revocation: WorkerRevocationEvidence | None = None

    @model_validator(mode="after")
    def _binding(self) -> Self:
        if self.phase == "prepared" and self.runtime is not None:
            raise ValueError("prepared attempts cannot claim an observed runtime")
        if self.phase == "active" and self.runtime is None:
            raise ValueError("active attempts require an observed runtime")
        if self.runtime is not None and (
            self.runtime.image_digest != self.intent.image_digest
            or self.runtime.sandbox_policy_sha256 != self.intent.sandbox_policy_sha256
        ):
            raise ValueError("runtime does not match the authorized image and policy")
        if (self.phase == "revoked") != (self.revocation is not None):
            raise ValueError("revocation evidence is required only for revoked attempts")
        if self.revocation is not None:
            evidence = self.revocation
            if evidence.attempt_id != self.intent.attempt_id:
                raise ValueError("revocation evidence belongs to another attempt")
            if self.runtime is None:
                if (evidence.sandbox_id is None) != (evidence.outcome == "not-created"):
                    raise ValueError("revoked runtimes require an observed sandbox identity")
            elif evidence.sandbox_id != self.runtime.sandbox_id or evidence.outcome != "revoked":
                raise ValueError("revocation evidence belongs to another runtime")
        return self


def validate_worker_transition(
    previous: WorkerAttemptRecord | None, current: WorkerAttemptRecord
) -> None:
    """Reject lifecycle skips and changes to a retained execution identity."""
    if current.phase == "prepared":
        if previous is not None and previous.phase != "revoked":
            raise WorkerAttemptError("previous worker access has not been revoked")
        if previous is not None and (
            current.intent.project_id != previous.intent.project_id
            or current.intent.session_id != previous.intent.session_id
            or current.intent.conversation_id != previous.intent.conversation_id
        ):
            raise WorkerAttemptError("replacement changed the project, session, or conversation")
        return
    if previous is None or current.intent != previous.intent:
        raise WorkerAttemptError("worker attempt identity changed")
    allowed = {"prepared": {"active", "fenced"}, "active": {"fenced"}, "fenced": {"revoked"}}
    if current.phase not in allowed.get(previous.phase, set()):
        raise WorkerAttemptError("worker attempt transition is not authorized")
    if previous.runtime is not None and current.runtime != previous.runtime:
        raise WorkerAttemptError("worker runtime identity changed")
    if previous.runtime is None and current.runtime is not None and current.phase != "active":
        raise WorkerAttemptError("worker runtime was not bound before fencing")


def current_worker_attempt(events: Sequence[SessionEvent]) -> WorkerAttemptRecord | None:
    """Reduce verified journal events; replay itself grants no live capability."""
    current = None
    attempts: set[UUID] = set()
    for event in events:
        if event.kind != EventKind.WORKER_ATTEMPT_RECORDED:
            continue
        candidate = WorkerAttemptRecord.model_validate(event.payload)
        if candidate.intent.session_id != event.session_id:
            raise WorkerAttemptError("worker attempt belongs to another session")
        if current is not None and candidate.intent.project_id != current.intent.project_id:
            raise WorkerAttemptError("worker attempt belongs to another project")
        validate_worker_transition(current, candidate)
        if candidate.phase == "prepared":
            if candidate.intent.attempt_id in attempts:
                raise WorkerAttemptError("worker attempt identity was reused")
            attempts.add(candidate.intent.attempt_id)
        current = candidate
    return current


class WorkerAttemptLease:
    """Process-local admission backed by a retained binding and session writer ownership."""

    def __init__(self, record: WorkerAttemptRecord, *, owns_writer: Callable[[], bool]) -> None:
        """Create admission only after a trusted binding is durably recorded."""
        if record.phase != "active" or record.runtime is None or not owns_writer():
            raise WorkerAttemptError("worker admission requires an owned active binding")
        self._intent = record.intent
        self._runtime = record.runtime
        self._owns_writer = owns_writer
        self._fenced = False
        self._runtime_observer: Callable[[WorkerRuntimeBinding], None] | None = None
        self._lock = RLock()

    @property
    def intent(self) -> WorkerAttemptIntent:
        """Return the immutable identity retained before launch."""
        return self._intent

    @property
    def runtime(self) -> WorkerRuntimeBinding:
        """Return the immutable runtime observed by the launcher."""
        return self._runtime

    @property
    def active(self) -> bool:
        """Observe local admission; this does not attest that a remote runtime stopped."""
        with self._lock:
            return not self._fenced and self._owns_writer()

    def ensure_active(self) -> None:
        """Reject stale work before dispatch and again before admitting its result."""
        if not self.active:
            raise WorkerAttemptError("worker attempt admission is closed")
        if self._runtime_observer is not None:
            try:
                self._runtime_observer(self._runtime)
            except Exception:
                self.fence()
                raise WorkerAttemptError(
                    "native runtime observation closed worker admission"
                ) from None
            if not self.active:
                raise WorkerAttemptError("worker attempt admission is closed")

    def require_runtime_observation(self, observer: Callable[[WorkerRuntimeBinding], None]) -> None:
        """Bind one outside runtime observer before admitting the worker connection."""
        with self._lock:
            if self._runtime_observer is not None:
                raise WorkerAttemptError("worker runtime observer cannot be replaced")
            self.ensure_active()
            self._runtime_observer = observer
            self.ensure_active()

    def fence(self) -> None:
        """Permanently close this capability without claiming infrastructure revocation."""
        with self._lock:
            self._fenced = True

    @contextmanager
    def admit_callback(self) -> Iterator[None]:
        """Serialize a bounded callback with fencing; never hold across worker I/O."""
        with self._lock:
            self.ensure_active()
            yield
