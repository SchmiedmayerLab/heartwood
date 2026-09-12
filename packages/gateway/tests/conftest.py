# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Synthetic authorization supplied by tests, never deployment qualification evidence."""

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5
from zipfile import ZipFile

import pytest
import tomli_w

from heartwood.compliance.review_benchmarks import planning_review_suite
from heartwood.schemas.evaluation import (
    EvaluationCheck,
    EvaluationConfiguration,
    EvaluationRun,
    EvaluationRuntimeObservation,
)
from heartwood.schemas.execution import ExecutionBudget, ExecutionUsage
from heartwood.schemas.parallel_reviews import (
    ParallelReviewPlan,
    ParallelReviewPolicy,
    ReviewExecutionScope,
    ReviewQualification,
    ReviewQualificationEvidence,
    ReviewQualifications,
)


@pytest.fixture
def analysis_lock(tmp_path: Path) -> Path:
    """An offline wheel with real importable analysis code, absent from Heartwood itself."""
    wheel = tmp_path / "synthetic_analysis-1.0-py3-none-any.whl"
    with ZipFile(wheel, "w") as archive:
        archive.writestr(
            "synthetic_analysis.py", "def mean(values):\n    return sum(values)/len(values)\n"
        )
        archive.writestr(
            "synthetic_analysis-1.0.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: synthetic-analysis\nVersion: 1.0\n",
        )
        archive.writestr(
            "synthetic_analysis-1.0.dist-info/WHEEL",
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr("synthetic_analysis-1.0.dist-info/RECORD", "")
    lock = tmp_path / "pylock.toml"
    lock.write_text(
        tomli_w.dumps(
            {
                "lock-version": "1.0",
                "created-by": "heartwood-test",
                "packages": [
                    {
                        "name": "synthetic-analysis",
                        "version": "1.0",
                        "wheels": [
                            {
                                "path": wheel.name,
                                "hashes": {
                                    "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()
                                },
                            }
                        ],
                    }
                ],
            }
        )
    )
    return lock


@pytest.fixture
def review_evidence() -> Callable[[EvaluationRuntimeObservation, datetime], ReviewQualifications]:
    """Fabricated retained records for contract tests; never real-model performance evidence."""

    def build(runtime: EvaluationRuntimeObservation, now: datetime) -> ReviewQualifications:
        suite = planning_review_suite()
        sequential = EvaluationConfiguration(
            provider="synthetic",
            model="synthetic",
            request_model=runtime.request_model or "synthetic",
            model_revision="synthetic-revision",
            platform=runtime.platform,
            hardware=("synthetic-api",),
            runtime="synthetic",
            openhands_version=runtime.openhands_version or "1.46.0",
            precision="declared",
            context_tokens=runtime.max_input_tokens or 32768,
            output_tokens=runtime.max_output_tokens or 4096,
            tool_parser="native",
            skill_tree_digest="e" * 64,
            harness_revision="f" * 64,
            runtime_fingerprint=runtime.fingerprint,
            specialist_catalog_fingerprint=runtime.specialist_catalog_fingerprint,
        )
        parallel = sequential.model_copy(update={"specialist_concurrency": 2})
        records = tuple(
            EvaluationRun(
                run_id=uuid5(NAMESPACE_URL, f"qualification-test:{case.case_id}:{workers}:{seed}"),
                suite_id=suite.suite_id,
                suite_fingerprint=suite.fingerprint,
                case_id=case.case_id,
                fixture_digest=case.fixture_digest,
                seed=seed,
                execution="live_model",
                configuration=sequential if workers == 1 else parallel,
                runtime_observation=runtime,
                started_at=now - timedelta(hours=1, minutes=seed * 3),
                finished_at=now
                - timedelta(hours=1, minutes=seed * 3)
                + timedelta(seconds=100 / workers),
                checks=tuple(
                    EvaluationCheck(**check.model_dump(), status="passed")
                    for check in case.required_checks
                ),
                usage=ExecutionUsage(
                    input_tokens=200,
                    output_tokens=100,
                    model_calls=4,
                    reported_cost_usd=0.01,
                    elapsed_seconds=100 / workers,
                ),
            )
            for case in suite.cases
            for workers in (1, 2)
            for seed in range(3)
        )
        return ReviewQualifications(
            routes=(
                ReviewQualification(
                    suite=suite,
                    case_id=suite.cases[0].case_id,
                    sequential=sequential,
                    parallel=parallel,
                    runs=records,
                ),
            )
        )

    return build


@pytest.fixture
def parallel_review_plan() -> ParallelReviewPlan:
    return ParallelReviewPlan(
        scope=ReviewExecutionScope(
            project_fingerprint="1" * 64,
            session_id="session-1",
            workflow_run_id="synthetic-review",
            workflow_id="baseline-analysis",
            stage_id="verification",
            revision=1,
            snapshot_fingerprint="2" * 64,
            reviewer_ids=("data-quality-reviewer", "statistical-reviewer"),
            workers=2,
            budget=ExecutionBudget(),
        ),
        suite_fingerprint="3" * 64,
        case_id="synthetic-review",
        sequential_configuration_fingerprint="4" * 64,
        parallel_configuration_fingerprint="5" * 64,
        policy=ParallelReviewPolicy(),
        evidence=tuple(
            ReviewQualificationEvidence(
                run_id=uuid5(NAMESPACE_URL, f"synthetic-admission:{index}"),
                record_fingerprint=f"{index:064x}",
            )
            for index in range(6)
        ),
        valid_until=datetime.now(UTC) + timedelta(days=1),
    )
