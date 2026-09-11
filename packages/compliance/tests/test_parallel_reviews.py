# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Matched evidence gates cannot substitute speed for review quality or missing usage."""

from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, uuid5

import pytest
from pydantic import ValidationError

from heartwood.compliance.parallel_reviews import PARALLEL_REVIEW_CHECKS, assess_parallel_reviews
from heartwood.schemas.evaluation import (
    EvaluationCase,
    EvaluationCheck,
    EvaluationConfiguration,
    EvaluationDimension,
    EvaluationRun,
    EvaluationRuntimeObservation,
    EvaluationSuite,
    RequiredEvaluationCheck,
)
from heartwood.schemas.execution import ExecutionUsage
from heartwood.schemas.parallel_reviews import ParallelReviewAssessment, ParallelReviewPolicy

NOW = datetime(2026, 9, 11, tzinfo=UTC)


def _runtime(workers: int) -> EvaluationRuntimeObservation:
    return EvaluationRuntimeObservation(
        backend="openhands-sdk",
        source="production",
        request_model="test/model",
        openhands_version="1.46.0",
        model_options_fingerprint="a" * 64,
        platform="generic",
        policy_fingerprint="b" * 64,
        action_confirmation="always-confirm",
        max_input_tokens=32768,
        max_output_tokens=4096,
        specialist_concurrency=workers,
        specialist_catalog_fingerprint="c" * 64,
    )


def _configuration(workers: int) -> EvaluationConfiguration:
    return EvaluationConfiguration(
        provider="synthetic",
        model="test/model",
        request_model="test/model",
        model_revision="d" * 40,
        platform="generic",
        hardware=("api",),
        runtime="synthetic-api",
        openhands_version="1.46.0",
        precision="declared",
        context_tokens=32768,
        output_tokens=4096,
        tool_parser="native",
        skill_tree_digest="e" * 64,
        harness_revision="f" * 64,
        runtime_fingerprint=_runtime(workers).fingerprint,
        specialist_concurrency=workers,
        specialist_catalog_fingerprint="c" * 64,
    )


def _suite() -> EvaluationSuite:
    checks = {dimension.value: dimension for dimension in EvaluationDimension}
    checks.update(PARALLEL_REVIEW_CHECKS)
    return EvaluationSuite(
        suite_id="synthetic-review.v1",
        cases=(
            EvaluationCase(
                case_id="seeded-review",
                workflow_id="baseline-analysis",
                fixture_digest="0" * 64,
                required_checks=tuple(
                    RequiredEvaluationCheck(check_id=key, dimension=value)
                    for key, value in checks.items()
                ),
            ),
        ),
    )


def _runs() -> list[EvaluationRun]:
    return [
        EvaluationRun(
            run_id=uuid5(NAMESPACE_URL, f"synthetic-review:{workers}:{seed}"),
            suite_id=_suite().suite_id,
            suite_fingerprint=_suite().fingerprint,
            case_id="seeded-review",
            fixture_digest="0" * 64,
            seed=seed,
            execution="live_model",
            configuration=_configuration(workers),
            runtime_observation=_runtime(workers),
            started_at=NOW - timedelta(hours=1, minutes=seed * 3),
            finished_at=NOW
            - timedelta(hours=1, minutes=seed * 3)
            + timedelta(seconds=100 / workers),
            checks=tuple(
                EvaluationCheck(**check.model_dump(), status="passed")
                for check in _suite().cases[0].required_checks
            ),
            usage=ExecutionUsage(
                input_tokens=200,
                output_tokens=100,
                model_calls=4,
                reported_cost_usd=0.01,
                elapsed_seconds=100 / workers,
            ),
        )
        for workers in (1, 2)
        for seed in range(3)
    ]


def _assess(runs: list[EvaluationRun]) -> ParallelReviewAssessment:
    return assess_parallel_reviews(
        suite=_suite(),
        sequential=_configuration(1),
        parallel=_configuration(2),
        runs=runs,
        policy=ParallelReviewPolicy(),
        now=NOW,
    )


def test_matched_trials_qualify_without_changing_inputs_and_are_order_independent() -> None:
    runs = _runs()
    original = [run.model_dump() for run in runs]
    result = _assess(runs)
    assert result.qualified
    assert result == _assess(list(reversed(runs)))
    assert len(result.parallel.evidence_run_ids) == len(result.sequential.evidence_run_ids) == 3
    comparison = result.comparisons[0]
    assert comparison.sequential_seconds == 100
    assert comparison.parallel_seconds == 50
    assert comparison.parallel_reported_cost_usd == pytest.approx(0.03)
    assert comparison.parallel_tokens == 900
    assert [run.model_dump() for run in runs] == original


