# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Content-free usage counts derived from verified session and experiment records."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from importlib.metadata import version
from uuid import UUID

from heartwood.core_adapter import FileSessionStore, SessionOwnershipError
from heartwood.core_adapter.workflow_provenance import workflow_experiment_events
from heartwood.gateway._experiment_store import ExperimentStore
from heartwood.gateway._project import ProjectContext, ProjectStateError
from heartwood.schemas.experiments import (
    ExperimentEvent,
    ExperimentRun,
    reduce_experiment_events,
)
from heartwood.schemas.usage import (
    ActionSetCounts,
    ExperimentRunCounts,
    ProjectUsageCounts,
    ProjectUsageSummary,
    ReproductionCounts,
    WorkflowRunCounts,
)
from heartwood.schemas.workflows import WorkflowRun
from heartwood.session import EventKind, SessionEvent, validate_session_id


def project_usage_summary(project: ProjectContext, *, platform_id: str) -> ProjectUsageSummary:
    """Summarize recorded use without writing records or contacting a model route.

    Every session's paired history is fully verified first; any failure reports no
    counts. The summary copies no prompt, path, file name, title, output or identifier.
    """
    if not project.state_exists():
        raise ProjectStateError("project state is not initialized; there is no usage to summarize")
    summary_id = project.usage_summary_id()
    release_version = version("heartwood-gateway")
    try:
        sessions = _verified_sessions(project)
        counts = _counts(project, sessions)
    except (ValueError, OSError, SessionOwnershipError):
        return ProjectUsageSummary(
            summary_id=summary_id,
            release_version=release_version,
            platform_id=platform_id,
            audit_verification="failed",
            counts=None,
        )
    return ProjectUsageSummary(
        summary_id=summary_id,
        release_version=release_version,
        platform_id=platform_id,
        audit_verification="verified",
        counts=counts,
    )


def _verified_sessions(project: ProjectContext) -> tuple[tuple[SessionEvent, ...], ...]:
    sessions: list[tuple[SessionEvent, ...]] = []
    for candidate in sorted(project.sessions_dir.iterdir()):
        if candidate.is_symlink() or not candidate.is_dir():
            continue
        try:
            session_id = validate_session_id(candidate.name)
        except ValueError:
            continue
        # Replay verifies the complete paired session and audit history.
        sessions.append(FileSessionStore(project.sessions_dir, session_id).replay_events())
    return tuple(sessions)


def _counts(
    project: ProjectContext, sessions: Sequence[tuple[SessionEvent, ...]]
) -> ProjectUsageCounts:
    events = tuple(event for session in sessions for event in session)
    decisions = Counter(
        event.payload.get("decision")
        for event in events
        if event.kind == EventKind.APPROVAL_RECORDED
    )
    workflows: dict[tuple[str, str], str] = {}
    for event in events:
        if event.kind == EventKind.WORKFLOW_UPDATED:
            run = WorkflowRun.model_validate(event.payload["run"])
            workflows[(event.session_id, run.run_id)] = run.phase
    reproductions = Counter(
        event.payload.get("status")
        for event in events
        if event.kind == EventKind.WORKFLOW_EXECUTION_RECORDED
    )
    phases: Counter[str] = Counter(workflows.values())
    experiments: Counter[str] = Counter(run.status for run in _experiment_runs(project, sessions))
    return ProjectUsageCounts(
        sessions=len(sessions),
        turns=sum(event.kind == EventKind.USER_MESSAGE_RECORDED for event in events),
        action_sets=ActionSetCounts(allowed=decisions["approved"], rejected=decisions["denied"]),
        workflow_runs=WorkflowRunCounts(
            **{phase: phases[phase] for phase in WorkflowRunCounts.model_fields}
        ),
        reproductions=ReproductionCounts(
            succeeded=reproductions["succeeded"],
            failed=reproductions["failed"],
            unsettled=max(
                0,
                reproductions["prepared"] - reproductions["succeeded"] - reproductions["failed"],
            ),
        ),
        experiment_runs=ExperimentRunCounts(
            **{status: experiments[status] for status in ExperimentRunCounts.model_fields}
        ),
    )


def _experiment_runs(
    project: ProjectContext, sessions: Sequence[tuple[SessionEvent, ...]]
) -> tuple[ExperimentRun, ...]:
    """Reduce recorded and not yet materialized experiment events without appending them."""
    path = project.state_root / "experiments.jsonl"
    recorded: dict[UUID, ExperimentEvent] = (
        {event.event_id: event for event in ExperimentStore(path).events()} if path.exists() else {}
    )
    pending: list[ExperimentEvent] = []
    for session in sessions:
        for event in workflow_experiment_events(session):
            existing = recorded.get(event.event_id)
            if existing is None:
                pending.append(event)
            elif existing != event:
                raise ValueError("Experiment record differs from its session source")
    return reduce_experiment_events((*recorded.values(), *pending))
