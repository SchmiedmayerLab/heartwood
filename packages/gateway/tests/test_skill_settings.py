# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import json
import shutil
import stat
from pathlib import Path

import pytest

from heartwood.gateway import SkillManager, SkillSettingsError


def test_manager_lists_bundled_skills() -> None:
    manager = SkillManager(
        bundled_dir=_repo_root() / "skills" / "verified",
        installed_dir=_repo_root() / ".heartwood-test-skills-missing",
        audit_path=_repo_root() / ".heartwood-test-audit-missing",
    )

    summaries = manager.summaries()

    assert {summary.name for summary in summaries} == {
        "aggregate-export",
        "baseline-model",
        "omop-cohort-summary",
    }
    assert all(summary.source == "bundled" for summary in summaries)
    assert all(summary.trust_tier == "verified" for summary in summaries)


def test_manager_inspects_approves_installs_and_removes_extension(tmp_path: Path) -> None:
    source = _community_skill(tmp_path)
    manager = _manager(tmp_path)

    candidate = manager.inspect(source)
    installed = manager.install(source, approved=True, actor_id="researcher")

    assert candidate.source == "candidate"
    assert candidate.trust_tier == "community"
    assert installed.source == "installed"
    assert (tmp_path / "installed" / "community-summary" / "SKILL.md").is_file()
    assert {summary.name for summary in manager.summaries()} >= {"community-summary"}
    audit_path = tmp_path / "skill-installations.jsonl"
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    assert audit["decision"] == "approved"
    assert audit["actor_id"] == "researcher"
    assert stat.S_IMODE(audit_path.stat().st_mode) == 0o600

    with pytest.raises(SkillSettingsError, match="already exists"):
        manager.install(source, approved=True)
    manager.remove("community-summary")
    assert not (tmp_path / "installed" / "community-summary").exists()


def test_manager_records_denial_without_installing(tmp_path: Path) -> None:
    source = _community_skill(tmp_path)
    audit_path = tmp_path / "skill-installations.jsonl"
    audit_path.write_text("", encoding="utf-8")
    audit_path.chmod(0o666)
    manager = _manager(tmp_path)

    with pytest.raises(SkillSettingsError, match="approval is required"):
        manager.install(source, approved=False)

    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    assert audit["decision"] == "denied"
    assert stat.S_IMODE(audit_path.stat().st_mode) == 0o600
    assert not (tmp_path / "installed").exists()


def test_manager_rejects_missing_unsafe_and_invalid_extensions(tmp_path: Path) -> None:
    manager = _manager(tmp_path)

    with pytest.raises(SkillSettingsError, match="does not exist"):
        manager.inspect(tmp_path / "missing")
    with pytest.raises(SkillSettingsError, match="does not exist"):
        manager.remove("missing")
    with pytest.raises(SkillSettingsError, match="only letters"):
        manager.remove("../escape")

    source = _community_skill(tmp_path)
    (source / "linked").symlink_to(source / "metadata.json")
    with pytest.raises(SkillSettingsError, match="symbolic links"):
        manager.install(source, approved=True)

    clean_source = _community_skill(tmp_path / "root-link")
    source_link = tmp_path / "linked-source"
    source_link.symlink_to(clean_source, target_is_directory=True)
    with pytest.raises(SkillSettingsError, match="symbolic links"):
        manager.inspect(source_link)


def _manager(tmp_path: Path) -> SkillManager:
    return SkillManager(
        bundled_dir=_repo_root() / "skills" / "verified",
        installed_dir=tmp_path / "installed",
        audit_path=tmp_path / "skill-installations.jsonl",
    )


def _community_skill(tmp_path: Path) -> Path:
    source = tmp_path / "source" / "community-summary"
    shutil.copytree(_repo_root() / "skills" / "verified" / "aggregate-export", source)
    skill_file = source / "SKILL.md"
    skill_file.write_text(
        skill_file.read_text(encoding="utf-8")
        .replace("heartwood.synthetic.aggregate-export", "example.community-summary")
        .replace('name: "aggregate-export"', 'name: "community-summary"')
        .replace('heartwood.trust-tier: "verified"', 'heartwood.trust-tier: "community"'),
        encoding="utf-8",
    )
    metadata_path = source / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["heartwood.trust-tier"] = "community"
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    return source


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]
