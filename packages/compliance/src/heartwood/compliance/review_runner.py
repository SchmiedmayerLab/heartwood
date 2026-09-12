# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Retained sequential and experimental review trials through normal gateway controls."""

from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType

from heartwood.compliance.replay_evidence import replay_evidence
from heartwood.compliance.research_runner import (
    ResearchTrial,
    ReviewDecision,
    _drive,
    _fresh_replay,
    _reviewed_execution,
    _TrialSession,
)
from heartwood.compliance.review_benchmarks import (
    planning_review_suite,
    planning_review_tasks,
    verify_planning_review,
)
from heartwood.compliance.review_trials import (
    ReservedReviewTrial,
    planning_review_files,
    validate_planning_review_project,
)
from heartwood.core_adapter.workflow_runtime import workflow_review_outcome
from heartwood.gateway import ProjectionApprovalGroup, SessionGateway, SessionProjection
from heartwood.schemas.evaluation import EvaluationCheck, EvaluationRun
from heartwood.session import CommandKind


def run_planning_review_trial(
    gateway: SessionGateway,
    reservation: ReservedReviewTrial,
    *,
    review: Callable[[ProjectionApprovalGroup], ReviewDecision],
    observe: Callable[[SessionProjection], None] | None = None,
) -> ResearchTrial:
    """Measure only review work, retaining failures and requiring explicit grouped consent.

    The gateway must already use the reservation as its experimental preparer for
    parallel trials. Model/hardware declarations are not server attestation. A
    successful synthesis check proves a final structured parent outcome after all
    reviewers, not the scientific correctness of free-form narrative.
    """
    pending = reservation.record()
    task = next(
        (item for item in planning_review_tasks() if item.case.case_id == pending.case_id), None
    )
    if (
        task is None
        or pending.status != "incomplete"
        or pending.session_id is None
        or reservation.project != gateway.project
        or reservation.suite != planning_review_suite()
        or pending.suite_id != reservation.suite.suite_id
        or pending.suite_fingerprint != reservation.suite.fingerprint
        or task.case not in reservation.suite.cases
        or pending.fixture_digest != task.case.fixture_digest
        or pending.runtime_observation is None
    ):
        raise ValueError("Review execution requires its exact incomplete reservation")
    session_id = pending.session_id
    workflow = validate_planning_review_project(gateway, task, session_id=session_id)
    original = planning_review_files(gateway, task, workflow)
    before = gateway.session_projection(session_id=session_id)
    runtime = reservation.observe_runtime(session_id)
    if (
        runtime != pending.runtime_observation
        or pending.configuration.runtime_fingerprint != runtime.fingerprint
        or runtime.declaration_mismatches(pending.configuration)
        or pending.configuration.specialist_concurrency not in (1, 2)
    ):
        raise ValueError("Review runtime changed after reservation")
    # Reservation waiting consumes elapsed budget, but bootstrap model work does not.
    age = (datetime.now(UTC) - pending.started_at).total_seconds()
    if age < 0 or age >= pending.budget.maximum_seconds:
        raise ValueError("Review reservation is not current")
    remaining_budget = pending.budget.model_copy(
        update={"maximum_seconds": pending.budget.maximum_seconds - age}
    )
    started = time.monotonic()
    session = _TrialSession(
        gateway,
        session_id,
        pending.run_id,
        pending.started_at.isoformat(),
        runtime,
        usage_baseline=before.execution_usage(elapsed_seconds=0),
    )
    try:
        workers = pending.configuration.specialist_concurrency
        controls = (
            ("prepare-parallel-review", "request-parallel-review")
            if workers > 1
            else ("request-review",)
        )
        for control_id in controls:
            projection = gateway.session_projection(session_id=session_id)
            if session.usage(projection, started).exhausted_limits(remaining_budget):
                raise TimeoutError("Review budget expired before dispatch")
            control = next(
                (item for item in projection.workflow_controls if item.control_id == control_id),
                None,
            )
            if control is None:
                raise ValueError("Required review control is not available")
            session.command(
                control_id, CommandKind.WORKFLOW, control.request.model_dump(mode="json")
            )
        stop = _drive(session, review, remaining_budget, started, observe)
        if not gateway.wait_for_session_idle(session_id=session_id, timeout=30):
            raise TimeoutError("Review did not reach a settled execution boundary")
        session.verify_runtime()
        projected = gateway.session_projection(session_id=session_id)
        measured = session.usage(projected, started)
        if measured.exceeded_limits(remaining_budget):
            stop = "budget-exceeded"
        session.command("audit", CommandKind.AUDIT_EXPORT, {})
        gateway.audit_export(session_id)
        replay_passed = _fresh_replay(gateway, session_id, replay_evidence(gateway, session_id))
        try:
            unchanged = planning_review_files(gateway, task, workflow) == original
        except (OSError, ValueError):
            unchanged = False
        new = projected.model_copy(
            update={
                "actions": tuple(
                    item for item in projected.actions if item.proposed_sequence > before.revision
                ),
                "subagents": tuple(
                    item
                    for item in projected.subagents
                    if item.invocation_id not in {prior.invocation_id for prior in before.subagents}
                ),
            }
        )
        states = _review_checks(new, workers=workers, original=original, gateway=gateway)
        current_review = (
            projected.workflow.research_review if projected.workflow is not None else None
        )
        states["review.findings"] = (
            current_review is not None
            and verify_planning_review(task, current_review).status == "passed"
        )
        states["review.artifacts"] = unchanged
        states["review.isolation"] &= unchanged and _reviewed_execution(
            new, session.approved_action_ids
        )
        states["review.lineage"] &= replay_passed
        states["review.synthesis"] &= (
            stop == "finished"
            and projected.workflow is not None
            and workflow_review_outcome(
                gateway.replay_events(session_id=session_id), projected.workflow
            )
            == "success"
        )
        record = EvaluationRun.model_validate(
            {
                **pending.model_dump(),
                "status": "completed",
                "finished_at": datetime.now(UTC),
                "usage": measured,
                "checks": tuple(
                    EvaluationCheck(
                        check_id=check.check_id,
                        dimension=check.dimension,
                        status="passed" if states.get(check.check_id, False) else "failed",
                    )
                    for check in task.case.required_checks
                ),
            }
        )
        reservation.store.complete(record)
        return ResearchTrial(record=record, stop=stop, artifacts=MappingProxyType({}))
    except BaseException:
        session.pause()
        raise


