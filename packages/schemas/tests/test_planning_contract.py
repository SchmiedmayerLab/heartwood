# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Synthetic planning messages, adversarial links, and exact-byte approval bindings."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import ValidationError

from heartwood.schemas._planning_contract import (
    BudgetObservation,
    OperationSetProposal,
    PlanningOverview,
    PlatformEstimate,
    ReviewedOperationSet,
    WorkloadPrediction,
    reviewed_operation_set,
    verify_reviewed_operation_set,
)

NOW = "2026-10-17T10:00:00Z"
LATER = "2026-10-17T11:00:00Z"
REFERENCE = {"record_id": "plan", "revision": 1}
PREDICTION = {"record_id": "prediction", "revision": 1}
DENOMINATION = {"kind": "currency", "unit": "USD"}


def prediction() -> dict[str, Any]:
    return {
        "project_context_id": "project",
        "prediction": PREDICTION.copy(),
        "plan": REFERENCE.copy(),
        "created_at": NOW,
        "uncertainty": "Duration depends on the observed input sizes.",
        "elements": [
            {
                "element_id": kind,
                "kind": kind,
                "description": f"Synthetic {kind}",
                "runtime_profile_ref": "profile-small",
                "quantities": [
                    {
                        "metric": "duration",
                        "unit": "seconds",
                        "expected": "120",
                        "lower": "60",
                        "upper": "240",
                        "uncertainty": "Pilot measurement range.",
                    }
                ],
            }
            for kind in ("exploration", "batch", "inference")
        ],
    }


def estimate() -> dict[str, Any]:
    return {
        "project_context_id": "project",
        "estimate_id": "estimate",
        "scope": "plan",
        "prediction": PREDICTION.copy(),
        "plan": REFERENCE.copy(),
        "created_at": NOW,
        "valid_until": LATER,
        "total": {"denomination": DENOMINATION.copy(), "coverage": "complete", "expected": "4.10"},
        "covered_element_ids": ["exploration", "batch", "inference"],
        "element_estimates": [
            {
                "element_ids": ["batch"],
                "included_in_total": True,
                "cost": {
                    "denomination": DENOMINATION.copy(),
                    "coverage": "complete",
                    "expected": "3.00",
                },
            }
        ],
    }


def proposal() -> dict[str, Any]:
    return {
        "project_context_id": "project",
        "proposal": {"record_id": "proposal", "revision": 1},
        "plan": REFERENCE.copy(),
        "prediction": PREDICTION.copy(),
        "created_at": NOW,
        "estimates": [estimate()],
        "operations": [
            {
                "operation_id": "batch-submission",
                "element_ids": ["batch"],
                "description": "Submit exactly the reviewed batch.",
                "data_scope": "Synthetic inputs only.",
                "destination": "project output directory",
                "estimate_ids": ["estimate"],
                "request": {
                    "service_id": "compute",
                    "protocol": "GA4GH TES",
                    "request_ref": "project://requests/batch-1",
                    "request_sha256": "a" * 64,
                },
            }
        ],
    }


def overview() -> dict[str, Any]:
    reviewed = reviewed_operation_set(
        json.dumps(proposal()).encode(), reference="project://proposal-1"
    )
    return {
        "prediction": prediction(),
        "estimates": [estimate()],
        "operation_sets": [reviewed.model_dump(mode="json")],
        "researcher_decisions": [
            {
                "project_context_id": "project",
                "decision_id": "decision",
                "proposal": {"record_id": "proposal", "revision": 1},
                "document_sha256": reviewed.document_sha256,
                "decision": "approved",
                "decided_at": NOW,
                "actor_ref": "researcher",
                "audit_ref": "session://decision-1",
            }
        ],
        "authorizations": [
            {
                "project_context_id": "project",
                "authorization_id": "authorization",
                "proposal": {"record_id": "proposal", "revision": 1},
                "document_sha256": reviewed.document_sha256,
                "status": "denied",
                "observed_at": NOW,
                "reason": "Another run consumed the available budget.",
                "platform_record_ref": "platform://authorization-1",
            }
        ],
        "budgets": [
            {
                "project_context_id": "project",
                "budget_id": "budget",
                "observed_at": NOW,
                "denomination": DENOMINATION.copy(),
                "allocated": "100",
                "reserved": "0",
                "spent": "101",
                "available": "-1",
                "platform_record_ref": "platform://budget-1",
            }
        ],
        "operations": [
            {
                "project_context_id": "project",
                "operation_id": "batch-submission",
                "service_id": "compute",
                "backend_operation_ref": "backend://request-1",
                "observed_at": NOW,
                "status": "denied",
                "platform_record_ref": "platform://operation-1",
            }
        ],
    }


