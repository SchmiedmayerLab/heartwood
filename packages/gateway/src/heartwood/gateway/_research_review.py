# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Confined read-only evidence capture for independently assessed review claims."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence

from heartwood.core_adapter.research_checks import MAX_RESEARCH_TEXT_BYTES
from heartwood.core_adapter.research_corrections import (
    assess_research_correction,
    plan_research_correction,
)
from heartwood.core_adapter.research_review import assess_research_review
from heartwood.gateway._workspace import WorkspaceInspectionError, WorkspaceInspector
from heartwood.schemas.experiments import ExperimentFile
from heartwood.schemas.review import (
    ResearchReviewRun,
    ReviewArtifact,
    ReviewAssessment,
    ReviewCorrectionAssessment,
    ReviewCorrectionCheck,
    ReviewCorrectionPlan,
    ReviewSnapshot,
    ReviewSubmission,
)


class ResearchReviewEvaluator:
    """Reuse workspace inspection; never execute reviewer-supplied checks or commands."""

    def __init__(self, workspace: WorkspaceInspector) -> None:
        self.workspace = workspace

    def prepare(self, artifacts: Mapping[str, str]) -> ReviewSnapshot:
        """Bind declared roles to the exact bytes returned by one confined file read."""
        if not 1 <= len(artifacts) <= 32:
            raise ValueError("A review requires between one and thirty-two artifacts")
        captured = []
        for name, path in sorted(artifacts.items()):
            text = self._read(path)
            if text is None:
                raise ValueError("Review evidence requires complete bounded UTF-8 project files")
            content = text.encode("utf-8")
            captured.append(
                ReviewArtifact(
                    artifact_id=name,
                    file=ExperimentFile(
                        path=path,
                        sha256=hashlib.sha256(content).hexdigest(),
                        size_bytes=len(content),
                    ),
                )
            )
        return ReviewSnapshot(artifacts=tuple(captured))

    def assess(
        self, snapshot: ReviewSnapshot, submissions: Sequence[ReviewSubmission]
    ) -> ReviewAssessment:
        """Reject changed context and retain unavailable evidence without a false finding."""
        snapshot = ReviewSnapshot.model_validate(snapshot)
        return assess_research_review(snapshot, submissions, observed=self._observed(snapshot))

    def prepare_correction(
        self, review: ResearchReviewRun, *, output_directory: str
    ) -> ReviewCorrectionPlan:
        """Require unchanged reviewed evidence and an unused confined destination."""
        review = ResearchReviewRun.model_validate(review)
        plan = plan_research_correction(
            review, output_directory=output_directory, observed=self._observed(review.snapshot)
        )
        if not self.workspace.is_absent(plan.output_directory):
            raise ValueError("Correction destination must be absent beneath an existing directory")
        return plan

    def assess_correction(
        self, review: ResearchReviewRun, plan: ReviewCorrectionPlan
    ) -> ReviewCorrectionAssessment:
        """Recheck declared new files while preserving every original reviewed artifact."""
        review = ResearchReviewRun.model_validate(review)
        plan = ReviewCorrectionPlan.model_validate(plan)
        observed = self._observed(review.snapshot)
        if (
            plan_research_correction(
                review, output_directory=plan.output_directory, observed=observed
            )
            != plan
        ):
            raise ValueError("Correction plan does not match the verified review")
        replacements = {item.artifact_id: item.path for item in plan.outputs}
        paths = {
            item.artifact_id: replacements.get(item.artifact_id, item.file.path)
            for item in review.snapshot.artifacts
        }
        try:
            corrected = self.prepare(paths)
        except ValueError:
            return ReviewCorrectionAssessment(
                plan_sha256=plan.fingerprint,
                checks=tuple(
                    ReviewCorrectionCheck(
                        finding_id=identity,
                        status="unavailable",
                        reason="missing-correction-evidence",
                    )
                    for identity in plan.finding_ids
                ),
            )
        result = assess_research_correction(
            review,
            plan,
            corrected,
            original_observed=observed,
            corrected_observed=self._observed(corrected),
        )
        if self._observed(review.snapshot) != observed:
            raise ValueError("Original evidence changed during correction assessment")
        return result

    def _observed(self, snapshot: ReviewSnapshot) -> dict[str, str]:
        return {
            item.artifact_id: text
            for item in snapshot.artifacts
            if (text := self._read(item.file.path)) is not None
        }

    def _read(self, path: str) -> str | None:
        try:
            response = self.workspace.file(path)
        except WorkspaceInspectionError:
            return None
        text = response["content"]
        if response["status"] != "available" or response["truncated"] or text is None:
            return None
        if len(text.encode("utf-8")) > MAX_RESEARCH_TEXT_BYTES:
            return None
        return text
