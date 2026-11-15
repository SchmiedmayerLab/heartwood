# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Bind trusted platform launch callbacks to the existing gateway session lifecycle."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from threading import Event, Lock, Thread
from typing import TYPE_CHECKING

from heartwood.core_adapter import SessionService
from heartwood.core_adapter.worker_attempts import (
    WorkerAttemptError,
    WorkerAttemptIntent,
    WorkerAttemptLease,
    WorkerAttemptRecord,
    WorkerControlPlane,
    WorkerRevocationEvidence,
    WorkerRuntimeBinding,
)

if TYPE_CHECKING:
    from heartwood.gateway._model_settings import ModelProfile
    from heartwood.gateway._openhands_sdk import WorkerConversationContext
    from heartwood.gateway._project import ProjectContext
    from heartwood.gateway._skill_settings import SkillManager
    from heartwood.gateway._specialists import SpecialistCatalog
    from heartwood.schemas import PolicyProfile


@dataclass(frozen=True, slots=True)
class WorkerSessionRequest:
    """Trusted project selection available only to the outside platform adapter."""

    project: ProjectContext
    session_id: str
    profile: ModelProfile
    policy: PolicyProfile
    skills: SkillManager
    specialists: SpecialistCatalog


@dataclass(frozen=True, slots=True)
class WorkerSessionPlan:
    """Prepared authority; callbacks reuse the platform's native lifecycle and transport."""

    image_digest: str
    sandbox_policy_sha256: str
    input_manifest_sha256: str
    launch: Callable[[WorkerAttemptIntent], WorkerRuntimeBinding]
    connect: Callable[[WorkerAttemptLease], WorkerConversationContext]
    revoke: Callable[[WorkerAttemptRecord], WorkerRevocationEvidence]
    # The infrastructure the launch will use, retained in the intent before any request.
    control_plane: WorkerControlPlane
    release_preparation: Callable[[], None] | None = None


WorkerPlanFactory = Callable[[WorkerSessionRequest], WorkerSessionPlan]
# How long a caller waits for a close deferred to a launch or prompt revocation in progress.
_CLOSE_WAIT_SECONDS = 30.0
# A failed prompt revocation is retried after these waits, doubling up to the maximum.
_REVOCATION_RETRY_SECONDS = 15.0
_REVOCATION_RETRY_MAX_SECONDS = 600.0
# Revokes an attempt whose owning gateway process ended, without that process's launch state.
WorkerReconciler = Callable[[WorkerAttemptRecord], WorkerRevocationEvidence]


