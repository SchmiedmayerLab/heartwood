# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Compare matched sequential and parallel reviews without changing execution policy."""

from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from statistics import median
from types import MappingProxyType

from heartwood.model_policy.evaluation import assess_research_evidence
from heartwood.schemas.evaluation import (
    EvaluationConfiguration,
    EvaluationDimension,
    EvaluationPolicy,
    EvaluationRun,
    EvaluationRuntimeObservation,
    EvaluationSuite,
)
from heartwood.schemas.parallel_reviews import (
    ParallelReviewAssessment,
    ParallelReviewComparison,
    ParallelReviewPlan,
    ParallelReviewPolicy,
    ParallelReviewTrialPlan,
    ReviewExecutionScope,
    ReviewQualificationEvidence,
)

PARALLEL_REVIEW_CHECKS = MappingProxyType(
    {
        "review.schedule": EvaluationDimension.TOOL_COMPATIBILITY,
        "review.isolation": EvaluationDimension.POLICY_ADHERENCE,
        "review.lineage": EvaluationDimension.RECOVERY,
        "review.findings": EvaluationDimension.STATISTICAL_CORRECTNESS,
        "review.synthesis": EvaluationDimension.WORKFLOW_COMPLETION,
    }
)


def assess_parallel_reviews(
    *,
    suite: EvaluationSuite,
    sequential: EvaluationConfiguration,
    parallel: EvaluationConfiguration,
    runs: Sequence[EvaluationRun],
    policy: ParallelReviewPolicy,
    now: datetime,
) -> ParallelReviewAssessment:
    """Require exact workload pairing, complete quality gates and a bounded latency benefit.

    Callers supply retained trials from their trusted evaluation path. This verifies
    their declared associations, not the authorship of an arbitrary JSON document.
    """
    suite = EvaluationSuite.model_validate(suite.model_dump())
    sequential = EvaluationConfiguration.model_validate(sequential.model_dump())
    parallel = EvaluationConfiguration.model_validate(parallel.model_dump())
    policy = ParallelReviewPolicy.model_validate(policy.model_dump())
    runs = tuple(EvaluationRun.model_validate(run.model_dump()) for run in runs)
    evidence_policy = EvaluationPolicy(
        minimum_repeats=policy.minimum_repeats, maximum_age_days=policy.maximum_age_days
    )
    baseline = assess_research_evidence(
        suite=suite, configuration=sequential, runs=runs, policy=evidence_policy, now=now
    )
    candidate = assess_research_evidence(
        suite=suite, configuration=parallel, runs=runs, policy=evidence_policy, now=now
    )
    reasons = {f"sequential:{reason}" for reason in baseline.reasons}
    reasons.update(f"parallel:{reason}" for reason in candidate.reasons)
    if sequential.specialist_concurrency != 1 or parallel.specialist_concurrency <= 1:
        reasons.add("invalid_concurrency_comparison")
    if parallel.specialist_concurrency > policy.maximum_workers:
        reasons.add("worker_limit_exceeded")
    excluded = {"specialist_concurrency", "runtime_fingerprint"}
    if sequential.model_dump(exclude=excluded) != parallel.model_dump(exclude=excluded):
        reasons.add("configuration_changed")
    if sequential.specialist_catalog_fingerprint is None:
        reasons.add("specialist_catalog_unbound")
    comparisons: list[ParallelReviewComparison] = []
    before = {run.run_id: run for run in runs if run.run_id in baseline.evidence_run_ids}
    after = {run.run_id: run for run in runs if run.run_id in candidate.evidence_run_ids}
    for case in suite.cases:
        if len(case.specialist_ids) < 2:
            reasons.add(f"{case.case_id}:reviewers_unbound")
        elif parallel.specialist_concurrency > len(case.specialist_ids):
            reasons.add(f"{case.case_id}:workers_exceed_reviewers")
        if case.review_stage_id is None:
            reasons.add(f"{case.case_id}:review_stage_unbound")
        checks = {check.check_id: check.dimension for check in case.required_checks}
        if any(checks.get(key) != dimension for key, dimension in PARALLEL_REVIEW_CHECKS.items()):
            reasons.add(f"{case.case_id}:review_checks_missing")
        left = sorted(
            (run for run in before.values() if run.case_id == case.case_id),
            key=lambda run: run.seed,
        )
        right = sorted(
            (run for run in after.values() if run.case_id == case.case_id), key=lambda run: run.seed
        )
        if (
            len(left) != policy.minimum_repeats
            or len(right) != policy.minimum_repeats
            or [run.seed for run in left] != [run.seed for run in right]
        ):
            reasons.add(f"{case.case_id}:unmatched_trials")
            continue
        for original, concurrent in zip(left, right, strict=True):
            if original.budget != concurrent.budget:
                reasons.add(f"{case.case_id}:budget_changed")
            a, b = original.runtime_observation, concurrent.runtime_observation
            if a is None or b is None or a.specialist_catalog_fingerprint is None or a != b:
                reasons.add(f"{case.case_id}:runtime_changed")
        if any((run.finished_at - run.started_at).total_seconds() <= 0 for run in (*left, *right)):
            reasons.add(f"{case.case_id}:duration_unavailable")
            continue
        if any(
            (run.finished_at - run.started_at).total_seconds() > run.budget.maximum_seconds
            for run in (*left, *right)
        ):
            reasons.add(f"{case.case_id}:duration_budget_exceeded")
        if any(
            run.usage.reported_cost_usd is None
            or run.usage.input_tokens is None
            or run.usage.output_tokens is None
            or run.usage.model_calls is None
            or run.usage.model_calls == 0
            or (run.usage.input_tokens == 0 and run.usage.output_tokens == 0)
            for run in (*left, *right)
        ):
            reasons.add(f"{case.case_id}:usage_unavailable")
            continue
        sequential_cost = sum(Decimal(str(run.usage.reported_cost_usd)) for run in left)
        parallel_cost = sum(Decimal(str(run.usage.reported_cost_usd)) for run in right)
        comparison = ParallelReviewComparison(
            case_id=case.case_id,
            sequential_seconds=median(
                (run.finished_at - run.started_at).total_seconds() for run in left
            ),
            parallel_seconds=median(
                (run.finished_at - run.started_at).total_seconds() for run in right
            ),
            sequential_reported_cost_usd=float(sequential_cost),
            parallel_reported_cost_usd=float(parallel_cost),
            sequential_tokens=sum(
                (run.usage.input_tokens or 0) + (run.usage.output_tokens or 0) for run in left
            ),
            parallel_tokens=sum(
                (run.usage.input_tokens or 0) + (run.usage.output_tokens or 0) for run in right
            ),
        )
        comparisons.append(comparison)
        if comparison.parallel_seconds > comparison.sequential_seconds * (
            1 - policy.minimum_latency_reduction
        ) or sum((run.finished_at - run.started_at).total_seconds() for run in right) > sum(
            (run.finished_at - run.started_at).total_seconds() for run in left
        ) * (1 - policy.minimum_latency_reduction):
            reasons.add(f"{case.case_id}:latency_benefit_insufficient")
        if parallel_cost > sequential_cost * Decimal(str(policy.maximum_cost_ratio)):
            reasons.add(f"{case.case_id}:cost_increase_excessive")
        if comparison.parallel_tokens > comparison.sequential_tokens * policy.maximum_token_ratio:
            reasons.add(f"{case.case_id}:token_increase_excessive")
    return ParallelReviewAssessment(
        sequential=baseline,
        parallel=candidate,
        policy=policy,
        comparisons=tuple(comparisons),
        qualified=not reasons,
        reasons=tuple(sorted(reasons)),
    )


