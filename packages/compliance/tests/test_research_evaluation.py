# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Evidence gates distinguish research quality from connectivity and test doubles."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from heartwood.compliance.evaluation import assess_research_evidence
from heartwood.schemas.evaluation import (
    EvaluationCase,
    EvaluationCheck,
    EvaluationConfiguration,
    EvaluationDimension,
    EvaluationPolicy,
    EvaluationRun,
    EvaluationSuite,
    EvaluationUsage,
    RequiredEvaluationCheck,
)

NOW = datetime(2026, 9, 10, tzinfo=UTC)


def _configuration() -> EvaluationConfiguration:
    return EvaluationConfiguration(
        provider="synthetic",
        model="test/model",
        model_revision="a" * 40,
        platform="generic",
        hardware=("cpu",),
        runtime="llama.cpp-test",
        openhands_version="1.46.0",
        precision="q4",
        context_tokens=32768,
        output_tokens=4096,
        tool_parser="native",
        skill_tree_digest="b" * 64,
        harness_revision="c" * 64,
    )


def _suite() -> EvaluationSuite:
    return EvaluationSuite(
        suite_id="synthetic-research.v1",
        cases=tuple(
            EvaluationCase(
                case_id=workflow,
                workflow_id=workflow,
                fixture_digest="d" * 64,
                required_checks=tuple(
                    RequiredEvaluationCheck(check_id=dimension.value, dimension=dimension)
                    for dimension in EvaluationDimension
                ),
            )
            for workflow in ("dataset-readiness", "baseline-analysis", "result-verification")
        ),
    )


def _runs() -> list[EvaluationRun]:
    return [
        EvaluationRun(
            run_id=uuid4(),
            suite_id=_suite().suite_id,
            suite_fingerprint=_suite().fingerprint,
            case_id=case.case_id,
            fixture_digest=case.fixture_digest,
            seed=repeat,
            execution="live_model",
            configuration=_configuration(),
            started_at=NOW - timedelta(minutes=10 - repeat),
            finished_at=NOW - timedelta(minutes=9 - repeat),
            checks=tuple(
                EvaluationCheck(check_id=check.check_id, dimension=check.dimension, status="passed")
                for check in case.required_checks
            ),
            usage=EvaluationUsage(elapsed_seconds=60),
        )
        for case in _suite().cases
        for repeat in range(3)
    ]


def test_repeated_results_are_exactly_scoped_and_order_independent() -> None:
    runs = _runs()
    assessment = assess_research_evidence(
        suite=_suite(),
        configuration=_configuration(),
        policy=EvaluationPolicy(),
        now=NOW,
        runs=runs,
    )
    reordered = assess_research_evidence(
        suite=_suite(),
        configuration=_configuration(),
        policy=EvaluationPolicy(),
        now=NOW,
        runs=list(reversed(runs)),
    )

    assert assessment.qualified
    assert len(assessment.evidence_run_ids) == 9
    assert assessment == reordered
    assert assessment.configuration_fingerprint == _configuration().fingerprint
    assert runs[0].usage.reported_cost_usd is None


@pytest.mark.parametrize("change", ["hardware", "platform", "context_tokens", "harness_revision"])
def test_evidence_does_not_qualify_a_different_configuration(change: str) -> None:
    data = _configuration().model_dump()
    replacements: dict[str, object] = {
        "hardware": ("other-gpu",),
        "platform": "terra",
        "context_tokens": 65536,
        "harness_revision": "e" * 64,
    }
    data[change] = replacements[change]
    configuration = EvaluationConfiguration.model_validate(data)

    result = assess_research_evidence(
        suite=_suite(),
        configuration=configuration,
        runs=_runs(),
        policy=EvaluationPolicy(),
        now=NOW,
    )

    assert not result.qualified
    assert result.evidence_run_ids == ()


@pytest.mark.parametrize("condition", ["deterministic", "stale", "future", "wrong-fixture"])
def test_nonqualifying_trials_do_not_inflate_evidence(condition: str) -> None:
    runs = _runs()
    updates: dict[str, object] = {
        "deterministic": {"execution": "deterministic"},
        "stale": {"started_at": NOW - timedelta(days=40), "finished_at": NOW - timedelta(days=39)},
        "future": {"finished_at": NOW + timedelta(days=1)},
        "wrong-fixture": {"fixture_digest": "e" * 64},
    }
    update = updates[condition]
    assert isinstance(update, dict)
    runs = [run.model_copy(update=update) for run in runs]

    result = assess_research_evidence(
        suite=_suite(),
        configuration=_configuration(),
        runs=runs,
        policy=EvaluationPolicy(),
        now=NOW,
    )

    assert not result.qualified
    assert result.evidence_run_ids == ()


