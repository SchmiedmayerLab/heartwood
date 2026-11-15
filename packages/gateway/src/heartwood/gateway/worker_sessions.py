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
    release_preparation: Callable[[], None] | None = None


WorkerPlanFactory = Callable[[WorkerSessionRequest], WorkerSessionPlan]
# How long a close waits for a launch or a prompt revocation already in progress.
_CLOSE_WAIT_SECONDS = 30.0
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
        self._revocation_lock = Lock()
        self._launch_settled = Event()
        self._launch_settled.set()
        self._closing = False
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
            )
        except BaseException:
            if self._launched is not None:
                self._revoke_promptly()
            raise
        finally:
            # A close waiting for this launch now revokes it, or this thread stops below.
            self._launch_settled.set()
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

    def _revoke_promptly(self) -> None:
        """Hand a fenced attempt to its own short-lived thread; the caller that fenced returns."""
        with self._revocation_lock:
            if self._closing or self._revocation is not None:
                return
            self._revocation = Thread(
                target=self._revoke_fenced, daemon=True, name="worker-revocation"
            )
            self._revocation.start()

    def _revoke_fenced(self) -> None:
        # A failure leaves the attempt fenced for session close or start-up reconciliation.
        with suppress(Exception):
            assert self._plan is not None
            self._service.revoke_worker(self._plan.revoke)

    def close(self) -> None:
        """Keep ownership until outside revocation and native client shutdown succeed.

        A launch or prompt revocation still in progress after a bounded wait leaves the
        session owned, and the close can be retried; the caller's locks are never held
        for longer than that wait.
        """
        with self._revocation_lock:
            self._closing = True
        if not self._launch_settled.wait(_CLOSE_WAIT_SECONDS):
            raise WorkerAttemptError("worker launch is still in progress")
        with self._revocation_lock:
            revocation, plan = self._revocation, self._plan
        if revocation is not None:
            revocation.join(_CLOSE_WAIT_SECONDS)
            if revocation.is_alive():
                raise WorkerAttemptError("worker revocation is still in progress")
        if plan is not None:
            # An attempt retained by another owner is left for start-up reconciliation.
            current = self._service.worker_attempt()
            if current is not None and current.intent == self._launched:
                self._service.revoke_worker(plan.revoke)
            if plan.release_preparation is not None:
                plan.release_preparation()
        self._service.close()
