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

from heartwood.model_policy.parallel_reviews import (
    PARALLEL_REVIEW_CHECKS,
    assess_parallel_reviews,
    prepare_parallel_review,
)
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
from heartwood.schemas.execution import ExecutionBudget, ExecutionUsage
from heartwood.schemas.parallel_reviews import (
    ParallelReviewAssessment,
    ParallelReviewPlan,
    ParallelReviewPolicy,
    ReviewExecutionScope,
)

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
                specialist_ids=("statistical-reviewer", "reproduction-reviewer"),
                review_stage_id="verification",
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


def _scope() -> ReviewExecutionScope:
    return ReviewExecutionScope(
        project_fingerprint="1" * 64,
        session_id="research-001",
        workflow_run_id="baseline-001",
        workflow_id="baseline-analysis",
        stage_id="verification",
        revision=4,
        snapshot_fingerprint="2" * 64,
        reviewer_ids=_suite().cases[0].specialist_ids,
        workers=2,
        budget=ExecutionBudget(),
    )


def _prepare(
    *,
    scope: ReviewExecutionScope | None = None,
    runtime: EvaluationRuntimeObservation | None = None,
    configuration: EvaluationConfiguration | None = None,
    runs: list[EvaluationRun] | None = None,
    policy: ParallelReviewPolicy | None = None,
    now: datetime = NOW,
    consent: str | None = None,
) -> ParallelReviewPlan:
    return prepare_parallel_review(
        scope=scope or _scope(),
        case_id="seeded-review",
        suite=_suite(),
        sequential=_configuration(1),
        parallel=_configuration(2),
        runtime=runtime or _runtime(1),
        configuration=configuration or _configuration(1),
        runs=_runs() if runs is None else runs,
        policy=policy or ParallelReviewPolicy(),
        now=now,
        consent_fingerprint=consent,
    )


def test_review_preview_is_stable_until_evidence_changes_or_expires() -> None:
    initial = _prepare()
    assert initial == _prepare(now=NOW + timedelta(minutes=3), consent=initial.fingerprint)
    assert initial == _prepare(runs=list(reversed(_runs())))
    assert len(initial.evidence) == 6
    assert {item.record_fingerprint for item in initial.evidence} == {
        run.fingerprint for run in _runs()
    }
    assert _prepare(now=initial.valid_until, consent=initial.fingerprint) == initial
    with pytest.raises(ValueError, match="not qualified"):
        _prepare(now=initial.valid_until + timedelta(microseconds=1), consent=initial.fingerprint)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("project_fingerprint", "3" * 64),
        ("session_id", "research-002"),
        ("workflow_run_id", "baseline-002"),
        ("revision", 5),
        ("snapshot_fingerprint", "4" * 64),
        ("budget", ExecutionBudget(maximum_tokens=20_000)),
    ],
)
def test_consent_cannot_be_reused_for_changed_work(field: str, value: object) -> None:
    consent = _prepare().fingerprint
    changed = ReviewExecutionScope.model_validate({**_scope().model_dump(), field: value})
    assert _prepare(scope=changed).fingerprint != consent
    with pytest.raises(ValueError, match="consent changed"):
        _prepare(scope=changed, consent=consent)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("workflow_id", "another-workflow"),
        ("stage_id", "another-stage"),
        ("reviewer_ids", ("statistical-reviewer", "untested-reviewer")),
        ("reviewer_ids", (*_suite().cases[0].specialist_ids, "untested-reviewer")),
    ],
)
def test_catalog_qualification_is_not_blanket_review_permission(field: str, value: object) -> None:
    changed = ReviewExecutionScope.model_validate({**_scope().model_dump(), field: value})
    with pytest.raises(ValueError, match="does not cover"):
        _prepare(scope=changed)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source", "injected"),
        ("request_model", "different-model"),
        ("model_options_fingerprint", "5" * 64),
        ("policy_fingerprint", "6" * 64),
        ("specialist_catalog_fingerprint", "7" * 64),
        ("openhands_version", "1.47.0"),
        ("max_input_tokens", 65536),
        ("specialist_concurrency", 2),
    ],
)
def test_changed_live_runtime_requires_qualification(field: str, value: object) -> None:
    runtime = EvaluationRuntimeObservation.model_validate(
        {**_runtime(1).model_dump(), field: value}
    )
    with pytest.raises(ValueError, match="current runtime differs"):
        _prepare(runtime=runtime)


