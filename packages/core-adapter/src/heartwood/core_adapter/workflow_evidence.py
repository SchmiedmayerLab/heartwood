# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Deterministic workflow gates over evidence supplied by gateway-owned evaluators."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

from heartwood.schemas.workflows import (
    WorkflowCheckResult,
    WorkflowDefinition,
    WorkflowOutcomeStatus,
    WorkflowStageAssessment,
    WorkflowValueFingerprint,
)


def assess_workflow_stage(
    definition: WorkflowDefinition,
    stage_id: str,
    *,
    artifacts: Sequence[WorkflowValueFingerprint],
    checks: Sequence[WorkflowCheckResult],
    model_status: WorkflowOutcomeStatus | None,
) -> WorkflowStageAssessment:
    """Require independent, current evidence before a stage is eligible to advance.

    This function does not execute checks, grant action approval, or trust a model's
    check claims. Callers supply results from the gateway's evaluator registry.
    A required researcher decision must separately bind to this exact assessment.
    """
    stage = definition.stage(stage_id)
    current = {item.artifact_id: item.sha256 for item in artifacts}
    observed = {item.check_id: item for item in checks}
    if len(current) != len(artifacts) or len(observed) != len(checks):
        raise ValueError("Workflow evidence identities must be unique")
    scope = set(stage.reads) | set(stage.writes)
    reasons: set[str] = set()
    if model_status != "success":
        reasons.add("model-outcome-not-successful")
    for artifact_id in scope - current.keys():
        reasons.add(f"{artifact_id}:missing-artifact")
    required = {check.check_id: check for check in stage.checks}
    for check_id in observed.keys() - required.keys():
        reasons.add(f"{check_id}:undeclared-check")
    for check_id, requirement in required.items():
        result = observed.get(check_id)
        if result is None:
            reasons.add(f"{check_id}:missing-check")
            continue
        if result.evaluator_id != requirement.evaluator_id:
            reasons.add(f"{check_id}:wrong-evaluator")
        if result.status != "passed":
            reasons.add(f"{check_id}:not-passed")
        inspected = {item.artifact_id: item.sha256 for item in result.inspected}
        if len(inspected) != len(result.inspected):
            raise ValueError("Workflow check evidence identities must be unique")
        expected = {name: current[name] for name in requirement.artifact_ids if name in current}
        if set(expected) != set(requirement.artifact_ids) or inspected != expected:
            reasons.add(f"{check_id}:stale-or-incomplete-evidence")
    evidence = {
        "workflow": definition.fingerprint,
        "stage": stage_id,
        "model_status": model_status,
        "artifacts": {name: current[name] for name in sorted(scope) if name in current},
        "checks": [
            {
                "check_id": result.check_id,
                "evaluator_id": result.evaluator_id,
                "status": result.status,
                "inspected": [
                    item.model_dump()
                    for item in sorted(result.inspected, key=lambda item: item.artifact_id)
                ],
            }
            for result in sorted(checks, key=lambda item: item.check_id)
        ],
    }
    content = json.dumps(evidence, sort_keys=True, separators=(",", ":"))
    return WorkflowStageAssessment(
        workflow_fingerprint=definition.fingerprint,
        stage_id=stage_id,
        evidence_fingerprint=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        evidence_satisfied=not reasons,
        researcher_review_required=stage.reviewer_gate == "researcher",
        reasons=tuple(sorted(reasons)),
    )