def parse(value: dict[str, Any]) -> PlanningOverview:
    return PlanningOverview.model_validate_json(json.dumps(value))


def test_full_overview_preserves_platform_denial_after_researcher_approval() -> None:
    value = overview()
    result = parse(value)
    assert result.researcher_decisions[0].decision == "approved"
    assert result.authorizations[0].status == "denied"
    assert result.budgets[0].available == "-1"
    assert result.operations[0].status == "denied"
    assert result.estimates[0].total.expected == "4.10"
    validator = Draft202012Validator(
        PlanningOverview.model_json_schema(), format_checker=FormatChecker()
    )
    validator.validate(result.model_dump(mode="json"))
    assert parse(result.model_dump(mode="json")) == result


@pytest.mark.parametrize("amount", [0.1, 1, True, "NaN", "Infinity", "1e3", "01", "-1", "1\n"])
def test_cost_numbers_are_exact_nonnegative_decimal_strings(amount: object) -> None:
    value = estimate()
    value["total"]["expected"] = amount
    with pytest.raises(ValidationError):
        PlatformEstimate.model_validate_json(json.dumps(value))
    assert not Draft202012Validator(PlatformEstimate.model_json_schema()).is_valid(value)


@pytest.mark.parametrize(
    "update",
    [
        {"coverage": "unknown", "expected": "0", "limitations": ["No price."]},
        {"coverage": "unknown", "expected": None},
        {"coverage": "partial", "expected": "1", "limitations": []},
        {"lower": "5", "expected": "4", "upper": "3"},
    ],
)
def test_unknown_partial_and_range_consistency(update: dict[str, Any]) -> None:
    value = estimate()
    value["total"].update(update)
    with pytest.raises(ValidationError):
        PlatformEstimate.model_validate_json(json.dumps(value))


def test_unknown_estimate_and_budget_remain_unknown() -> None:
    value = estimate()
    value["total"] = {
        "denomination": DENOMINATION,
        "coverage": "unknown",
        "limitations": ["Runtime price unavailable."],
    }
    value["element_estimates"] = []
    parsed = PlatformEstimate.model_validate_json(json.dumps(value))
    assert parsed.total.expected is None
    budget = overview()["budgets"][0]
    budget["available"] = None
    with pytest.raises(ValidationError, match="missing budget"):
        BudgetObservation.model_validate_json(json.dumps(budget))
    budget["limitations"] = ["Pending accounting update."]
    assert BudgetObservation.model_validate_json(json.dumps(budget)).available is None


def test_included_estimate_components_cannot_overlap() -> None:
    value = estimate()
    value["element_estimates"] *= 2
    with pytest.raises(ValidationError, match="estimate components"):
        PlatformEstimate.model_validate_json(json.dumps(value))


@pytest.mark.parametrize("target", ["created_at", "valid_until"])
def test_estimates_require_aware_times(target: str) -> None:
    value = estimate()
    value[target] = "2026-10-17T10:00:00"
    with pytest.raises(ValidationError):
        PlatformEstimate.model_validate_json(json.dumps(value))


def test_expired_estimate_is_representable_without_becoming_authority() -> None:
    value = estimate()
    value["created_at"] = "2000-01-01T00:00:00Z"
    value["valid_until"] = "2000-01-01T01:00:00Z"
    assert PlatformEstimate.model_validate_json(json.dumps(value)).valid_until.year == 2000


@pytest.mark.parametrize(
    "mutation",
    [
        "cross-project",
        "stale-prediction",
        "missing-element",
        "incomplete-plan",
        "changed-proposal",
        "missing-estimate",
        "wrong-service",
        "missing-budget",
    ],
)
def test_overview_rejects_mismatched_links(mutation: str) -> None:
    value = overview()
    if mutation == "cross-project":
        value["estimates"][0]["project_context_id"] = "other"
    elif mutation == "stale-prediction":
        value["estimates"][0]["prediction"]["revision"] = 2
    elif mutation == "missing-element":
        value["estimates"][0]["covered_element_ids"].append("missing")
    elif mutation == "incomplete-plan":
        value["estimates"][0]["covered_element_ids"] = ["batch"]
    elif mutation == "changed-proposal":
        value["researcher_decisions"][0]["document_sha256"] = "b" * 64
    elif mutation == "missing-estimate":
        value["operation_sets"][0]["proposal"]["operations"][0]["estimate_ids"] = ["missing"]
    elif mutation == "wrong-service":
        value["operations"][0]["service_id"] = "other"
    else:
        value["authorizations"][0]["budget_id"] = "missing"
    with pytest.raises(ValidationError):
        parse(value)