def prepare_parallel_review(
    *,
    scope: ReviewExecutionScope,
    case_id: str,
    suite: EvaluationSuite,
    sequential: EvaluationConfiguration,
    parallel: EvaluationConfiguration,
    runtime: EvaluationRuntimeObservation,
    configuration: EvaluationConfiguration,
    runs: Sequence[EvaluationRun],
    policy: ParallelReviewPolicy,
    now: datetime,
    consent_fingerprint: str | None = None,
) -> ParallelReviewPlan:
    """Recompute a preview from trusted evidence and optionally require its exact consent.

    The caller supplies deployment-owned evidence and gateway-observed session state,
    never an assessment or eligibility flag from a model or browser. This does not
    approve tools, reserve provider capacity, or dispatch work. Re-run it at admission.
    """
    scope = ReviewExecutionScope.model_validate(scope.model_dump())
    runtime = EvaluationRuntimeObservation.model_validate(runtime.model_dump())
    configuration = EvaluationConfiguration.model_validate(configuration.model_dump())
    runs = tuple(EvaluationRun.model_validate(run.model_dump()) for run in runs)
    assessment = assess_parallel_reviews(
        suite=suite, sequential=sequential, parallel=parallel, runs=runs, policy=policy, now=now
    )
    if not assessment.qualified:
        raise ValueError(
            "Parallel review evidence is not qualified: " + ", ".join(assessment.reasons)
        )
    case = next((item for item in suite.cases if item.case_id == case_id), None)
    if (
        case is None
        or case.workflow_id != scope.workflow_id
        or case.review_stage_id != scope.stage_id
        or set(case.specialist_ids) != set(scope.reviewer_ids)
        or parallel.specialist_concurrency != scope.workers
    ):
        raise ValueError("Parallel review evidence does not cover the requested work")
    if (
        runtime.tool_concurrency != 1
        or not runtime.scoped_advisory_reviews
        or runtime.fingerprint != parallel.runtime_fingerprint
        or runtime.fingerprint != sequential.runtime_fingerprint
        or configuration.fingerprint != sequential.fingerprint
    ):
        raise ValueError("The current runtime differs from the qualified review route")
    selected_ids = set(assessment.sequential.evidence_run_ids) | set(
        assessment.parallel.evidence_run_ids
    )
    selected = sorted(
        (run for run in runs if run.run_id in selected_ids), key=lambda run: str(run.run_id)
    )
    requested_limits = scope.budget.model_dump()
    if any(
        any(value > run.budget.model_dump()[name] for name, value in requested_limits.items())
        for run in selected
        if run.case_id == case_id
    ):
        raise ValueError("Requested review limits exceed the qualified workload budget")
    plan = ParallelReviewPlan(
        scope=scope,
        suite_fingerprint=suite.fingerprint,
        case_id=case_id,
        sequential_configuration_fingerprint=sequential.fingerprint,
        parallel_configuration_fingerprint=parallel.fingerprint,
        policy=policy,
        evidence=tuple(
            ReviewQualificationEvidence(run_id=run.run_id, record_fingerprint=run.fingerprint)
            for run in selected
        ),
        valid_until=min(run.finished_at for run in selected)
        + timedelta(days=policy.maximum_age_days),
    )
    if consent_fingerprint is not None and consent_fingerprint != plan.fingerprint:
        raise ValueError("Parallel review consent changed; refresh and confirm the current plan")
    return plan


