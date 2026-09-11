# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Deployment evidence is bounded input; ordinary project state cannot enable parallel work."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from heartwood.core_adapter.research_workflows import research_workflow
from heartwood.gateway import ProjectContext, SessionGateway
from heartwood.gateway._review_qualification import (
    load_review_qualifications,
    qualified_review_plan,
)
from heartwood.schemas.evaluation import EvaluationRuntimeObservation
from heartwood.schemas.parallel_reviews import ParallelReviewPlan, ReviewQualifications
from heartwood.schemas.review import ReviewSnapshot
from heartwood.schemas.workflows import WorkflowBoundInput, WorkflowProjectBinding, WorkflowRun


def runtime() -> EvaluationRuntimeObservation:
    return EvaluationRuntimeObservation(
        backend="openhands-sdk",
        source="production",
        request_model="synthetic/model",
        openhands_version="1.46.0",
        model_options_fingerprint="a" * 64,
        platform="generic",
        policy_fingerprint="b" * 64,
        action_confirmation="always-confirm",
        max_input_tokens=32768,
        max_output_tokens=4096,
        tool_concurrency=1,
        scoped_advisory_reviews=True,
        specialist_catalog_fingerprint="c" * 64,
    )


def request() -> tuple[WorkflowRun, ReviewSnapshot]:
    definition = research_workflow("baseline-analysis")
    snapshot = ReviewSnapshot.model_validate(
        {
            "artifacts": [
                {
                    "artifact_id": "plan",
                    "file": {
                        "path": "results/plan.json",
                        "sha256": "d" * 64,
                        "size_bytes": 20,
                    },
                }
            ]
        }
    )
    return WorkflowRun(
        run_id="review-test",
        revision=1,
        binding=WorkflowProjectBinding(
            workflow_id=definition.workflow_id,
            workflow_fingerprint=definition.fingerprint,
            output_directory="results",
            inputs=tuple(
                WorkflowBoundInput(
                    input_id=item.input_id,
                    kind=item.kind,
                    value=f"{item.input_id}.txt",
                    sha256="d" * 64,
                )
                for item in definition.inputs
            ),
            artifacts=definition.bind_artifacts("results"),
        ),
        stage_id="plan",
        phase="ready",
        created_at=datetime.now(UTC),
    ), snapshot


@pytest.mark.parametrize(
    "damage", [None, "stale", "incomplete", "unknown-usage", "ambiguous", "runtime"]
)
def test_deployment_selection_reuses_all_retained_trial_gates(
    tmp_path: Path,
    review_evidence: Callable[[EvaluationRuntimeObservation, datetime], ReviewQualifications],
    damage: str | None,
) -> None:
    now = datetime.now(UTC)
    observed = runtime()
    evidence = review_evidence(observed, now)
    route = evidence.routes[0]
    if damage in {"incomplete", "unknown-usage"}:
        first = route.runs[0]
        altered = (
            first.model_copy(
                update={
                    "checks": tuple(
                        check.model_copy(update={"status": "not_run"}) for check in first.checks
                    )
                }
            )
            if damage == "incomplete"
            else first.model_copy(
                update={"usage": first.usage.model_copy(update={"reported_cost_usd": None})}
            )
        )
        evidence = evidence.model_copy(
            update={"routes": (route.model_copy(update={"runs": (altered, *route.runs[1:])}),)}
        )
    elif damage == "ambiguous":
        evidence = evidence.model_copy(update={"routes": (route, route)})
    elif damage == "runtime":
        observed = observed.model_copy(update={"model_options_fingerprint": "e" * 64})
    run, snapshot = request()

    def prepare() -> ParallelReviewPlan:
        return qualified_review_plan(
            evidence=evidence,
            project_root=tmp_path,
            run=run,
            snapshot=snapshot,
            session_id="research",
            runtime=observed,
            now=now + timedelta(days=31) if damage == "stale" else now,
        )

    if damage is None:
        plan = prepare()
        assert plan.scope.workers == 2
        assert len(plan.evidence) == 12
        assert (
            plan.scope.budget.maximum_seconds
            <= research_workflow("baseline-analysis").stage("plan").budget.maximum_seconds
        )
    else:
        with pytest.raises(ValueError, match=r"qualified|unique"):
            prepare()


def test_evidence_loader_rejects_project_paths_links_corruption_and_public_writes(
    tmp_path: Path,
    review_evidence: Callable[[EvaluationRuntimeObservation, datetime], ReviewQualifications],
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    path = tmp_path / "evidence.json"
    evidence = review_evidence(runtime(), datetime.now(UTC))
    path.write_text(evidence.model_dump_json())
    path.chmod(0o600)
    assert load_review_qualifications(path, project_root=project) == evidence
    linked = tmp_path / "linked.json"
    linked.symlink_to(path)
    internal = project / "evidence.json"
    internal.write_bytes(path.read_bytes())
    for invalid in (linked, internal, Path("relative.json")):
        with pytest.raises(ValueError, match="unavailable or invalid"):
            load_review_qualifications(invalid, project_root=project)
    path.chmod(0o666)
    with pytest.raises(ValueError, match="unavailable or invalid"):
        load_review_qualifications(path, project_root=project)
    path.chmod(0o600)
    path.write_text("invalid")
    with pytest.raises(ValueError, match="unavailable or invalid"):
        load_review_qualifications(path, project_root=project)


def test_missing_evidence_does_not_start_a_session_or_modify_project(tmp_path: Path) -> None:
    gateway = SessionGateway(
        project=ProjectContext(tmp_path),
        env={
            "HEARTWOOD_REVIEW_QUALIFICATIONS": str(tmp_path.parent / "missing-evidence.json"),
        },
    )
    try:
        assert gateway.research_workflows().workflows
        assert not gateway._services
        assert list(tmp_path.iterdir()) == []
    finally:
        gateway.stop()
