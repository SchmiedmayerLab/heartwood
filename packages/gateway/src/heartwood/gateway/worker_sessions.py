# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Bind trusted platform launch callbacks to the existing gateway session lifecycle."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
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


@dataclass(frozen=True, slots=True)
class WorkerSessionRequest:
    """Trusted project selection available only to the outside platform adapter."""

    project: ProjectContext
    session_id: str
    profile: ModelProfile
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


WorkerPlanFactory = Callable[[WorkerSessionRequest], WorkerSessionPlan]


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
        self._context: WorkerConversationContext | None = None

    def context(self) -> WorkerConversationContext:
        """Launch once after retaining intent, then admit only that exact session lease."""
        if self._context is not None:
            self._context.workspace.attempt.ensure_active()
            return self._context
        if self._attempted:
            raise WorkerAttemptError("worker session requires outside reconciliation")
        self._attempted = True
        plan = self._prepare(self._request)
        self._plan = plan
        lease = self._service.launch_worker(
            plan.launch,
            image_digest=plan.image_digest,
            sandbox_policy_sha256=plan.sandbox_policy_sha256,
            input_manifest_sha256=plan.input_manifest_sha256,
        )
        try:
            context = plan.connect(lease)
            if (
                context.workspace.attempt is not lease
                or context.specialist_catalog_sha256 != self._request.specialists.fingerprint
            ):
                raise WorkerAttemptError("worker context does not match the session launch")
            lease.ensure_active()
        except BaseException:
            self._service.fence_worker_attempt(attempt_id=lease.intent.attempt_id)
            raise
        self._context = context
        return context

    def close(self) -> None:
        """Keep ownership until outside revocation and native client shutdown succeed."""
        if self._plan is not None and self._service.worker_attempt() is not None:
            self._service.revoke_worker(self._plan.revoke)
        self._service.close()
