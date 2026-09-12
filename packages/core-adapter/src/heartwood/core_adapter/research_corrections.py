# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Read-only correction planning and rechecks over preserved review evidence."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import PurePosixPath
from posixpath import commonpath

from heartwood.core_adapter.research_review import (
    assess_research_review,
    research_correction_roles,
)
from heartwood.schemas.artifacts import ResearchArtifactPath
from heartwood.schemas.review import (
    ResearchReviewRun,
    ReviewCorrectionAssessment,
    ReviewCorrectionCheck,
    ReviewCorrectionPlan,
    ReviewProposals,
    ReviewSnapshot,
    ReviewSubmission,
)


def plan_research_correction(
    review: ResearchReviewRun, *, output_directory: str, observed: Mapping[str, str]
) -> ReviewCorrectionPlan:
    """Revalidate the actual defect before declaring a separate set of correction outputs."""
    review = ResearchReviewRun.model_validate(review)
    if review.status != "assessed" or review.assessment is None:
        raise ValueError("Corrections require a completed independent review")
    actual = assess_research_review(review.snapshot, review.submissions, observed=observed)
    if actual != review.assessment:
        raise ValueError("Review evidence changed or its assessment is inconsistent")
    roles = research_correction_roles(actual)
    if not roles:
        raise ValueError("No independently verified findings can be corrected")
    outputs = tuple(item for item in review.snapshot.artifacts if item.artifact_id in roles)
    common = PurePosixPath(
        commonpath([str(PurePosixPath(item.file.path).parent) for item in outputs])
    )
    directory = PurePosixPath(output_directory.casefold())
    for artifact in review.snapshot.artifacts:
        original = PurePosixPath(artifact.file.path.casefold())
        if directory == original or directory in original.parents or original in directory.parents:
            raise ValueError("Correction destination overlaps the reviewed evidence")
    return ReviewCorrectionPlan(
        review_id=review.review_id,
        snapshot_sha256=review.snapshot.fingerprint,
        finding_ids=tuple(
            item.finding_id for item in actual.findings if item.verification == "verified"
        ),
        output_directory=output_directory,
        outputs=tuple(
            ResearchArtifactPath(
                artifact_id=item.artifact_id,
                path=str(
                    PurePosixPath(output_directory)
                    / PurePosixPath(item.file.path).relative_to(common)
                ),
            )
            for item in sorted(outputs, key=lambda item: item.artifact_id)
        ),
    )


def assess_research_correction(
    review: ResearchReviewRun,
    plan: ReviewCorrectionPlan,
    corrected: ReviewSnapshot,
    *,
    original_observed: Mapping[str, str],
    corrected_observed: Mapping[str, str],
) -> ReviewCorrectionAssessment:
    """Check the same narrow conditions without accepting a changed original or substituted path.

    The caller captures corrected bytes through workspace confinement and owns
    execution provenance. Matching bytes alone do not establish that code ran.
    """
    review = ResearchReviewRun.model_validate(review)
    plan = ReviewCorrectionPlan.model_validate(plan)
    corrected = ReviewSnapshot.model_validate(corrected)
    expected = plan_research_correction(
        review, output_directory=plan.output_directory, observed=original_observed
    )
    if plan != expected:
        raise ValueError("Correction plan does not match the verified review")
    replacements = {item.artifact_id: item.path for item in plan.outputs}
    expected_paths = {
        item.artifact_id: replacements.get(item.artifact_id, item.file.path)
        for item in review.snapshot.artifacts
    }
    if {item.artifact_id: item.file.path for item in corrected.artifacts} != expected_paths:
        raise ValueError("Corrected evidence does not match the declared output paths")
    originals = {item.artifact_id: item for item in review.snapshot.artifacts}
    if any(
        item != originals[item.artifact_id]
        for item in corrected.artifacts
        if item.artifact_id not in replacements
    ):
        raise ValueError("Correction changed protected evidence")
    submissions = tuple(
        ReviewSubmission.associate(
            ReviewProposals(candidates=item.candidates),
            review_id=item.review_id,
            reviewer_id=item.reviewer_id,
            snapshot=corrected,
        )
        for item in review.submissions
    )
    assessment = assess_research_review(corrected, submissions, observed=corrected_observed)
    # Source proposal identities persist while snapshot-bound finding IDs change.
    checked = {
        (item.condition, item.category, tuple(sorted(item.sources[0].candidate.artifact_ids))): item
        for item in assessment.findings
    }
    assert review.assessment is not None
    checks = []
    for finding in review.assessment.findings:
        if finding.finding_id not in plan.finding_ids:
            continue
        key = (
            finding.condition,
            finding.category,
            tuple(sorted(finding.sources[0].candidate.artifact_ids)),
        )
        actual = checked[key]
        checks.append(
            ReviewCorrectionCheck(
                finding_id=finding.finding_id,
                status=(
                    "not_observed"
                    if actual.verification == "rejected"
                    else "still_observed"
                    if actual.verification == "verified"
                    else "stale"
                    if actual.verification == "stale"
                    else "unavailable"
                ),
                reason=actual.reason,
            )
        )
    return ReviewCorrectionAssessment(
        plan_sha256=plan.fingerprint, snapshot=corrected, checks=tuple(checks)
    )
