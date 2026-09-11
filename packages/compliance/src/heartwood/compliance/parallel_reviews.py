# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Compare matched sequential and parallel reviews without changing execution policy."""

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from statistics import median
from types import MappingProxyType

from heartwood.compliance.evaluation import assess_research_evidence
from heartwood.schemas.evaluation import (
    EvaluationConfiguration,
    EvaluationDimension,
    EvaluationPolicy,
    EvaluationRun,
    EvaluationSuite,
)
from heartwood.schemas.parallel_reviews import (
    ParallelReviewAssessment,
    ParallelReviewComparison,
    ParallelReviewPolicy,
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
            if (
                a is None
                or b is None
                or a.specialist_catalog_fingerprint is None
                or a.model_dump(exclude={"specialist_concurrency"})
                != b.model_dump(exclude={"specialist_concurrency"})
            ):
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
