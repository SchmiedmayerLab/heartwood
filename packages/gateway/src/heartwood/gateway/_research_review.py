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
from heartwood.core_adapter.research_review import assess_research_review
from heartwood.gateway._workspace import WorkspaceInspectionError, WorkspaceInspector
from heartwood.schemas.experiments import ExperimentFile
from heartwood.schemas.review import (
    ReviewArtifact,
    ReviewAssessment,
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
        observed: dict[str, str] = {}
        for artifact in snapshot.artifacts:
            text = self._read(artifact.file.path)
            if text is not None:
                observed[artifact.artifact_id] = text
        return assess_research_review(snapshot, submissions, observed=observed)

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
