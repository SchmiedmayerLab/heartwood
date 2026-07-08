# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Tests for local skill verification."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from heartwood.skills import (
    LocalSkillVerifier,
    SkillTestHarness,
    SkillVerificationError,
    build_skill_approval_record,
    load_skill_manifest,
)

_SKILLS_ROOT = Path("skills/verified")


def _write_skill(
    root: Path,
    *,
    metadata_requires_network: str = "false",
    frontmatter_requires_network: str = "false",
    trust_tier: str = "verified",
    signature: str | None = "sigstore:synthetic-fixture",
    tools: str = "read-local-csv",
    entrypoint: str = "scripts/run.py",
) -> Path:
    skill_root = root / "synthetic-skill"
    scripts = skill_root / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "run.py").write_text("print('offline placeholder')\n", encoding="utf-8")
    metadata = {
        "schema_version": "heartwood.skill-metadata.v1",
        "heartwood.dataset-types": "omop-cdm",
        "heartwood.platforms": "generic",
        "heartwood.phi-risk": "none",
        "heartwood.trust-tier": trust_tier,
        "heartwood.requires-network": metadata_requires_network,
        "heartwood.version": "0.1.0",
    }
    if signature is not None:
        metadata["heartwood.sig"] = signature
    (skill_root / "metadata.json").write_text(
        json.dumps(metadata, indent=2) + "\n",
        encoding="utf-8",
    )
    signature_frontmatter = f'  heartwood.sig: "{signature}"\n' if signature is not None else ""
    (skill_root / "SKILL.md").write_text(
        f"""---
id: "heartwood.synthetic.test-skill"
name: "Synthetic skill"
description: "A synthetic verifier fixture."
tools: "{tools}"
approval-summary: "Reads synthetic inputs."
entrypoint: "{entrypoint}"
metadata:
  heartwood.dataset-types: "omop-cdm"
  heartwood.platforms: "generic"
  heartwood.phi-risk: "none"
  heartwood.trust-tier: "{trust_tier}"
  heartwood.requires-network: "{frontmatter_requires_network}"
  heartwood.version: "0.1.0"
{signature_frontmatter.rstrip()}
---

# Synthetic Skill
""",
        encoding="utf-8",
    )
    return skill_root


def test_verified_prototype_skills_pass_local_gate() -> None:
    harness = SkillTestHarness(_SKILLS_ROOT)
    results = harness.verify_all()
    manifests = tuple(result.manifest for result in results)
    assert all(result.verified for result in results)
    assert {manifest.skill_id for manifest in manifests if manifest is not None} == {
        "heartwood.synthetic.aggregate-export",
        "heartwood.synthetic.baseline-model",
        "heartwood.synthetic.omop-cohort-summary",
    }


def test_verifier_builds_skill_approval_record() -> None:
    verifier = LocalSkillVerifier(_SKILLS_ROOT)
    manifest = verifier.load_manifest(_SKILLS_ROOT / "omop-cohort-summary")
    approval = build_skill_approval_record(
        manifest,
        session_id="session-synthetic-0d",
        actor_id="synthetic-reviewer",
        occurred_at="2026-01-01T00:00:00Z",
    )
    assert approval.target_type == "skill"
    assert approval.target_id == "heartwood.synthetic.omop-cohort-summary"
    assert approval.reason == manifest.approval_summary


def test_verifier_rejects_network_required_skill(tmp_path: Path) -> None:
    skill_root = _write_skill(
        tmp_path,
        metadata_requires_network="true",
        frontmatter_requires_network="true",
    )
    result = LocalSkillVerifier(tmp_path).verify(skill_root)
    assert result.verified is False
    assert result.reason == "skills requiring network access are not allowed in the local gate"


def test_verifier_rejects_community_skill_in_verified_gate(tmp_path: Path) -> None:
    skill_root = _write_skill(tmp_path, trust_tier="community", signature=None)
    result = LocalSkillVerifier(tmp_path).verify(skill_root)
    assert result.verified is False
    assert result.reason == "only verified skills can pass this local gate"


def test_verifier_allows_unsigned_community_skill_when_gate_allows_it(tmp_path: Path) -> None:
    skill_root = _write_skill(tmp_path, trust_tier="community", signature=None)
    result = LocalSkillVerifier(tmp_path, require_verified_tier=False).verify(skill_root)
    assert result.verified is True
    assert result.manifest is not None
    assert result.manifest.metadata.signature is None


def test_verifier_rejects_non_sigstore_signature(tmp_path: Path) -> None:
    skill_root = _write_skill(tmp_path, signature="synthetic-fixture")
    result = LocalSkillVerifier(tmp_path).verify(skill_root)
    assert result.verified is False
    assert result.reason == "verified skills must declare a sigstore provenance placeholder"


def test_verifier_rejects_unsupported_tools(tmp_path: Path) -> None:
    skill_root = _write_skill(tmp_path, tools="read-local-csv,network-fetch")
    result = LocalSkillVerifier(tmp_path).verify(skill_root)
    assert result.verified is False
    assert result.reason == "skill declares unsupported tools: network-fetch"


def test_verifier_rejects_missing_entrypoint(tmp_path: Path) -> None:
    skill_root = _write_skill(tmp_path, entrypoint="scripts/missing.py")
    result = LocalSkillVerifier(tmp_path).verify(skill_root)
    assert result.verified is False
    assert result.reason == "skill entrypoint does not exist: scripts/missing.py"


def test_verifier_rejects_mismatched_metadata(tmp_path: Path) -> None:
    skill_root = _write_skill(tmp_path, frontmatter_requires_network="true")
    with pytest.raises(SkillVerificationError, match=r"SKILL\.md metadata does not match"):
        load_skill_manifest(skill_root)


def test_verifier_rejects_missing_manifest_files(tmp_path: Path) -> None:
    skill_root = tmp_path / "missing"
    skill_root.mkdir()
    with pytest.raises(SkillVerificationError, match=r"missing SKILL\.md"):
        load_skill_manifest(skill_root)
    (skill_root / "SKILL.md").write_text("---\n---\n", encoding="utf-8")
    with pytest.raises(SkillVerificationError, match=r"missing metadata\.json"):
        load_skill_manifest(skill_root)


def test_verifier_rejects_malformed_frontmatter(tmp_path: Path) -> None:
    skill_root = tmp_path / "malformed"
    skill_root.mkdir()
    (skill_root / "metadata.json").write_text("{}", encoding="utf-8")
    (skill_root / "SKILL.md").write_text("name: missing fence\n", encoding="utf-8")
    with pytest.raises(SkillVerificationError, match="YAML frontmatter"):
        load_skill_manifest(skill_root)
    (skill_root / "SKILL.md").write_text(
        "# Heading\n---\nname: late fence\n---\n", encoding="utf-8"
    )
    with pytest.raises(SkillVerificationError, match="must start with YAML frontmatter"):
        load_skill_manifest(skill_root)
    (skill_root / "SKILL.md").write_text("---\nname without colon\n---\n", encoding="utf-8")
    with pytest.raises(SkillVerificationError, match=r"unsupported SKILL\.md"):
        load_skill_manifest(skill_root)


def test_build_skill_approval_record_rejects_unknown_decision() -> None:
    manifest = LocalSkillVerifier(_SKILLS_ROOT).load_manifest(_SKILLS_ROOT / "aggregate-export")
    with pytest.raises(SkillVerificationError, match="unsupported skill approval decision"):
        build_skill_approval_record(
            manifest,
            session_id="session-synthetic-0d",
            actor_id="synthetic-reviewer",
            occurred_at="2026-01-01T00:00:00Z",
            decision="maybe",
        )
