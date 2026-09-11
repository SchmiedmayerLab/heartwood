# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Workflow definition and deterministic evidence-gate contracts."""

from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from heartwood.core_adapter.research_workflows import research_workflow, research_workflows
from heartwood.core_adapter.workflow_evidence import assess_workflow_stage
from heartwood.schemas.workflows import (
    WorkflowCheckResult,
    WorkflowDefinition,
    WorkflowOutcomeStatus,
    WorkflowValueFingerprint,
)


def test_workflow_definitions_round_trip_and_keep_execution_provider_neutral() -> None:
    definitions = research_workflows()
    assert [item.workflow_id for item in definitions] == [
        "dataset-readiness",
        "baseline-analysis",
        "result-verification",
    ]
    for definition in definitions:
        restored = WorkflowDefinition.model_validate_json(definition.model_dump_json())
        assert restored == definition
        assert restored.fingerprint == definition.fingerprint
        assert restored.stage(restored.stages[0].stage_id) == restored.stages[0]
        assert restored.stages[-1].reviewer_gate == "researcher"
        with pytest.raises(ValueError, match="Unknown workflow stage"):
            restored.stage("undeclared")
        with pytest.raises(ValidationError):
            WorkflowDefinition.model_validate({**definition.model_dump(), "model": "other-model"})
    with pytest.raises(ValueError, match="Unknown research workflow"):
        research_workflow("undeclared")


@pytest.mark.parametrize(
    "damage",
    [
        "future-input",
        "unknown-input",
        "duplicate-output",
        "unused-output",
        "unchecked-output",
        "wrong-check-context",
        "duplicate-stage",
        "duplicate-check",
        "duplicate-reference",
        "input-output-collision",
        "duplicate-path",
        "directory-shadow",
    ],
)
def test_incoherent_workflow_cannot_be_loaded(damage: str) -> None:
    data = research_workflow("baseline-analysis").model_dump(mode="json")
    if damage == "future-input":
        data["stages"][0]["reads"].append("metrics")
    elif damage == "unknown-input":
        data["stages"][0]["reads"].append("undeclared")
    elif damage == "duplicate-output":
        data["stages"][1]["writes"].append("plan")
    elif damage == "unused-output":
        data["artifacts"].append(
            {**data["artifacts"][0], "artifact_id": "extra", "relative_path": "extra.json"}
        )
    elif damage == "unchecked-output":
        data["stages"][0]["checks"][0]["artifact_ids"].remove("plan")
    elif damage == "wrong-check-context":
        data["stages"][0]["checks"][0]["artifact_ids"].append("metrics")
    elif damage == "duplicate-stage":
        data["stages"][1]["stage_id"] = "plan"
    elif damage == "duplicate-check":
        data["stages"][1]["checks"][0]["check_id"] = "analysis-plan"
    elif damage == "duplicate-reference":
        data["stages"][0]["reads"].append("data")
    elif damage == "input-output-collision":
        data["inputs"][0]["input_id"] = "plan"
    elif damage == "duplicate-path":
        data["artifacts"][1]["relative_path"] = "PLAN.JSON"
    else:
        data["artifacts"][1]["relative_path"] = "plan.json/program.py"
    with pytest.raises(ValidationError):
        WorkflowDefinition.model_validate(data)


@pytest.mark.parametrize(
    "path",
    [
        "../outside",
        "/absolute",
        ".heartwood/secret",
        "nested/.git/config",
        "a/../b",
        "a//b",
        "a\\b",
        "a\x00b",
        ".",
    ],
)
def test_workflow_artifacts_use_the_shared_public_project_path_boundary(path: str) -> None:
    data = research_workflow("baseline-analysis").model_dump(mode="json")
    data["artifacts"][0]["relative_path"] = path
    with pytest.raises(ValidationError):
        WorkflowDefinition.model_validate(data)


def _evidence(stage_id: str) -> tuple[list[WorkflowValueFingerprint], list[WorkflowCheckResult]]:
    definition = research_workflow("baseline-analysis")
    stage = definition.stage(stage_id)
    fingerprints = {
        name: WorkflowValueFingerprint(
            artifact_id=name,
            sha256=hashlib.sha256(name.encode()).hexdigest(),
        )
        for name in {*stage.reads, *stage.writes}
    }
    checks = [
        WorkflowCheckResult(
            check_id=check.check_id,
            evaluator_id=check.evaluator_id,
            status="passed",
            inspected=tuple(fingerprints[name] for name in check.artifact_ids),
        )
        for check in stage.checks
    ]
    return list(fingerprints.values()), checks


