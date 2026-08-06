# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""OpenHands-native verification for complete Agent Skill directories."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, cast

from heartwood_skill_catalog import CatalogBuildError, SkillPolicy, inspect_skill

from heartwood.schemas import ApprovalRecord

_DEFAULT_ALLOWED_TOOLS: Final[tuple[str, ...]] = ("terminal",)
SkillReview = Literal["repository-reviewed", "local-unreviewed"]


class SkillVerificationError(ValueError):
    """Raised when an Agent Skill directory fails Heartwood policy verification."""


@dataclass(frozen=True, slots=True)
class SkillManifest:
    """Verified projection of one complete Agent Skill directory."""

    skill_id: str
    name: str
    description: str
    root: Path
    policy: SkillPolicy
    declared_tools: tuple[str, ...]
    approval_summary: str
    entrypoint: Path | None
    review: SkillReview
    tree_sha256: str

    @property
    def requires_network(self) -> bool:
        """Return whether the Skill declares network access."""
        return self.policy.requires_network

    @property
    def version(self) -> str:
        """Return the Skill's Semantic Versioning identifier."""
        return self.policy.version


@dataclass(frozen=True, slots=True)
class SkillVerification:
    """Result of verifying one Agent Skill directory."""

    verified: bool
    reason: str
    manifest: SkillManifest | None = None


class LocalSkillVerifier:
    """Verify complete Agent Skills under one root without executing their content."""

    def __init__(
        self,
        root: Path,
        *,
        allowed_tools: tuple[str, ...] = _DEFAULT_ALLOWED_TOOLS,
        review: SkillReview = "local-unreviewed",
        require_repository_review: bool = False,
        allow_network: bool = False,
    ) -> None:
        """Initialize a root-confined verifier and its deployment policy."""
        self.root = root.resolve()
        self.allowed_tools = allowed_tools
        self.review = review
        self.require_repository_review = require_repository_review
        self.allow_network = allow_network

    def verify(self, path: Path) -> SkillVerification:
        """Return a stable verification result for one path."""
        try:
            manifest = self.load_manifest(path)
        except SkillVerificationError as error:
            return SkillVerification(verified=False, reason=str(error))
        return SkillVerification(
            verified=True,
            reason="complete Agent Skill and declared permissions verified",
            manifest=manifest,
        )

    def load_manifest(self, path: Path) -> SkillManifest:
        """Load one Skill through OpenHands and enforce Heartwood policy."""
        skill_root = path.resolve()
        if not skill_root.is_relative_to(self.root):
            raise SkillVerificationError(f"Skill path escapes verification root: {path}")
        manifest = load_skill_manifest(skill_root, review=self.review)
        if self.require_repository_review and manifest.review != "repository-reviewed":
            raise SkillVerificationError("Skill has not passed repository review")
        if manifest.requires_network and not self.allow_network:
            raise SkillVerificationError("Skill requires network access, which is disabled")
        unsupported_tools = sorted(set(manifest.declared_tools) - set(self.allowed_tools))
        if unsupported_tools:
            raise SkillVerificationError(
                f"Skill declares unsupported tools: {', '.join(unsupported_tools)}"
            )
        return manifest


def load_skill_manifest(
    skill_root: Path,
    *,
    review: SkillReview = "local-unreviewed",
) -> SkillManifest:
    """Load a complete Agent Skill through the public OpenHands Skill contract."""
    root = skill_root.resolve()
    try:
        inspected = inspect_skill(root)
    except CatalogBuildError as error:
        raise SkillVerificationError(str(error)) from error
    entrypoint = (
        (root / inspected.policy.entrypoint).resolve()
        if inspected.policy.entrypoint is not None
        else None
    )
    return SkillManifest(
        skill_id=inspected.policy.skill_id,
        name=inspected.name,
        description=inspected.description,
        root=root,
        policy=inspected.policy,
        declared_tools=inspected.allowed_tools,
        approval_summary=inspected.policy.approval_summary,
        entrypoint=entrypoint,
        review=review,
        tree_sha256=inspected.tree_sha256,
    )


def build_skill_approval_record(
    manifest: SkillManifest,
    *,
    session_id: str,
    actor_id: str,
    occurred_at: str,
    decision: str = "approved",
) -> ApprovalRecord:
    """Build the durable approval record for a Skill activation decision."""
    if decision not in {"approved", "denied"}:
        raise SkillVerificationError(f"Unsupported Skill approval decision: {decision}")
    checked_decision = cast(Literal["approved", "denied"], decision)
    return ApprovalRecord(
        approval_id=f"{session_id}-{manifest.skill_id.rsplit('.', maxsplit=1)[-1]}-approval",
        session_id=session_id,
        target_type="skill",
        target_id=manifest.skill_id,
        decision=checked_decision,
        actor_id=actor_id,
        occurred_at=occurred_at,
        reason=manifest.approval_summary,
    )
