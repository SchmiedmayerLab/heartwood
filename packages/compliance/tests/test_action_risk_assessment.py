# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""False approvals and incomplete measurements must not qualify a deployment."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from heartwood.compliance.action_risk import action_risk_corpus, assess_action_risk
from heartwood.schemas.action_risk import (
    ActionRiskCorpus,
    ActionRiskEvaluation,
    ActionRiskMeasurement,
    ActionRiskPolicy,
)


def _policy() -> ActionRiskPolicy:
    return ActionRiskPolicy(
        deployment="synthetic-contract-test",
        maximum_unnecessary_confirmation_fraction=0,
        maximum_mean_latency_seconds=0.1,
    )


def _evaluation() -> ActionRiskEvaluation:
    corpus = action_risk_corpus()
    return ActionRiskEvaluation(
        corpus_fingerprint=corpus.fingerprint,
        analyzer_fingerprint="a" * 64,
        evaluated_at=datetime(2026, 9, 10, tzinfo=UTC),
        openhands_version="synthetic-test",
        confirmation_mode="confirm-risky",
        measurements=tuple(
            ActionRiskMeasurement(
                case_id=probe.case_id,
                category=probe.category,
                must_confirm=probe.must_confirm,
                observed_risk="HIGH" if probe.must_confirm else "LOW",
                confirmation_required=probe.must_confirm,
                elapsed_seconds=0.001,
            )
            for probe in corpus.probes
        ),
    )


def test_corpus_covers_threat_families_and_missing_model_risk() -> None:
    corpus = action_risk_corpus()
    assert {probe.category for probe in corpus.probes} == {
        "benign",
        "ambiguous",
        "destructive",
        "encoded",
        "injected",
        "network",
    }
    assert {probe.tool for probe in corpus.probes} == {"terminal", "file_editor"}
    assert {probe.model_risk for probe in corpus.probes} == {"LOW", "UNKNOWN"}
    assert ActionRiskCorpus.model_validate_json(corpus.model_dump_json()) == corpus


def test_complete_correct_decisions_pass_explicit_limits() -> None:
    report = _evaluation()
    decision = assess_action_risk(action_risk_corpus(), report, policy=_policy())
    assert decision.passed
    assert decision.reasons == ()
    assert decision.evaluation_fingerprint == report.fingerprint
    assert ActionRiskEvaluation.model_validate_json(report.model_dump_json()) == report


@pytest.mark.parametrize("change", ["missing", "label", "digest"])
def test_missing_or_mislabeled_cases_cannot_be_counted_as_success(change: str) -> None:
    report = _evaluation()
    if change == "missing":
        report = report.model_copy(update={"measurements": report.measurements[1:]})
    elif change == "digest":
        report = report.model_copy(update={"corpus_fingerprint": "b" * 64})
    else:
        items = list(report.measurements)
        items[0] = items[0].model_copy(update={"must_confirm": not items[0].must_confirm})
        report = report.model_copy(update={"measurements": tuple(items)})
    decision = assess_action_risk(action_risk_corpus(), report, policy=_policy())
    assert not decision.passed
    assert {"corpus-mismatch", "label-mismatch"}.intersection(decision.reasons)


def test_false_approval_and_unconfirmed_unknown_are_independent_failures() -> None:
    report = _evaluation()
    items = list(report.measurements)
    index = next(i for i, item in enumerate(items) if item.must_confirm)
    items[index] = items[index].model_copy(
        update={"observed_risk": "UNKNOWN", "confirmation_required": False}
    )
    report = report.model_copy(update={"measurements": tuple(items)})
    assert report.false_approvals == report.unknown_classifications == 1
    decision = assess_action_risk(action_risk_corpus(), report, policy=_policy())
    assert set(decision.reasons) == {"false-approval", "unknown-not-confirmed"}


def test_benign_confirmation_fraction_and_latency_have_explicit_limits() -> None:
    report = _evaluation()
    items = tuple(
        item.model_copy(update={"confirmation_required": True, "elapsed_seconds": 0.2})
        for item in report.measurements
    )
    report = report.model_copy(update={"measurements": items})
    decision = assess_action_risk(action_risk_corpus(), report, policy=_policy())
    assert set(decision.reasons) == {"unnecessary-confirmation-limit", "latency-limit"}


def test_corpus_labels_and_model_ratings_are_bound_to_its_identity() -> None:
    corpus = action_risk_corpus()
    items = list(corpus.probes)
    items[0] = items[0].model_copy(update={"model_risk": "HIGH"})
    assert ActionRiskCorpus(probes=tuple(items)).fingerprint != corpus.fingerprint


def test_duplicate_cases_and_missing_measurements_are_invalid() -> None:
    corpus = action_risk_corpus()
    with pytest.raises(ValidationError, match="unique"):
        ActionRiskCorpus(probes=(*corpus.probes, corpus.probes[0]))
    report = _evaluation().model_dump()
    report["measurements"] = []
    with pytest.raises(ValidationError):
        ActionRiskEvaluation.model_validate(report)


@pytest.mark.parametrize("limit", [-1.0, 1.1, float("nan"), float("inf")])
def test_invalid_thresholds_are_rejected(limit: float) -> None:
    with pytest.raises(ValidationError):
        ActionRiskPolicy(
            deployment="test",
            maximum_unnecessary_confirmation_fraction=limit,
            maximum_mean_latency_seconds=0.1,
        )
