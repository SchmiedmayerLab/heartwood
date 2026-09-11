# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Independent evaluation of research benchmark evidence across platforms."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from uuid import UUID

from heartwood.schemas.evaluation import (
    EvaluationAssessment,
    EvaluationConfiguration,
    EvaluationPolicy,
    EvaluationRun,
    EvaluationSuite,
)


def assess_research_evidence(
    *,
    suite: EvaluationSuite,
    configuration: EvaluationConfiguration,
    runs: Sequence[EvaluationRun],
    policy: EvaluationPolicy,
    now: datetime,
) -> EvaluationAssessment:
    """Require recent repeated independent checks, scoped to one exact configuration.

    Deterministic trials test the harness but never qualify a model.
    The most recent trials decide each case so a regression invalidates older passes.
    This pure function does not run an agent, relax policy, or change catalog choices.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Evidence assessment requires an explicit timezone")
    if len({run.run_id for run in runs}) != len(runs):
        raise ValueError("Duplicate evaluation run identity")
    fingerprint = configuration.fingerprint
    reasons: set[str] = set()
    evidence: list[UUID] = []
    if configuration.model_revision is None:
        reasons.add("model_revision_unknown")
    if configuration.runtime_fingerprint is None:
        reasons.add("runtime_unbound")
    cutoff = now - timedelta(days=policy.maximum_age_days)
    scoped = [
        run
        for run in runs
        if run.suite_id == suite.suite_id
        and run.suite_fingerprint == suite.fingerprint
        and run.configuration.fingerprint == fingerprint
        and run.execution == "live_model"
    ]
    if any(run.finished_at > now for run in scoped):
        reasons.add("future_evidence")
    for case in suite.cases:
        candidates = sorted(
            (
                run
                for run in scoped
                if run.case_id == case.case_id
                and run.fixture_digest == case.fixture_digest
                and cutoff <= run.finished_at <= now
            ),
            key=lambda run: (run.finished_at, str(run.run_id)),
            reverse=True,
        )[: policy.minimum_repeats]
        evidence.extend(run.run_id for run in candidates)
        if len(candidates) < policy.minimum_repeats:
            reasons.add(f"{case.case_id}:insufficient_repeats")
        required = {check.check_id: check.dimension for check in case.required_checks}
        for run in candidates:
            runtime = run.runtime_observation
            if runtime is None:
                reasons.add(f"{case.case_id}:runtime_unobserved")
            else:
                if runtime.source != "production":
                    reasons.add(f"{case.case_id}:runtime_not_production")
                if runtime.fingerprint != configuration.runtime_fingerprint:
                    reasons.add(f"{case.case_id}:runtime_mismatch")
                if runtime.declaration_mismatches(configuration):
                    reasons.add(f"{case.case_id}:runtime_declaration_mismatch")
                if any(
                    value is None
                    for value in (
                        runtime.request_model,
                        runtime.openhands_version,
                        runtime.model_options_fingerprint,
                    )
                ):
                    reasons.add(f"{case.case_id}:runtime_identity_unknown")
                if runtime.max_input_tokens is None or runtime.max_output_tokens is None:
                    reasons.add(f"{case.case_id}:runtime_context_unknown")
            if run.status != "completed":
                reasons.add(f"{case.case_id}:incomplete_trial")
            for limit in run.usage.exceeded_limits(run.budget):
                reasons.add(f"{case.case_id}:budget_exceeded:{limit}")
            observed = {check.check_id: check for check in run.checks}
            for check_id, dimension in required.items():
                check = observed.get(check_id)
                if check is None or check.dimension != dimension or check.status != "passed":
                    reasons.add(f"{case.case_id}:{check_id}:not_passed")
            if any(check.status == "failed" for check in run.checks):
                reasons.add(f"{case.case_id}:failed_check")
    return EvaluationAssessment(
        configuration_fingerprint=fingerprint,
        suite_fingerprint=suite.fingerprint,
        assessed_at=now,
        policy=policy,
        qualified=not reasons,
        reasons=tuple(sorted(reasons)),
        evidence_run_ids=tuple(evidence),
    )