class ManagedWorkerSession:
    """Own one launch attempt without restoring or retrying uncertain worker access."""

    def __init__(
        self,
        service: SessionService,
        request: WorkerSessionRequest,
        prepare: WorkerPlanFactory,
    ) -> None:
        """Retain only trusted preparation callbacks until the session needs a worker."""
        self._service = service
        self._request = request
        self._prepare = prepare
        self._attempted = False
        self._plan: WorkerSessionPlan | None = None
        self._launched: WorkerAttemptIntent | None = None
        self._revocation: Thread | None = None
        self._revoking = False
        self._revocation_lock = Lock()
        self._launch_settled = Event()
        self._launch_settled.set()
        self._closing = False
        # A close that found a launch or prompt revocation in progress is finished once that
        # ends; `_close_settled` is clear until then, and `_closed` is set when a close succeeds.
        self._close_deferred = False
        self._close_settled = Event()
        self._close_settled.set()
        self._closer: Thread | None = None
        self._closed = Event()
        self._released = False
        self._close_requested = Event()
        self._context: WorkerConversationContext | None = None
        # This owner reaches a worker only through `context`, so it may replace a revoked one.
        service.admit_worker_replacement()

    def context(self) -> WorkerConversationContext:
        """Launch once after retaining intent, then admit only that exact session lease."""
        if self._context is not None:
            self._context.workspace.attempt.ensure_active()
            return self._context
        if self._attempted:
            raise WorkerAttemptError("worker session requires outside reconciliation")
        self._attempted = True
        if reason := self._service.backend.configuration_error:
            raise WorkerAttemptError(reason)
        profile = self._request.profile
        decision = self._service.policy.evaluate(
            endpoint=profile.policy_endpoint,
            capability_tier=profile.capability_tier,
            credential_reference=profile.credential_reference,
            data_handling=self._service.model_data_handling,
            decision_id="worker-launch",
            purpose="worker launch",
        )
        if decision.decision != "allow":
            raise WorkerAttemptError("worker launch is denied by session policy")
        plan = self._prepare(self._request)
        with self._revocation_lock:
            self._plan, closing = plan, self._closing
            if not closing:
                self._launch_settled.clear()
        if closing:
            # A close that began during preparation found no plan to release.
            if plan.release_preparation is not None:
                plan.release_preparation()
            raise WorkerAttemptError("worker session closed before launch")

        def launch(intent: WorkerAttemptIntent) -> WorkerRuntimeBinding:
            self._launched = intent
            return plan.launch(intent)

        try:
            lease = self._service.launch_worker(
                launch,
                image_digest=plan.image_digest,
                sandbox_policy_sha256=plan.sandbox_policy_sha256,
                input_manifest_sha256=plan.input_manifest_sha256,
                control_plane=plan.control_plane,
            )
        except BaseException:
            if self._launched is not None:
                self._revoke_promptly()
            raise
        finally:
            # A close deferred to this launch now revokes it, or this thread stops below.
            with self._revocation_lock:
                self._launch_settled.set()
                self._finish_deferred_close_locked()
        lease.on_fenced(self._revoke_promptly)
        with self._revocation_lock:
            closing = self._closing
        if closing:
            with suppress(Exception):
                self._service.fence_worker_attempt(attempt_id=lease.intent.attempt_id)
            raise WorkerAttemptError("worker session closed during launch")
        try:
            context = plan.connect(lease)
            if (
                context.workspace.attempt is not lease
                or context.specialist_catalog_sha256 != self._request.specialists.fingerprint
            ):
                raise WorkerAttemptError("worker context does not match the session launch")
            lease.ensure_active()
        except BaseException:
            # Prompt revocation may already have recorded the attempt as revoked; the
            # connection's own failure is the one to report.
            with suppress(Exception):
                self._service.fence_worker_attempt(attempt_id=lease.intent.attempt_id)
            raise
        self._context = context
        return context

    @property
    def replaceable(self) -> bool:
        """Return whether the journal records this spent owner's attempt as revoked.

        The owner launches at most once, so only a new owner may admit a replacement worker.
        Whichever path recorded the revocation, the journal is what makes the session
        replaceable.
        """
        if not self._attempted:
            return False
        current = self._service.worker_attempt()
        return (
            current is not None
            and current.phase == "revoked"
            and (self._launched is None or current.intent == self._launched)
        )

    def _revoke_promptly(self) -> None:
        """Hand a fenced attempt to its own short-lived thread; the caller that fenced returns."""
        with self._revocation_lock:
            if self._closing or self._revocation is not None:
                return
            self._revoking = True
            self._revocation = Thread(
                target=self._revoke_fenced, daemon=True, name="worker-revocation"
            )
            self._revocation.start()

    def _revoke_fenced(self) -> None:
        # Until revocation is observed the attempt stays fenced; the attempt is retried while
        # the session is open, and session close or start-up reconciliation retries it too.
        try:
            assert self._plan is not None
            delay = _REVOCATION_RETRY_SECONDS
            while True:
                with suppress(Exception):
                    self._service.revoke_worker(self._plan.revoke)
                    return
                if self._close_requested.wait(delay):
                    return
                delay = min(delay * 2, _REVOCATION_RETRY_MAX_SECONDS)
        finally:
            with self._revocation_lock:
                self._revoking = False
                self._finish_deferred_close_locked()

    def close(self) -> bool:
        """Keep ownership until outside revocation and native client shutdown succeed.

        A close never waits for a launch or prompt revocation in progress, so the caller's
        locks are not held for a control-plane call that is already running. That thread
        finishes the close once it ends, and this returns false; `wait_closed` observes
        the result. Otherwise this returns true once closed, or raises and leaves the
        session owned, and the close can be retried.
        """
        with self._revocation_lock:
            if self._closed.is_set():
                return True
            self._closing = True
            # A prompt revocation waiting to retry stops, and this close revokes instead.
            self._close_requested.set()
            if (
                not self._launch_settled.is_set()
                or self._revoking
                or (self._closer is not None and self._closer.is_alive())
            ):
                self._close_deferred = True
                self._close_settled.clear()
                return False
            self._close_deferred = False
        self._close_now()
        return True

    @property
    def closing(self) -> bool:
        """Return whether a deferred close is pending or in progress."""
        return not self._close_settled.is_set()

    def wait_closed(self, timeout: float | None = None) -> bool:
        """Wait at most ``timeout`` seconds for a deferred close; return whether it succeeded."""
        self._close_settled.wait(_CLOSE_WAIT_SECONDS if timeout is None else timeout)
        return self._closed.is_set()

    def _finish_deferred_close_locked(self) -> None:
        """Start a deferred close on its own thread once no launch or revocation remains.

        The close waits for the session command in progress, which may be the launch
        that started it, so it never runs on that thread.
        """
        if not self._close_deferred or not self._launch_settled.is_set() or self._revoking:
            return
        if self._closer is not None and self._closer.is_alive():
            return

        def finish() -> None:
            with suppress(Exception):
                self._close_now()
            with self._revocation_lock:
                self._close_deferred = False
                self._close_settled.set()

        self._closer = Thread(target=finish, daemon=True, name="worker-close")
        self._closer.start()

    def _close_now(self) -> None:
        plan = self._plan
        if plan is not None:
            # An attempt retained by another owner is left for start-up reconciliation.
            current = self._service.worker_attempt()
            if current is not None and current.intent == self._launched:
                self._service.revoke_worker(plan.revoke)
            if plan.release_preparation is not None and not self._released:
                plan.release_preparation()
                self._released = True
        self._service.close()
        self._closed.set()
