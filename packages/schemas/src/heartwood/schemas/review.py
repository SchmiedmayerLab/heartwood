# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Review proposals and independently assessed evidence share one closed contract."""

from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
from typing import Literal, Self

from pydantic import Field, model_validator

from heartwood.schemas.artifacts import ResearchArtifactPath
from heartwood.schemas.experiments import Digest, ExperimentFile, ExperimentRecord, Reference
from heartwood.schemas.identifiers import WorkflowIdentifier
from heartwood.schemas.parallel_reviews import ParallelReviewPlan, ReviewDispatchAction
from heartwood.schemas.project_paths import project_relative_path
from heartwood.schemas.research import ResearchText

type ReviewCategory = Literal["coding", "statistical", "reproducibility"]
type ReviewSeverity = Literal["low", "medium", "high", "critical"]
type ReviewVerification = Literal["verified", "rejected", "unsupported", "stale", "unavailable"]


class ReviewArtifact(ExperimentRecord):
    """A named evidence role bound to one observed project file."""

    artifact_id: WorkflowIdentifier
    file: ExperimentFile


class ReviewSnapshot(ExperimentRecord):
    """Exact file context authorized for a review, without its contents."""

    schema_version: Literal["heartwood.review-snapshot.v1"] = "heartwood.review-snapshot.v1"
    artifacts: tuple[ReviewArtifact, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def unique_files(self) -> Self:
        """Prevent ambiguous aliases or two roles pretending to be independent files."""
        if len({item.artifact_id for item in self.artifacts}) != len(self.artifacts):
            raise ValueError("Review artifact identities must be unique")
        if len({item.file.path.casefold() for item in self.artifacts}) != len(self.artifacts):
            raise ValueError("Review evidence must reference distinct files")
        return self

    @property
    def fingerprint(self) -> str:
        """Bind named roles and exact file identities independent of presentation order."""
        return review_digest(
            {
                "schema_version": self.schema_version,
                "artifacts": [
                    item.model_dump(mode="json")
                    for item in sorted(self.artifacts, key=lambda item: item.artifact_id)
                ],
            }
        )


class ReviewCandidate(ExperimentRecord):
    """A model proposal cannot assign itself verification or a final disposition."""

    candidate_id: WorkflowIdentifier
    condition: WorkflowIdentifier
    category: ReviewCategory
    severity: ReviewSeverity
    summary: ResearchText
    artifact_ids: tuple[WorkflowIdentifier, ...] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def unique_artifacts(self) -> Self:
        """Do not silently normalize ambiguous model evidence references."""
        if len(set(self.artifact_ids)) != len(self.artifact_ids):
            raise ValueError("Candidate artifact references must be unique")
        return self


class ReviewProposals(ExperimentRecord):
    """Structured model output has no authority to select a reviewer or evidence snapshot."""

    candidates: tuple[ReviewCandidate, ...] = Field(max_length=32)

    @model_validator(mode="after")
    def unique_candidates(self) -> Self:
        """Require a stable identity for every finding within one reviewer result."""
        if len({item.candidate_id for item in self.candidates}) != len(self.candidates):
            raise ValueError("Reviewer candidate identities must be unique")
        return self


class ReviewSubmission(ReviewProposals):
    """Gateway-associated reviewer output for one immutable review context."""

    schema_version: Literal["heartwood.review-submission.v1"] = "heartwood.review-submission.v1"
    review_id: Reference
    reviewer_id: Reference
    snapshot_sha256: Digest

    @classmethod
    def associate(
        cls,
        proposals: ReviewProposals,
        *,
        review_id: str,
        reviewer_id: str,
        snapshot: ReviewSnapshot,
    ) -> Self:
        """Attach caller-owned lineage; this does not authenticate an arbitrary principal."""
        proposals = ReviewProposals.model_validate(proposals)
        snapshot = ReviewSnapshot.model_validate(snapshot)
        return cls(
            review_id=review_id,
            reviewer_id=reviewer_id,
            snapshot_sha256=snapshot.fingerprint,
            candidates=proposals.candidates,
        )


class ReviewSource(ExperimentRecord):
    """Retain each advisory claim without confusing it with verified evidence."""

    review_id: Reference
    reviewer_id: Reference
    candidate: ReviewCandidate


class ReviewFinding(ExperimentRecord):
    """One deduplicated observation; verification never grants action permission."""

    finding_id: Digest
    condition: WorkflowIdentifier
    category: ReviewCategory
    severity: ReviewSeverity
    verification: ReviewVerification
    disposition: Literal["open", "not_actionable"]
    verified_claim: ResearchText | None = None
    reason: WorkflowIdentifier
    evidence: tuple[ReviewArtifact, ...] = ()
    sources: tuple[ReviewSource, ...] = Field(min_length=1, max_length=512)

    @model_validator(mode="after")
    def verified_disposition(self) -> Self:
        """Only independently verified observations can become actionable findings."""
        if self.verification == "verified":
            if self.disposition != "open" or self.verified_claim is None or not self.evidence:
                raise ValueError("Verified findings require a bounded claim and observed evidence")
        elif self.disposition != "not_actionable" or self.verified_claim is not None:
            raise ValueError("Unverified proposals cannot become actionable findings")
        return self


class ReviewAssessment(ExperimentRecord):
    """A deterministic evidence projection, not an approval or a quality benchmark."""

    schema_version: Literal["heartwood.review-assessment.v1"] = "heartwood.review-assessment.v1"
    snapshot_sha256: Digest
    findings: tuple[ReviewFinding, ...] = Field(default=(), max_length=512)


class ResearchReviewRun(ExperimentRecord):
    """Pre-dispatch evidence and native reviewer results retained by the workflow journal."""

    review_id: Reference
    snapshot: ReviewSnapshot
    reviewer_ids: tuple[WorkflowIdentifier, ...] = Field(min_length=1, max_length=16)
    started_sequence: int = Field(ge=0, strict=True)
    status: Literal["pending", "assessed", "unavailable", "cancelled"] = "pending"
    submissions: tuple[ReviewSubmission, ...] = Field(default=(), max_length=16)
    assessment: ReviewAssessment | None = None
    parallel_plan: ParallelReviewPlan | None = None
    parallel_dispatch: tuple[ReviewDispatchAction, ...] = Field(default=(), max_length=16)
    unavailable_reason: (
        Literal["incomplete-review", "invalid-review", "no-structured-outcome"] | None
    ) = None

    @model_validator(mode="after")
    def coherent_evidence(self) -> Self:
        """Persist only results associated with this evidence and selected reviewers."""
        if len(set(self.reviewer_ids)) != len(self.reviewer_ids):
            raise ValueError("Reviewers must be distinct")
        if self.parallel_plan is not None and (
            self.parallel_plan.scope.snapshot_fingerprint != self.snapshot.fingerprint
            or set(self.parallel_plan.scope.reviewer_ids) != set(self.reviewer_ids)
        ):
            raise ValueError("Parallel execution must match the recorded review evidence and roles")
        if self.parallel_dispatch and (
            self.parallel_plan is None
            or len(self.parallel_dispatch) != len(self.reviewer_ids)
            or {item.reviewer_id for item in self.parallel_dispatch} != set(self.reviewer_ids)
            or len({item.event_id for item in self.parallel_dispatch})
            != len(self.parallel_dispatch)
            or len({item.tool_call_id for item in self.parallel_dispatch})
            != len(self.parallel_dispatch)
        ):
            raise ValueError("Parallel dispatch requires one distinct native action per reviewer")
        if len({item.review_id for item in self.submissions}) != len(self.submissions):
            raise ValueError("Review submissions must be distinct")
        if any(
            item.snapshot_sha256 != self.snapshot.fingerprint
            or item.reviewer_id not in self.reviewer_ids
            for item in self.submissions
        ):
            raise ValueError("Review submissions do not match the prepared context")
        if self.status == "pending" and (self.submissions or self.assessment is not None):
            raise ValueError("Pending reviews cannot have assessed results")
        if (self.status == "unavailable") != (self.unavailable_reason is not None):
            raise ValueError("Unavailable reviews require an explicit reason")
        if self.status == "assessed" and (
            self.assessment is None
            or {item.reviewer_id for item in self.submissions} != set(self.reviewer_ids)
        ):
            raise ValueError("An assessed review requires all selected reviewers")
        if (
            self.assessment is not None
            and self.assessment.snapshot_sha256 != self.snapshot.fingerprint
        ):
            raise ValueError("Review assessment belongs to different evidence")
        return self


class ReviewCorrectionPlan(ExperimentRecord):
    """Gateway-selected findings and fresh output locations for one correction attempt."""

    review_id: Reference
    snapshot_sha256: Digest
    finding_ids: tuple[Digest, ...] = Field(min_length=1, max_length=512)
    output_directory: str = Field(min_length=1, max_length=512)
    outputs: tuple[ResearchArtifactPath, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def distinct_outputs(self) -> Self:
        """Require unambiguous evidence identities and confined non-overlapping files."""
        project_relative_path(self.output_directory, allow_root=False)
        if len(set(self.finding_ids)) != len(self.finding_ids):
            raise ValueError("Correction findings must be distinct")
        if len({item.artifact_id for item in self.outputs}) != len(self.outputs):
            raise ValueError("Correction output roles must be distinct")
        directory = PurePosixPath(self.output_directory)
        paths = [PurePosixPath(item.path) for item in self.outputs]
        if any(directory not in path.parents for path in paths):
            raise ValueError("Correction files must be beneath their declared directory")
        folded = [PurePosixPath(str(path).casefold()) for path in paths]
        for index, path in enumerate(folded):
            if any(
                path == other or path in other.parents or other in path.parents
                for other in folded[index + 1 :]
            ):
                raise ValueError("Correction files must not overlap")
        return self

    @property
    def fingerprint(self) -> str:
        """Bind an attempt's selected findings and output scope to one identity."""
        return review_digest(self.model_dump(mode="json"))


class ReviewCorrectionCheck(ExperimentRecord):
    """A narrow defect recheck, not execution evidence or scientific acceptance."""

    finding_id: Digest
    status: Literal["not_observed", "still_observed", "unavailable", "stale"]
    reason: WorkflowIdentifier


class ReviewCorrectionAssessment(ExperimentRecord):
    """Independently observed corrected bytes and checks bound to their proposal."""

    plan_sha256: Digest
    snapshot: ReviewSnapshot | None = None
    checks: tuple[ReviewCorrectionCheck, ...] = Field(min_length=1, max_length=512)

    @model_validator(mode="after")
    def complete_observations(self) -> Self:
        """Do not retain a positive result without complete observed file evidence."""
        if len({item.finding_id for item in self.checks}) != len(self.checks):
            raise ValueError("Correction checks must be distinct")
        if self.snapshot is None and any(
            item.status in {"not_observed", "still_observed"} for item in self.checks
        ):
            raise ValueError("Correction observations require a complete snapshot")
        return self


class ResearchCorrectionAttempt(ExperimentRecord):
    """One journaled parent-agent attempt and its independently observed result."""

    attempt_id: Reference
    plan: ReviewCorrectionPlan
    started_sequence: int = Field(ge=0, strict=True)
    status: Literal["pending", "assessed", "unavailable", "cancelled"] = "pending"
    assessment: ReviewCorrectionAssessment | None = None
    unavailable_reason: (
        Literal["no-structured-outcome", "changed-context", "invalid-evidence", "admission-denied"]
        | None
    ) = None

    @model_validator(mode="after")
    def coherent_result(self) -> Self:
        """Do not attach an assessment to a different plan or incomplete finding set."""
        if (self.status == "assessed") != (self.assessment is not None):
            raise ValueError("Only assessed corrections retain an assessment")
        if (self.status == "unavailable") != (self.unavailable_reason is not None):
            raise ValueError("Unavailable corrections require a reason")
        if self.assessment is not None and (
            self.assessment.plan_sha256 != self.plan.fingerprint
            or {item.finding_id for item in self.assessment.checks} != set(self.plan.finding_ids)
        ):
            raise ValueError("Correction assessment does not match its planned findings")
        return self

    @property
    def defect_not_observed(self) -> bool:
        """Report the narrow recheck, never scientific acceptance or action permission."""
        return self.assessment is not None and all(
            item.status == "not_observed" for item in self.assessment.checks
        )


class ResearchCorrectionRun(ExperimentRecord):
    """Explicit bounded correction consent, using the existing stage's cumulative budget."""

    correction_id: Reference
    stage_id: WorkflowIdentifier
    review: ResearchReviewRun
    maximum_attempts: int = Field(ge=1, le=3, strict=True)
    attempts: tuple[ResearchCorrectionAttempt, ...] = Field(min_length=1, max_length=3)
    stop_reason: (
        Literal["corrected", "attempt-limit", "budget", "unavailable", "cancelled"] | None
    ) = None

    @model_validator(mode="after")
    def bounded_attempts(self) -> Self:
        """Retain one evidence source and distinct, ordered, non-overwriting attempts."""
        if self.review.status != "assessed" or self.review.assessment is None:
            raise ValueError("Correction requires independently assessed review evidence")
        if len(self.attempts) > self.maximum_attempts:
            raise ValueError("Correction attempt limit exceeded")
        if self.stop_reason == "corrected" and not self.attempts[-1].defect_not_observed:
            raise ValueError("Corrected state requires independently checked evidence")
        if self.stop_reason is not None and self.attempts[-1].status == "pending":
            raise ValueError("A stopped correction cannot retain pending execution")
        if len({item.attempt_id for item in self.attempts}) != len(self.attempts):
            raise ValueError("Correction attempt identities must be distinct")
        paths: list[PurePosixPath] = []
        findings = {
            item.finding_id
            for item in self.review.assessment.findings
            if item.verification == "verified"
        }
        original = {item.artifact_id: item.file for item in self.review.snapshot.artifacts}
        previous_sequence = self.review.started_sequence
        for index, attempt in enumerate(self.attempts):
            if (
                attempt.plan.review_id != self.review.review_id
                or attempt.plan.snapshot_sha256 != self.review.snapshot.fingerprint
                or attempt.started_sequence <= previous_sequence
                or set(attempt.plan.finding_ids) != findings
                or not {item.artifact_id for item in attempt.plan.outputs} <= original.keys()
            ):
                raise ValueError("Correction attempt does not match its source review")
            if attempt.assessment is not None and attempt.assessment.snapshot is not None:
                replacements = {item.artifact_id: item.path for item in attempt.plan.outputs}
                observed = {
                    item.artifact_id: item.file for item in attempt.assessment.snapshot.artifacts
                }
                if observed.keys() != original.keys() or any(
                    item.path != replacements.get(role, original[role].path)
                    or (role not in replacements and item != original[role])
                    for role, item in observed.items()
                ):
                    raise ValueError(
                        "Correction observations changed their evidence roles or paths"
                    )
            if index < len(self.attempts) - 1 and (
                attempt.status != "assessed"
                or attempt.defect_not_observed
                or attempt.assessment is None
                or any(
                    check.status not in {"not_observed", "still_observed"}
                    for check in attempt.assessment.checks
                )
            ):
                raise ValueError("Only a completed, still-observed defect can have another attempt")
            directory = PurePosixPath(attempt.plan.output_directory.casefold())
            if any(
                directory == path or directory in path.parents or path in directory.parents
                for path in paths
            ):
                raise ValueError("Correction attempts require separate output directories")
            paths.append(directory)
            previous_sequence = attempt.started_sequence
        return self


def review_digest(value: object) -> str:
    """Fingerprint JSON-compatible review identities with one canonical encoding."""
    content = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()
