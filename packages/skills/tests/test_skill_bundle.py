# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Tests for bundled skill catalog validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from heartwood.skills import (
    GitSkillSource,
    LocalSkillSource,
    SkillBundleError,
    load_skill_bundle,
    resolve_skill_bundle,
    skill_ids,
)


def test_checked_in_skill_bundle_resolves_verified_local_skills() -> None:
    resolutions = resolve_skill_bundle(Path("skills/bundle.toml"))
    assert skill_ids(resolutions) == (
        "heartwood.synthetic.omop-cohort-summary",
        "heartwood.synthetic.aggregate-export",
        "heartwood.synthetic.baseline-model",
    )
    assert all(isinstance(resolution.entry.source, LocalSkillSource) for resolution in resolutions)
    assert all(resolution.manifest is not None for resolution in resolutions)
    assert all(resolution.source_path is not None for resolution in resolutions)


def test_skill_bundle_rejects_local_source_path_escape(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle.toml"
    bundle.write_text(
        """schema_version = "heartwood.skill-bundle.v1"

[[skills]]
skill_id = "heartwood.synthetic.wrong-id"

[skills.source]
type = "local"
path = "../../skills/verified/aggregate-export"
""",
        encoding="utf-8",
    )
    with pytest.raises(SkillBundleError, match="clean relative path"):
        resolve_skill_bundle(bundle)


def test_external_git_skill_requires_pinning_metadata_and_provenance(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle.toml"
    bundle.write_text(
        """schema_version = "heartwood.skill-bundle.v1"

[[skills]]
skill_id = "heartwood.external.omop-summary"

[skills.source]
type = "git"
repository = "https://github.com/example/heartwood-external-skills"
ref = "v1.2.3"
commit = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
path = "skills/omop-summary"
content_sha256 = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

[skills.metadata]
schema_version = "heartwood.skill-metadata.v1"
"heartwood.dataset-types" = "omop-cdm"
"heartwood.platforms" = "generic,terra"
"heartwood.phi-risk" = "reads-phi"
"heartwood.trust-tier" = "verified"
"heartwood.requires-network" = "false"
"heartwood.version" = "1.2.3"
"heartwood.sig" = "sigstore:external-omop-summary"

[skills.provenance]
signature = "sigstore:external-omop-summary"
reviewed_by = "heartwood-review"
license = "MIT"
""",
        encoding="utf-8",
    )

    loaded = load_skill_bundle(bundle)
    assert isinstance(loaded.skills[0].source, GitSkillSource)
    resolutions = resolve_skill_bundle(bundle)
    assert resolutions[0].manifest is None
    assert resolutions[0].metadata.version == "1.2.3"
    assert resolutions[0].metadata.platforms == ("generic", "terra")


def test_external_git_skill_rejects_unpinned_or_unreviewed_sources(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle.toml"
    bundle.write_text(
        """schema_version = "heartwood.skill-bundle.v1"

[[skills]]
skill_id = "heartwood.external.omop-summary"

[skills.source]
type = "git"
repository = "https://github.com/example/heartwood-external-skills"
ref = "main"
commit = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
path = "skills/omop-summary"
content_sha256 = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
""",
        encoding="utf-8",
    )
    with pytest.raises(SkillBundleError, match="semver tag"):
        load_skill_bundle(bundle)

    bundle.write_text(
        """schema_version = "heartwood.skill-bundle.v1"

[[skills]]
skill_id = "heartwood.external.omop-summary"

[skills.source]
type = "git"
repository = "https://github.com/example/heartwood-external-skills"
ref = "v1.2.3"
commit = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
path = "skills/omop-summary"
content_sha256 = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
""",
        encoding="utf-8",
    )
    with pytest.raises(SkillBundleError, match="heartwood metadata"):
        load_skill_bundle(bundle)