def prepare_parallel_review_trial(
    *,
    scope: ReviewExecutionScope,
    suite: EvaluationSuite,
    trial: EvaluationRun,
    runtime: EvaluationRuntimeObservation,
    now: datetime,
) -> ParallelReviewTrialPlan:
    """Preview one reserved synthetic trial without requiring prior qualification.

    The evaluation harness owns the reserved incomplete record and must verify the
    pinned fixtures in its isolated project. This pure check neither authenticates
    arbitrary records nor approves actions. Re-read the reservation at dispatch.
    """
    scope = ReviewExecutionScope.model_validate(scope.model_dump())
    suite = EvaluationSuite.model_validate(suite.model_dump())
    trial = EvaluationRun.model_validate(trial.model_dump())
    runtime = EvaluationRuntimeObservation.model_validate(runtime.model_dump())
    case = next((item for item in suite.cases if item.case_id == trial.case_id), None)
    if (
        case is None
        or trial.suite_id != suite.suite_id
        or trial.suite_fingerprint != suite.fingerprint
        or trial.fixture_digest != case.fixture_digest
        or trial.status != "incomplete"
        or trial.session_id != scope.session_id
        or case.workflow_id != scope.workflow_id
        or case.review_stage_id != scope.stage_id
        or set(case.specialist_ids) != set(scope.reviewer_ids)
        or trial.configuration.specialist_concurrency != scope.workers
        or tuple((check.check_id, check.dimension) for check in trial.checks)
        != tuple((check.check_id, check.dimension) for check in case.required_checks)
    ):
        raise ValueError("Experimental review requires its exact reserved case and session")
    checks = {check.check_id: check.dimension for check in case.required_checks}
    if any(checks.get(key) != dimension for key, dimension in PARALLEL_REVIEW_CHECKS.items()):
        raise ValueError("Experimental review requires scheduling and independent review checks")
    if (
        runtime.backend != "openhands-sdk"
        or runtime.tool_concurrency != 1
        or not runtime.scoped_advisory_reviews
        or runtime.specialist_catalog_fingerprint is None
        or trial.runtime_observation != runtime
        or trial.configuration.runtime_fingerprint != runtime.fingerprint
        or runtime.declaration_mismatches(trial.configuration)
        or (trial.execution == "live_model" and runtime.source != "production")
    ):
        raise ValueError("Experimental review runtime changed or cannot run scoped specialists")
    valid_until = trial.started_at + timedelta(seconds=trial.budget.maximum_seconds)
    if not trial.started_at <= now < valid_until:
        raise ValueError("Experimental review reservation is not current")
    if any(
        value > trial.budget.model_dump()[name] for name, value in scope.budget.model_dump().items()
    ):
        raise ValueError("Experimental review exceeds its reserved work limits")
    return ParallelReviewTrialPlan(
        scope=scope,
        suite_fingerprint=suite.fingerprint,
        case_id=trial.case_id,
        trial_id=trial.run_id,
        reservation_fingerprint=trial.fingerprint,
        seed=trial.seed,
        configuration_fingerprint=trial.configuration.fingerprint,
        runtime_fingerprint=runtime.fingerprint,
        valid_until=valid_until,
    )
