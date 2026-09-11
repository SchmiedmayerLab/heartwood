# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Use deployment-owned retained evidence through the existing review admission contract."""

import hashlib
from datetime import datetime
from pathlib import Path

from heartwood.core_adapter.research_workflows import research_workflow
from heartwood.model_policy.parallel_reviews import prepare_parallel_review
from heartwood.persistence import read_private_bytes
from heartwood.schemas.evaluation import EvaluationRuntimeObservation
from heartwood.schemas.execution import ExecutionBudget
from heartwood.schemas.parallel_reviews import (
    ParallelReviewPlan,
    ReviewExecutionScope,
    ReviewQualifications,
)
from heartwood.schemas.review import ReviewSnapshot
from heartwood.schemas.workflows import WorkflowRun

QUALIFICATION_ENV = "HEARTWOOD_REVIEW_QUALIFICATIONS"


def load_review_qualifications(path: Path, *, project_root: Path) -> ReviewQualifications:
    """Re-read bounded private input; project files and link substitutions cannot grant scope."""
    try:
        if not path.is_absolute() or path.is_symlink():
            raise ValueError("Qualification path must be absolute and not a link")
        resolved = path.resolve(strict=True)
        if resolved == project_root or project_root in resolved.parents:
            raise ValueError("Qualification evidence must be outside the project")
        if resolved.stat().st_mode & 0o022:
            raise ValueError("Qualification evidence must not be writable by other users")
        return ReviewQualifications.model_validate_json(
            read_private_bytes(resolved, max_bytes=8 * 1024 * 1024)
        )
    except (OSError, ValueError):
        raise ValueError("Deployment review evidence is unavailable or invalid") from None


def qualified_review_plan(
    *,
    evidence: ReviewQualifications,
    project_root: Path,
    run: WorkflowRun,
    snapshot: ReviewSnapshot,
    session_id: str,
    runtime: EvaluationRuntimeObservation,
    now: datetime,
) -> ParallelReviewPlan:
    """Select exactly one matching route and reuse the full retained-trial policy assessment."""
    stage = research_workflow(run.binding.workflow_id).stage(run.stage_id)
    matches = []
    for route in evidence.routes:
        case = next((case for case in route.suite.cases if case.case_id == route.case_id), None)
        if (
            case is not None
            and case.workflow_id == run.binding.workflow_id
            and case.review_stage_id == run.stage_id
            and set(case.specialist_ids) == set(stage.specialist_ids)
            and route.sequential.runtime_fingerprint == runtime.fingerprint
            and route.parallel.runtime_fingerprint == runtime.fingerprint
        ):
            matches.append(route)
    if len(matches) != 1:
        raise ValueError("No unique qualified review route matches this session and stage")
    route = matches[0]
    # Constrain the offered work to both the maintained stage and every retained
    # trial of this case. The policy assessor still checks trial eligibility.
    budgets = [
        stage.budget,
        *(trial.budget for trial in route.runs if trial.case_id == route.case_id),
    ]
    budget = ExecutionBudget.model_validate(
        {key: min(item.model_dump()[key] for item in budgets) for key in stage.budget.model_dump()}
    )
    return prepare_parallel_review(
        scope=ReviewExecutionScope(
            project_fingerprint=hashlib.sha256(str(project_root).encode()).hexdigest(),
            session_id=session_id,
            workflow_run_id=run.run_id,
            workflow_id=run.binding.workflow_id,
            stage_id=run.stage_id,
            revision=run.revision,
            snapshot_fingerprint=snapshot.fingerprint,
            reviewer_ids=stage.specialist_ids,
            workers=route.parallel.specialist_concurrency,
            budget=budget,
        ),
        case_id=route.case_id,
        suite=route.suite,
        sequential=route.sequential,
        parallel=route.parallel,
        runtime=runtime,
        configuration=route.sequential,
        runs=route.runs,
        policy=route.policy,
        now=now,
    )