@pytest.mark.parametrize("status", ["failed", "not_run", "missing", "wrong_dimension"])
def test_latest_incomplete_or_failed_trial_invalidates_previous_passes(status: str) -> None:
    runs = _runs()
    checks = list(runs[0].checks)
    if status == "missing":
        checks.pop()
    elif status == "wrong_dimension":
        checks[-1] = checks[-1].model_copy(update={"dimension": EvaluationDimension.CONNECTIVITY})
    else:
        checks[-1] = checks[-1].model_copy(update={"status": status})
    latest = runs[0].model_copy(
        update={"run_id": uuid4(), "finished_at": NOW, "checks": tuple(checks)}
    )
    runs.append(latest)

    result = assess_research_evidence(
        suite=_suite(),
        configuration=_configuration(),
        runs=runs,
        policy=EvaluationPolicy(),
        now=NOW,
    )

    assert not result.qualified
    assert latest.run_id in result.evidence_run_ids
    assert any("not_passed" in reason for reason in result.reasons)


def test_duplicate_trial_cannot_count_as_repeated_evidence() -> None:
    runs = _runs()
    with pytest.raises(ValueError, match="Duplicate evaluation run"):
        assess_research_evidence(
            suite=_suite(),
            configuration=_configuration(),
            runs=[*runs, runs[0]],
            policy=EvaluationPolicy(),
            now=NOW,
        )


def test_unknown_model_revision_is_not_a_qualified_snapshot() -> None:
    configuration = _configuration().model_copy(update={"model_revision": None})
    runs = [run.model_copy(update={"configuration": configuration}) for run in _runs()]
    result = assess_research_evidence(
        suite=_suite(), configuration=configuration, runs=runs, policy=EvaluationPolicy(), now=NOW
    )
    assert result.reasons == ("model_revision_unknown",)
    assert not result.qualified


def test_connectivity_only_suite_cannot_claim_research_quality() -> None:
    case = (
        _suite()
        .cases[0]
        .model_copy(update={"required_checks": _suite().cases[0].required_checks[:1]})
    )
    with pytest.raises(ValidationError, match="every research evidence dimension"):
        EvaluationSuite(suite_id="connection-only", cases=(case,))


@pytest.mark.parametrize("field", ["prompt", "tool_output", "api_key", "participant_data"])
def test_evidence_schema_does_not_accept_raw_execution_content(field: str) -> None:
    payload = _runs()[0].model_dump()
    payload[field] = "Must not be retained"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        EvaluationRun.model_validate(payload)


@pytest.mark.parametrize("value", [-1, float("inf"), float("nan")])
def test_efficiency_measurements_reject_invalid_values(value: float) -> None:
    with pytest.raises(ValidationError):
        EvaluationUsage(elapsed_seconds=value)


def test_evidence_round_trip_preserves_metadata_and_unknown_cost() -> None:
    run = _runs()[0]
    assert EvaluationRun.model_validate_json(run.model_dump_json()) == run
    assert run.usage.reported_cost_usd is None
    with pytest.raises(ValidationError):
        EvaluationPolicy(minimum_repeats=1)


def test_changed_suite_does_not_inherit_old_results() -> None:
    suite = _suite()
    cases = list(suite.cases)
    cases[0] = cases[0].model_copy(update={"workflow_id": "changed-baseline"})
    changed = suite.model_copy(update={"cases": tuple(cases)})
    result = assess_research_evidence(
        suite=changed,
        configuration=_configuration(),
        runs=_runs(),
        policy=EvaluationPolicy(),
        now=NOW,
    )
    assert not result.qualified
    assert result.evidence_run_ids == ()
    assert result.suite_fingerprint == changed.fingerprint


def test_recovered_results_must_replace_the_full_recent_trial_window() -> None:
    runs = _runs()
    failed = runs[0].model_copy(
        update={"run_id": uuid4(), "finished_at": NOW - timedelta(minutes=1), "checks": ()}
    )
    runs.append(failed)
    for seed in range(3):
        runs.append(
            runs[0].model_copy(update={"run_id": uuid4(), "finished_at": NOW, "seed": 10 + seed})
        )
    result = assess_research_evidence(
        suite=_suite(),
        configuration=_configuration(),
        runs=runs,
        policy=EvaluationPolicy(),
        now=NOW,
    )
    assert result.qualified
    assert failed.run_id not in result.evidence_run_ids
    assert result.assessed_at == NOW
    assert result.policy == EvaluationPolicy()


def test_assessment_rejects_an_ambiguous_clock() -> None:
    with pytest.raises(ValueError, match="explicit timezone"):
        assess_research_evidence(
            suite=_suite(),
            configuration=_configuration(),
            runs=_runs(),
            policy=EvaluationPolicy(),
            now=NOW.replace(tzinfo=None),
        )


def test_trial_rejects_duplicate_checks_and_backwards_time() -> None:
    run = _runs()[0]
    for change in (
        {"checks": (*run.checks, run.checks[0])},
        {"finished_at": run.started_at - timedelta(seconds=1)},
    ):
        with pytest.raises(ValidationError):
            EvaluationRun.model_validate({**run.model_dump(), **change})