@pytest.mark.parametrize("field", ["harness_revision", "skill_tree_digest", "model_revision"])
def test_changed_configuration_cannot_reuse_a_matching_runtime(field: str) -> None:
    configuration = EvaluationConfiguration.model_validate(
        {**_configuration(1).model_dump(), field: "8" * 64}
    )
    with pytest.raises(ValueError, match="current runtime differs"):
        _prepare(configuration=configuration)


def test_replaced_results_require_new_consent_even_when_still_qualified() -> None:
    consent = _prepare().fingerprint
    runs = _runs()
    runs[-1] = runs[-1].model_copy(
        update={"usage": runs[-1].usage.model_copy(update={"input_tokens": 201})}
    )
    assert _assess(runs).qualified
    with pytest.raises(ValueError, match="consent changed"):
        _prepare(runs=runs, consent=consent)


def test_changed_qualification_policy_requires_new_consent() -> None:
    with pytest.raises(ValueError, match="consent changed"):
        _prepare(
            policy=ParallelReviewPolicy(maximum_cost_ratio=1.1), consent=_prepare().fingerprint
        )


def test_unqualified_evidence_cannot_produce_a_preview() -> None:
    with pytest.raises(ValueError, match="not qualified"):
        _prepare(runs=[])


@pytest.mark.parametrize("specialists", [(), ("statistical-reviewer",)])
def test_a_case_must_pin_multiple_distinct_reviewers(specialists: tuple[str, ...]) -> None:
    suite = _suite().model_copy(
        update={"cases": (_suite().cases[0].model_copy(update={"specialist_ids": specialists}),)}
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
    assert "seeded-review:reviewers_unbound" in result.reasons
    assert not result.qualified


def test_duplicate_reviewers_and_unused_workers_are_invalid() -> None:
    with pytest.raises(ValidationError, match="distinct"):
        ReviewExecutionScope.model_validate(
            {**_scope().model_dump(), "reviewer_ids": ("statistical-reviewer",) * 2}
        )
    with pytest.raises(ValidationError, match="Worker count"):
        ReviewExecutionScope.model_validate({**_scope().model_dump(), "workers": 3})
    with pytest.raises(ValidationError, match="unique"):
        EvaluationCase.model_validate(
            {**_suite().cases[0].model_dump(), "specialist_ids": ("statistical-reviewer",) * 2}
        )


def test_extra_workers_cannot_be_qualified_with_only_two_reviewers() -> None:
    result = assess_parallel_reviews(
        suite=_suite(),
        sequential=_configuration(1),
        parallel=_configuration(3),
        runs=_runs(),
        policy=ParallelReviewPolicy(),
        now=NOW,
    )
    assert "seeded-review:workers_exceed_reviewers" in result.reasons
    assert not result.qualified


def test_review_evidence_must_bind_the_stage_not_just_the_workflow() -> None:
    suite = _suite().model_copy(
        update={"cases": (_suite().cases[0].model_copy(update={"review_stage_id": None}),)}
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
    assert "seeded-review:review_stage_unbound" in result.reasons
    assert not result.qualified


def test_newer_failed_trial_invalidates_a_previously_consented_plan() -> None:
    runs = _runs()
    latest = runs[-1]
    runs.append(
        latest.model_copy(
            update={
                "run_id": uuid5(NAMESPACE_URL, "later-failed-review"),
                "started_at": NOW - timedelta(minutes=5),
                "finished_at": NOW - timedelta(minutes=4),
                "checks": tuple(
                    check.model_copy(update={"status": "failed"}) for check in latest.checks
                ),
            }
        )
    )
    with pytest.raises(ValueError, match="not qualified"):
        _prepare(runs=runs, consent=_prepare().fingerprint)


@pytest.mark.parametrize(
    "field",
    [
        "maximum_seconds",
        "maximum_model_calls",
        "maximum_tokens",
        "maximum_reported_cost_usd",
        "maximum_actions",
    ],
)
def test_qualification_cannot_authorize_larger_untested_budgets(field: str) -> None:
    budget = ExecutionBudget.model_validate(
        {**ExecutionBudget().model_dump(), field: getattr(ExecutionBudget(), field) * 2}
    )
    with pytest.raises(ValueError, match="exceed the qualified workload budget"):
        _prepare(scope=_scope().model_copy(update={"budget": budget}))