@pytest.mark.parametrize(
    "damage",
    [
        "slow",
        "cost",
        "tokens",
        "unknown-cost",
        "unknown-tokens",
        "unknown-calls",
        "zero-calls",
        "zero-tokens",
        "failed-review",
        "failed-isolation",
        "missing-overlap",
        "injected",
        "deterministic",
        "expired",
        "future",
        "seed",
        "budget",
        "zero-duration",
        "duration-budget",
        "incomplete",
        "runtime",
    ],
)
def test_faster_trials_cannot_qualify_with_missing_or_regressed_evidence(damage: str) -> None:
    runs = _runs()
    item = runs[-1]
    updates: dict[str, object] = {}
    if damage in {
        "cost",
        "tokens",
        "unknown-cost",
        "unknown-tokens",
        "unknown-calls",
        "zero-calls",
        "zero-tokens",
    }:
        usage_changes: dict[str, dict[str, object]] = {
            "cost": {"reported_cost_usd": 0.9},
            "tokens": {"input_tokens": 10_000},
            "unknown-cost": {"reported_cost_usd": None},
            "unknown-tokens": {"input_tokens": None},
            "unknown-calls": {"model_calls": None},
            "zero-calls": {"model_calls": 0},
            "zero-tokens": {"input_tokens": 0, "output_tokens": 0},
        }
        updates["usage"] = item.usage.model_copy(update=usage_changes[damage])
    elif damage in {"failed-review", "failed-isolation", "missing-overlap"}:
        key = {
            "failed-review": "review.findings",
            "failed-isolation": "review.isolation",
            "missing-overlap": "review.schedule",
        }[damage]
        updates["checks"] = tuple(
            check.model_copy(update={"status": "failed"}) if check.check_id == key else check
            for check in item.checks
        )
    elif damage == "slow":
        updates["finished_at"] = item.started_at + timedelta(seconds=190)
    elif damage == "injected":
        updates["runtime_observation"] = _runtime(2).model_copy(update={"source": "injected"})
    elif damage == "deterministic":
        updates["execution"] = "deterministic"
    elif damage == "expired":
        updates.update(started_at=NOW - timedelta(days=31), finished_at=NOW - timedelta(days=31))
    elif damage == "future":
        updates["finished_at"] = NOW + timedelta(minutes=1)
    elif damage == "seed":
        updates["seed"] = 99
    elif damage == "budget":
        updates["budget"] = item.budget.model_copy(update={"maximum_model_calls": 10})
    elif damage == "zero-duration":
        updates["finished_at"] = item.started_at
    elif damage == "duration-budget":
        updates["finished_at"] = item.started_at + timedelta(seconds=301)
    elif damage == "incomplete":
        updates.update(status="incomplete", checks=())
    elif damage == "runtime":
        updates["runtime_observation"] = _runtime(2).model_copy(
            update={"model_options_fingerprint": "9" * 64}
        )
    runs[-1] = item.model_copy(update=updates)
    assert not _assess(runs).qualified


@pytest.mark.parametrize(
    "field", ["model", "skill_tree_digest", "harness_revision", "specialist_catalog_fingerprint"]
)
def test_different_configuration_cannot_inherit_baseline(field: str) -> None:
    parallel = _configuration(2).model_copy(update={field: "9" * 64})
    runs = [
        run.model_copy(update={"configuration": parallel})
        if run.configuration.specialist_concurrency == 2
        else run
        for run in _runs()
    ]
    result = assess_parallel_reviews(
        suite=_suite(),
        sequential=_configuration(1),
        parallel=parallel,
        runs=runs,
        policy=ParallelReviewPolicy(),
        now=NOW,
    )
    assert not result.qualified
    assert "configuration_changed" in result.reasons


def test_generic_research_checks_are_not_parallel_review_qualification() -> None:
    case = _suite().cases[0]
    suite = _suite().model_copy(
        update={
            "cases": (
                case.model_copy(
                    update={
                        "required_checks": tuple(
                            check
                            for check in case.required_checks
                            if not check.check_id.startswith("review.")
                        ),
                    }
                ),
            )
        }
    )
    runs = [run.model_copy(update={"suite_fingerprint": suite.fingerprint}) for run in _runs()]
    result = assess_parallel_reviews(
        suite=suite,
        sequential=_configuration(1),
        parallel=_configuration(2),
        runs=runs,
        policy=ParallelReviewPolicy(),
        now=NOW,
    )
    assert not result.qualified
    assert "seeded-review:review_checks_missing" in result.reasons


@pytest.mark.parametrize("workers", [0, True, 1.5])
def test_concurrency_is_a_positive_integer(workers: object) -> None:
    with pytest.raises(ValidationError):
        EvaluationConfiguration.model_validate(
            {**_configuration(1).model_dump(), "specialist_concurrency": workers}
        )


def test_duplicate_trial_identity_is_rejected() -> None:
    with pytest.raises(ValueError, match="Duplicate"):
        _assess([*_runs(), _runs()[0]])


def test_observed_concurrency_does_not_override_the_policy_limit() -> None:
    parallel = _configuration(16)
    assert _runtime(16).specialist_concurrency == 16
    result = assess_parallel_reviews(
        suite=_suite(),
        sequential=_configuration(1),
        parallel=parallel,
        runs=_runs(),
        policy=ParallelReviewPolicy(),
        now=NOW,
    )
    assert not result.qualified
    assert "worker_limit_exceeded" in result.reasons


def test_independent_repeated_trials_may_share_a_seed() -> None:
    assert _assess([run.model_copy(update={"seed": 42}) for run in _runs()]).qualified


def test_missing_specialist_catalog_cannot_qualify() -> None:
    configurations = {
        workers: _configuration(workers).model_copy(update={"specialist_catalog_fingerprint": None})
        for workers in (1, 2)
    }
    runs = [
        run.model_copy(
            update={"configuration": configurations[run.configuration.specialist_concurrency]}
        )
        for run in _runs()
    ]
    result = assess_parallel_reviews(
        suite=_suite(),
        sequential=configurations[1],
        parallel=configurations[2],
        runs=runs,
        policy=ParallelReviewPolicy(),
        now=NOW,
    )
    assert not result.qualified
    assert "specialist_catalog_unbound" in result.reasons


def test_exact_declared_comparison_limits_are_inclusive() -> None:
    runs = [
        run.model_copy(
            update={
                "finished_at": run.started_at + timedelta(seconds=90),
                "usage": run.usage.model_copy(
                    update={"reported_cost_usd": 0.0125, "input_tokens": 275}
                ),
            }
        )
        if run.configuration.specialist_concurrency == 2
        else run
        for run in _runs()
    ]
    assert _assess(runs).qualified