def _review_checks(
    projection: SessionProjection,
    *,
    workers: int,
    original: dict[str, str],
    gateway: SessionGateway,
) -> dict[str, bool]:
    """Observe typed task identities and ordering, not model claims about execution."""
    run = projection.workflow
    review = run.research_review if run is not None else None
    roles = set(review.reviewer_ids) if review is not None else set()
    tasks = tuple(item for item in projection.actions if item.details.kind == "task")
    children = projection.subagents
    lineage = (
        bool(roles)
        and len(tasks) == len(children) == len(roles)
        and (
            {item.agent_name for item in children} == roles
            and len({item.task_id for item in children}) == len(children)
            and all(
                item.task_id is not None
                and item.status == "completed"
                and item.parent_session_id == projection.session_id
                for item in children
            )
            and all(
                sum(
                    child.parent_action_id == action.action_id
                    and child.invocation_id == action.tool_call_id
                    for child in children
                )
                == 1
                for action in tasks
            )
            and all(action.state == "succeeded" for action in tasks)
        )
    )
    intervals = tuple(
        item.native_execution for item in children if item.native_execution is not None
    )
    schedule = False
    if lineage and len(intervals) == 2:
        with suppress(ValueError):
            schedule = (intervals[0].overlap_seconds(intervals[1]) > 0) == (workers == 2)
    allowed_paths = {str(gateway.project.root / name) for name in original} | set(original)
    isolated = all(
        action.tool_name in ("finish", "think", "task_tracker")
        or (
            action.details.kind == "task"
            and action.details.capability == "advisory"
            and action.details.subagent_type in roles
        )
        or (
            action.details.kind == "file-editor"
            and action.details.operation == "view"
            and action.details.path is not None
            and str(Path(action.details.path)) in allowed_paths
        )
        for action in projection.actions
        if action.outcome is not None
    )
    synthesis = lineage and any(
        message.role == "agent" and all(message.sequence > task.updated_sequence for task in tasks)
        for message in projection.conversation
    )
    return {
        "review.connectivity": projection.context.model_decision == "allow" and bool(tasks),
        "review.schedule": schedule,
        "review.lineage": lineage,
        "review.structured-results": lineage
        and all(item.review_proposals is not None for item in children),
        "review.isolation": isolated,
        "review.synthesis": synthesis,
    }
