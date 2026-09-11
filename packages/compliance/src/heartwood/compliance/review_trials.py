# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Prepare explicitly experimental review work from a reserved benchmark record."""

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from heartwood.compliance.evaluation_store import EvaluationStore
from heartwood.core_adapter.research_workflows import research_workflow
from heartwood.gateway import ProjectContext
from heartwood.model_policy.parallel_reviews import prepare_parallel_review_trial
from heartwood.schemas.evaluation import EvaluationRuntimeObservation, EvaluationSuite
from heartwood.schemas.parallel_reviews import ParallelReviewTrialPlan, ReviewExecutionScope
from heartwood.schemas.review import ReviewSnapshot
from heartwood.schemas.workflows import WorkflowRun


@dataclass(frozen=True)
class ReservedReviewTrial:
    """Harness-owned preparer for one reserved synthetic trial, not a project setting.

    The harness verifies pinned inputs before reserving its trial. The runtime
    observer must read its owning backend directly without acquiring gateway or
    native agent-step locks; admission runs while the parent awaits dispatch.
    """

    project: ProjectContext
    store: EvaluationStore
    trial_id: UUID
    suite: EvaluationSuite
    observe_runtime: Callable[[str], EvaluationRuntimeObservation]

    def __call__(
        self, run: WorkflowRun, snapshot: ReviewSnapshot, session_id: str, now: datetime
    ) -> ParallelReviewTrialPlan:
        """Re-read the reserved trial and observe its backend before every admission decision."""
        trial = next((item for item in self.store.records() if item.run_id == self.trial_id), None)
        if trial is None:
            raise ValueError("Experimental review reservation is unavailable")
        stage = research_workflow(run.binding.workflow_id).stage(run.stage_id)
        return prepare_parallel_review_trial(
            scope=ReviewExecutionScope(
                project_fingerprint=hashlib.sha256(str(self.project.root).encode()).hexdigest(),
                session_id=session_id,
                workflow_run_id=run.run_id,
                workflow_id=run.binding.workflow_id,
                stage_id=run.stage_id,
                revision=run.revision,
                snapshot_fingerprint=snapshot.fingerprint,
                reviewer_ids=stage.specialist_ids,
                workers=trial.configuration.specialist_concurrency,
                budget=trial.budget,
            ),
            suite=self.suite,
            trial=trial,
            runtime=self.observe_runtime(session_id),
            now=now,
        )