def test_evidence_eligibility_does_not_bypass_required_researcher_review() -> None:
    definition = research_workflow("baseline-analysis")
    artifacts, checks = _evidence("plan")
    result = assess_workflow_stage(
        definition,
        "plan",
        artifacts=artifacts,
        checks=checks,
        model_status="success",
    )
    assert result.evidence_satisfied
    assert result.researcher_review_required
    assert result.reasons == ()


@pytest.mark.parametrize(
    "damage",
    [
        "missing-artifact",
        "missing-check",
        "failed-check",
        "not-run",
        "wrong-evaluator",
        "changed-artifact",
        "incomplete-check-inputs",
        "undeclared-check",
    ],
)
def test_reported_model_success_cannot_override_missing_or_stale_evidence(damage: str) -> None:
    definition = research_workflow("baseline-analysis")
    artifacts, checks = _evidence("plan")
    if damage == "missing-artifact":
        artifacts = [item for item in artifacts if item.artifact_id != "plan"]
    elif damage == "missing-check":
        checks = []
    elif damage == "failed-check":
        checks[0] = checks[0].model_copy(update={"status": "failed"})
    elif damage == "not-run":
        checks[0] = checks[0].model_copy(update={"status": "not_run"})
    elif damage == "wrong-evaluator":
        checks[0] = checks[0].model_copy(update={"evaluator_id": "model.claim"})
    elif damage == "changed-artifact":
        artifacts[0] = artifacts[0].model_copy(update={"sha256": "e" * 64})
    elif damage == "incomplete-check-inputs":
        checks[0] = checks[0].model_copy(update={"inspected": checks[0].inspected[1:]})
    else:
        checks.append(checks[0].model_copy(update={"check_id": "undeclared"}))
    result = assess_workflow_stage(
        definition,
        "plan",
        artifacts=artifacts,
        checks=checks,
        model_status="success",
    )
    assert not result.evidence_satisfied
    assert result.reasons


@pytest.mark.parametrize("status", [None, "partial_success", "blocked", "failed", "unknown"])
def test_finished_conversation_is_not_a_successful_stage(
    status: WorkflowOutcomeStatus | None,
) -> None:
    artifacts, checks = _evidence("plan")
    result = assess_workflow_stage(
        research_workflow("baseline-analysis"),
        "plan",
        artifacts=artifacts,
        checks=checks,
        model_status=status,
    )
    assert not result.evidence_satisfied
    assert result.reasons == ("model-outcome-not-successful",)


def test_assessment_fingerprint_is_order_independent_and_binds_changed_evidence() -> None:
    definition = research_workflow("baseline-analysis")
    artifacts, checks = _evidence("execute")
    original = assess_workflow_stage(
        definition,
        "execute",
        artifacts=artifacts,
        checks=checks,
        model_status="success",
    )
    reordered = assess_workflow_stage(
        definition,
        "execute",
        artifacts=list(reversed(artifacts)),
        checks=[
            check.model_copy(update={"inspected": tuple(reversed(check.inspected))})
            for check in reversed(checks)
        ],
        model_status="success",
    )
    assert original == reordered
    assert original.evidence_satisfied
    assert not original.researcher_review_required
    artifacts[0] = artifacts[0].model_copy(update={"sha256": "c" * 64})
    changed = assess_workflow_stage(
        definition,
        "execute",
        artifacts=artifacts,
        checks=checks,
        model_status="success",
    )
    assert changed.evidence_fingerprint != original.evidence_fingerprint
    assert not changed.evidence_satisfied


@pytest.mark.parametrize("duplicate", ["artifact", "check", "inspected"])
def test_ambiguous_evidence_identity_is_rejected(duplicate: str) -> None:
    artifacts, checks = _evidence("plan")
    if duplicate == "artifact":
        artifacts.append(artifacts[0])
    elif duplicate == "check":
        checks.append(checks[0])
    else:
        checks[0] = checks[0].model_copy(
            update={"inspected": (*checks[0].inspected, checks[0].inspected[0])}
        )
    with pytest.raises(ValueError, match="identities must be unique"):
        assess_workflow_stage(
            research_workflow("baseline-analysis"),
            "plan",
            artifacts=artifacts,
            checks=checks,
            model_status="success",
        )