def test_exact_bytes_not_reserialized_values_bind_approval() -> None:
    document = json.dumps(proposal()).encode()
    reviewed = reviewed_operation_set(document, reference="project://proposal-1")
    changed = reviewed_operation_set(document + b"\n", reference="project://proposal-2")
    assert reviewed.proposal == changed.proposal
    assert reviewed.document_sha256 == hashlib.sha256(document).hexdigest()
    assert changed.document_sha256 != reviewed.document_sha256
    field_name = "description"
    with pytest.raises(ValidationError):
        setattr(reviewed.proposal.operations[0], field_name, "changed")
    assert isinstance(reviewed.proposal.operations, tuple)


def test_prediction_evidence_is_project_local_and_revision_bound() -> None:
    value = prediction()
    value["evidence"] = [
        {
            "evidence_id": "run",
            "project_context_id": "other",
            "kind": "prior-run",
            "reference": "project://run",
            "description": "Measured workload.",
            "observed_at": NOW,
        }
    ]
    with pytest.raises(ValidationError, match="same project context"):
        WorkloadPrediction.model_validate_json(json.dumps(value))
    value["evidence"][0]["project_context_id"] = "project"
    value["elements"][0]["evidence_ids"] = ["missing"]
    with pytest.raises(ValidationError, match="missing evidence"):
        WorkloadPrediction.model_validate_json(json.dumps(value))
    value["elements"][0]["evidence_ids"] = ["run"]
    value["supersedes"] = PREDICTION.copy()
    with pytest.raises(ValidationError, match="earlier revision"):
        WorkloadPrediction.model_validate_json(json.dumps(value))
    value["prediction"]["revision"] = 2
    assert WorkloadPrediction.model_validate_json(json.dumps(value)).prediction.revision == 2


def test_invalid_payload_values_are_not_in_exception_text() -> None:
    value = deepcopy(proposal())
    value["operations"][0]["request"]["request_sha256"] = "synthetic-secret-value"
    with pytest.raises(ValidationError) as failure:
        OperationSetProposal.model_validate_json(json.dumps(value))
    assert "synthetic-secret-value" not in str(failure.value)


def test_complete_element_estimate_does_not_claim_whole_plan_coverage() -> None:
    value = overview()
    value["operation_sets"] = []
    value["researcher_decisions"] = []
    value["authorizations"] = []
    value["operations"] = []
    value["estimates"][0]["scope"] = "elements"
    value["estimates"][0]["covered_element_ids"] = ["batch"]
    assert parse(value).estimates[0].total.coverage == "complete"


def test_changed_price_or_expiry_under_same_id_invalidates_review_snapshot() -> None:
    for field, changed in (
        ("valid_until", "2026-10-17T12:00:00Z"),
        ("total", {"denomination": DENOMINATION, "coverage": "complete", "expected": "40"}),
    ):
        value = overview()
        value["estimates"][0][field] = changed
        with pytest.raises(ValidationError, match="snapshot differs"):
            parse(value)


def test_proposal_envelope_requires_exact_bytes_verification() -> None:
    document = json.dumps(proposal()).encode()
    reviewed = reviewed_operation_set(document, reference="project://proposal-1")
    verify_reviewed_operation_set(reviewed, document)
    tampered = reviewed.model_dump(mode="json")
    tampered["proposal"]["operations"][0]["destination"] = "different destination"
    claimed = ReviewedOperationSet.model_validate_json(json.dumps(tampered))
    with pytest.raises(ValueError, match="exact document bytes"):
        verify_reviewed_operation_set(claimed, document)
    with pytest.raises(ValueError, match="exact document bytes"):
        verify_reviewed_operation_set(reviewed, document + b"\n")


def test_review_document_rejects_duplicate_keys_without_exposing_payload() -> None:
    document = (
        json.dumps(proposal())
        .replace(
            '"project_context_id": "project"',
            '"project_context_id": "synthetic-secret-value", "project_context_id": "project"',
        )
        .encode()
    )
    with pytest.raises(ValueError, match="unique object keys") as failure:
        reviewed_operation_set(document, reference="project://proposal-1")
    assert "synthetic-secret-value" not in str(failure.value)


