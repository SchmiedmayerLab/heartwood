# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Prepare explicitly experimental review work from a reserved benchmark record."""

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Literal
from uuid import UUID, uuid4

from heartwood.compliance.evaluation_store import EvaluationStore
from heartwood.compliance.research import ResearchTask
from heartwood.compliance.research_runner import _read_artifacts
from heartwood.compliance.review_benchmarks import planning_review_suite, planning_review_tasks
from heartwood.core_adapter.research_workflows import research_workflow
from heartwood.gateway import ProjectContext, SessionGateway
from heartwood.model_policy.parallel_reviews import prepare_parallel_review_trial
from heartwood.schemas.evaluation import (
    EvaluationCheck,
    EvaluationConfiguration,
    EvaluationRun,
    EvaluationRuntimeObservation,
    EvaluationSuite,
)
from heartwood.schemas.execution import ExecutionBudget, ExecutionUsage
from heartwood.schemas.parallel_reviews import ParallelReviewTrialPlan, ReviewExecutionScope
from heartwood.schemas.research import AnalysisPlan
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

    def record(self) -> EvaluationRun:
        """Read the original reservation, including interrupted or completed evidence."""
        trial = next((item for item in self.store.records() if item.run_id == self.trial_id), None)
        if trial is None:
            raise ValueError("Experimental review reservation is unavailable")
        return trial

    def __call__(
        self, run: WorkflowRun, snapshot: ReviewSnapshot, session_id: str, now: datetime
    ) -> ParallelReviewTrialPlan:
        """Re-read the reserved trial and observe its backend before every admission decision."""
        trial = self.record()
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


def reserve_planning_review_trial(
    gateway: SessionGateway,
    task: ResearchTask,
    *,
    session_id: str,
    configuration: EvaluationConfiguration,
    execution: Literal["deterministic", "live_model"],
    budget: ExecutionBudget,
    seed: int = 0,
) -> ReservedReviewTrial:
    """Reserve a review of an already prepared plan in a dedicated synthetic project.

    Preparation is outside the measured review. The caller configures the gateway's
    experimental preparer before construction and supplies this returned source to
    it. Reserving neither dispatches work nor approves any proposed action.
    """
    run = validate_planning_review_project(gateway, task, session_id=session_id)
    runtime = gateway.evaluation_observation(session_id=session_id)
    if execution == "live_model" and runtime.source != "production":
        raise ValueError("Live review requires the production OpenHands backend")
    if (
        runtime.backend != "openhands-sdk"
        or runtime.tool_concurrency != 1
        or runtime.specialist_catalog_fingerprint is None
        or configuration.specialist_catalog_fingerprint != runtime.specialist_catalog_fingerprint
        or configuration.specialist_concurrency not in (1, 2)
        or runtime.declaration_mismatches(configuration)
        or configuration.runtime_fingerprint not in (None, runtime.fingerprint)
    ):
        raise ValueError("Review configuration does not match the observed specialist runtime")
    stage = research_workflow(run.binding.workflow_id).stage(run.stage_id)
    if any(value > stage.budget.model_dump()[key] for key, value in budget.model_dump().items()):
        raise ValueError("Review trial exceeds the workflow stage budget")
    observe = gateway.bind_evaluation_observer(session_id=session_id)
    if observe(session_id) != runtime:
        raise ValueError("Review runtime changed while reserving the trial")
    suite = planning_review_suite()
    now = datetime.now(UTC).replace(microsecond=0)
    pending = EvaluationRun(
        run_id=uuid4(),
        session_id=session_id,
        status="incomplete",
        suite_id=suite.suite_id,
        suite_fingerprint=suite.fingerprint,
        case_id=task.case.case_id,
        fixture_digest=task.case.fixture_digest,
        seed=seed,
        execution=execution,
        configuration=configuration.model_copy(update={"runtime_fingerprint": runtime.fingerprint}),
        runtime_observation=runtime,
        started_at=now,
        finished_at=now,
        budget=budget,
        checks=tuple(
            EvaluationCheck(**check.model_dump(), status="not_run")
            for check in task.case.required_checks
        ),
        usage=ExecutionUsage(elapsed_seconds=0, proposed_actions=0),
    )
    store = EvaluationStore(gateway.project.state_root / "evaluations")
    store.begin(pending)
    return ReservedReviewTrial(gateway.project, store, pending.run_id, suite, observe)


def validate_planning_review_project(
    gateway: SessionGateway, task: ResearchTask, *, session_id: str
) -> WorkflowRun:
    """Accept only exact maintained inputs and a settled, unreviewed planning stage."""
    if task not in planning_review_tasks():
        raise ValueError("Review trial requires an unchanged maintained case")
    if not gateway.wait_for_session_idle(session_id=session_id, timeout=0):
        raise ValueError("Review trial requires settled planning work")
    projection = gateway.session_projection(session_id=session_id)
    run = projection.workflow
    if (
        run is None
        or run.binding.workflow_id != task.case.workflow_id
        or run.stage_id != task.case.review_stage_id
        or run.research_review is not None
        or projection.pending_approval is not None
        or not any(item.control_id == "request-review" for item in projection.workflow_controls)
    ):
        raise ValueError("Review trial requires an unreviewed planning stage")
    question = AnalysisPlan.model_validate_json(task.inputs["plan.json"]).question
    if {item.input_id: item.value for item in run.binding.inputs if item.kind == "text"} != {
        "question": question
    }:
        raise ValueError("Review trial requires the maintained research question")
    planning_review_files(gateway, task, run)
    return run


def planning_review_files(
    gateway: SessionGateway, task: ResearchTask, run: WorkflowRun
) -> dict[str, str]:
    """Check the same confined fixture before and after review without scanning private state."""
    paths = {item.input_id: item.value for item in run.binding.inputs if item.kind == "file"}
    paths["plan"] = run.binding.artifact_path("plan")
    output = PurePosixPath(run.binding.output_directory)
    if len(output.parts) != 1 or paths != {
        "data": "data.csv",
        "dictionary": "dictionary.json",
        "plan": f"{output}/plan.json",
    }:
        raise ValueError("Review trial requires the standard synthetic project layout")
    expected = {
        paths[role]: task.inputs[name]
        for role, name in (
            ("data", "data.csv"),
            ("dictionary", "dictionary.json"),
            ("plan", "plan.json"),
        )
    }
    root = gateway.project.root
    directory = root / str(output)
    if (
        directory.is_symlink()
        or not directory.is_dir()
        or {item.name for item in root.iterdir()}
        != {".heartwood", "data.csv", "dictionary.json", str(output)}
        or {item.name for item in directory.iterdir()} != {"plan.json"}
        or _read_artifacts(gateway, tuple(expected)) != expected
    ):
        raise ValueError("Review trial requires only its exact synthetic fixture files")
    return expected
