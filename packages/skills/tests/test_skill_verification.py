# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Tests for the OpenHands-native Heartwood Skill policy adapter."""

from __future__ import annotations

from pathlib import Path

import pytest

from heartwood.skills import (
    LocalSkillVerifier,
    SkillTestHarness,
    SkillVerificationError,
    build_skill_approval_record,
    load_skill_manifest,
)

_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
_SKILLS_ROOT = _REPOSITORY_ROOT / "vendor" / "heartwood-skills" / "skills" / "verified"


def _write_skill(
    root: Path,
    *,
    name: str = "synthetic-skill",
    tools: str = "terminal",
    requires_network: str = "false",
    entrypoint: str = "scripts/run.py",
) -> Path:
    skill_root = root / name
    scripts = skill_root / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "run.py").write_text("print('offline placeholder')\n", encoding="utf-8")
    (skill_root / "SKILL.md").write_text(
        f"""---
name: {name}
description: A synthetic Agent Skill used only by Heartwood tests.
license: MIT
allowed-tools: {tools}
metadata:
  heartwood.id: "heartwood.synthetic.{name}"
  heartwood.version: "1.0.0"
  heartwood.dataset-types: "synthetic-tabular"
  heartwood.platforms: "generic"
  heartwood.phi-risk: "none"
  heartwood.requires-network: "{requires_network}"
  heartwood.controlled-data: "not-approved"
  heartwood.approval-summary: "Reads and writes synthetic test files."
  heartwood.entrypoint: "{entrypoint}"
---

# Synthetic Skill
""",
        encoding="utf-8",
    )
    return skill_root


def test_vendored_skills_load_through_openhands_as_repository_reviewed() -> None:
    verifier = LocalSkillVerifier(
        _SKILLS_ROOT,
        review="repository-reviewed",
        require_repository_review=True,
    )
    results = tuple(
        verifier.verify(path) for path in sorted(_SKILLS_ROOT.iterdir()) if path.is_dir()
    )
    manifests = tuple(result.manifest for result in results if result.manifest is not None)

    assert all(result.verified for result in results)
    assert {manifest.skill_id for manifest in manifests} == {
        "heartwood.research.aggregate-export",
        "heartwood.research.baseline-model",
        "heartwood.research.omop-cohort-summary",
    }
    assert all(manifest.review == "repository-reviewed" for manifest in manifests)
    assert all(len(manifest.tree_sha256) == 64 for manifest in manifests)


def test_local_skill_review_and_policy_are_separate(tmp_path: Path) -> None:
    skill_root = _write_skill(tmp_path)
    local = LocalSkillVerifier(tmp_path).load_manifest(skill_root)
    assert local.review == "local-unreviewed"
    assert local.requires_network is False
    assert local.version == "1.0.0"

    reviewed = LocalSkillVerifier(
        tmp_path,
        review="repository-reviewed",
        require_repository_review=True,
    ).load_manifest(skill_root)
    assert reviewed.review == "repository-reviewed"

    result = LocalSkillVerifier(tmp_path, require_repository_review=True).verify(skill_root)
    assert result.verified is False
    assert result.reason == "Skill has not passed repository review"


def test_verifier_rejects_network_and_unsupported_tools(tmp_path: Path) -> None:
    network = _write_skill(tmp_path / "network", requires_network="true")
    result = LocalSkillVerifier(network.parent).verify(network)
    assert result.verified is False
    assert result.reason == "Skill requires network access, which is disabled"

    unsupported = _write_skill(tmp_path / "unsupported", tools="terminal network-fetch")
    result = LocalSkillVerifier(unsupported.parent).verify(unsupported)
    assert result.verified is False
    assert result.reason == "Skill declares unsupported tools: network-fetch"


def test_verifier_confines_paths_and_wraps_openhands_errors(tmp_path: Path) -> None:
    skill_root = _write_skill(tmp_path / "source")
    with pytest.raises(SkillVerificationError, match="escapes verification root"):
        LocalSkillVerifier(tmp_path / "other").load_manifest(skill_root)

    (skill_root / "SKILL.md").write_text("---\nname: [\n---\n", encoding="utf-8")
    with pytest.raises(SkillVerificationError, match="OpenHands rejected"):
        load_skill_manifest(skill_root)


def test_harness_requires_an_entrypoint_and_runs_vendored_script(tmp_path: Path) -> None:
    manifest = LocalSkillVerifier(_SKILLS_ROOT).load_manifest(_SKILLS_ROOT / "aggregate-export")
    summary = tmp_path / "summary.json"
    output = tmp_path / "output.json"
    summary.write_text(
        '{"summary":{"participant_count":20,"condition_occurrence_count":20,'
        '"target_condition_occurrence_count":20}}\n',
        encoding="utf-8",
    )
    completed = SkillTestHarness(_SKILLS_ROOT).run(
        manifest,
        "--summary",
        str(summary),
        "--output",
        str(output),
    )
    assert completed.returncode == 0
    assert output.is_file()

    no_entrypoint = _write_skill(tmp_path / "no-entrypoint")
    skill_file = no_entrypoint / "SKILL.md"
    skill_file.write_text(
        skill_file.read_text(encoding="utf-8").replace(
            '  heartwood.entrypoint: "scripts/run.py"\n', ""
        ),
        encoding="utf-8",
    )
    manifest = LocalSkillVerifier(no_entrypoint.parent).load_manifest(no_entrypoint)
    with pytest.raises(SkillVerificationError, match="no executable entrypoint"):
        SkillTestHarness(no_entrypoint.parent).run(manifest)


def test_skill_approval_record_uses_stable_policy_identity() -> None:
    manifest = LocalSkillVerifier(_SKILLS_ROOT).load_manifest(_SKILLS_ROOT / "omop-cohort-summary")
    approval = build_skill_approval_record(
        manifest,
        session_id="session-synthetic",
        actor_id="synthetic-reviewer",
        occurred_at="2026-01-01T00:00:00Z",
    )
    assert approval.target_id == "heartwood.research.omop-cohort-summary"
    assert approval.reason == manifest.approval_summary

    with pytest.raises(SkillVerificationError, match="Unsupported Skill approval decision"):
        build_skill_approval_record(
            manifest,
            session_id="session-synthetic",
            actor_id="synthetic-reviewer",
            occurred_at="2026-01-01T00:00:00Z",
            decision="maybe",
        )