def test_mixed_cost_denominations_are_rejected() -> None:
    value = estimate()
    value["element_estimates"][0]["cost"]["denomination"] = {
        "kind": "allocation-unit",
        "unit": "credits",
    }
    with pytest.raises(ValidationError, match="one denomination"):
        PlatformEstimate.model_validate_json(json.dumps(value))


def test_additional_components_cannot_recount_an_included_element() -> None:
    value = estimate()
    value["element_estimates"].append(deepcopy(value["element_estimates"][0]))
    value["element_estimates"][1]["included_in_total"] = False
    with pytest.raises(ValidationError, match="cannot overlap total coverage"):
        PlatformEstimate.model_validate_json(json.dumps(value))


def test_operation_cannot_use_an_unrelated_estimate() -> None:
    value = proposal()
    unrelated = value["estimates"][0]
    unrelated["scope"] = "elements"
    unrelated["covered_element_ids"] = ["exploration"]
    unrelated["element_estimates"] = []
    unrelated["total"]["coverage"] = "partial"
    unrelated["total"]["limitations"] = ["Exploration only."]
    with pytest.raises(ValidationError, match="relevant workload elements"):
        OperationSetProposal.model_validate_json(json.dumps(value))


def test_revoked_authorization_can_report_already_expired_validity() -> None:
    value = overview()
    value["authorizations"][0]["status"] = "revoked"
    value["authorizations"][0]["observed_at"] = "2026-10-17T12:00:00Z"
    value["authorizations"][0]["valid_until"] = LATER
    parsed = parse(value).authorizations[0]
    assert parsed.valid_until is not None
    assert parsed.valid_until < parsed.observed_at


@pytest.mark.parametrize("coverage", ["complete", "partial", "unknown"])
def test_operation_can_reference_separately_estimated_additional_component(coverage: str) -> None:
    value = proposal()
    price = value["estimates"][0]
    price["scope"] = "elements"
    price["covered_element_ids"] = ["exploration"]
    additional = price["element_estimates"][0]
    additional["included_in_total"] = False
    additional["cost"]["coverage"] = coverage
    if coverage != "complete":
        additional["cost"]["limitations"] = ["Batch resource pricing is incomplete."]
    if coverage == "unknown":
        additional["cost"]["expected"] = None
    parsed = OperationSetProposal.model_validate_json(json.dumps(value))
    assert parsed.estimates[0].element_estimates[0].cost.coverage == coverage
    assert parsed.estimates[0].total.coverage == "complete"
    assert parsed.estimates[0].total.expected == "4.10"


@pytest.mark.parametrize("component_coverage", ["partial", "unknown"])
def test_additional_component_requires_its_own_limitations(component_coverage: str) -> None:
    value = proposal()
    price = value["estimates"][0]
    price["scope"] = "elements"
    price["covered_element_ids"] = ["exploration"]
    price["total"]["coverage"] = "partial"
    price["total"]["limitations"] = ["Exploration cost is incomplete."]
    additional = price["element_estimates"][0]
    additional["included_in_total"] = False
    additional["cost"]["coverage"] = component_coverage
    if component_coverage == "unknown":
        additional["cost"]["expected"] = None
    with pytest.raises(ValidationError, match="requires limitations"):
        OperationSetProposal.model_validate_json(json.dumps(value))


def test_unrelated_partial_total_cannot_excuse_unrepresented_operation_element() -> None:
    value = proposal()
    price = value["estimates"][0]
    price["scope"] = "elements"
    price["covered_element_ids"] = ["exploration"]
    price["total"]["coverage"] = "partial"
    price["total"]["limitations"] = ["Exploration cost is incomplete."]
    price["element_estimates"][0]["included_in_total"] = False
    value["operations"][0]["element_ids"] = ["batch", "inference"]
    with pytest.raises(ValidationError, match="represent every element"):
        OperationSetProposal.model_validate_json(json.dumps(value))
    # Explicitly identifying the inference cost as unknown makes the gap visible.
    price["element_estimates"].append(
        {
            "element_ids": ["inference"],
            "included_in_total": False,
            "cost": {
                "denomination": DENOMINATION,
                "coverage": "unknown",
                "limitations": ["Inference price unavailable."],
            },
        }
    )
    parsed = OperationSetProposal.model_validate_json(json.dumps(value))
    assert parsed.estimates[0].element_estimates[-1].cost.coverage == "unknown"
