# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Review proposals and independently assessed evidence share one closed contract."""

from __future__ import annotations

import hashlib
import json
from typing import Literal, Self

from pydantic import Field, model_validator

from heartwood.schemas.experiments import Digest, ExperimentFile, ExperimentRecord, Reference
from heartwood.schemas.research import ResearchText
from heartwood.schemas.workflows import WorkflowIdentifier

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


def review_digest(value: object) -> str:
    """Fingerprint JSON-compatible review identities with one canonical encoding."""
    content = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()
