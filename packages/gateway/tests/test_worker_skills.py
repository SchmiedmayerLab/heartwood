# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from heartwood_skill_catalog import CatalogBuildError

from heartwood.gateway.worker_skills import WorkerSkillProjection, stage_skill_projection
from heartwood.skills import LocalSkillVerifier, SkillManifest


@pytest.fixture
def selected(tmp_path: Path) -> tuple[SkillManifest, Path, Path]:
    source = Path(__file__).resolve().parents[3] / "vendor/heartwood-skills/skills/aggregate-export"
    manifest = LocalSkillVerifier(source.parent).load_manifest(source)
    parent = tmp_path / "staging"
    parent.mkdir(mode=0o700)
    project = tmp_path / "project"
    project.mkdir()
    return manifest, parent, project


def test_projection_identity_binds_selection_content_and_mount(tmp_path: Path) -> None:
    first = WorkerSkillProjection(tmp_path, (("first", "a" * 64), ("second", "b" * 64)))
    relocated = replace(
        first, root=tmp_path / "relocated", bindings=tuple(reversed(first.bindings))
    )
    assert first.sha256 == relocated.sha256
    assert first.sha256 != replace(first, bindings=(("first", "b" * 64),)).sha256
    assert first.sha256 != replace(first, bindings=()).sha256


def test_projection_rejects_changed_selection_and_cleans_staging(
    selected: tuple[SkillManifest, Path, Path],
) -> None:
    manifest, parent, project = selected
    with (
        pytest.raises(CatalogBuildError, match="changed after worker selection"),
        stage_skill_projection(
            (replace(manifest, tree_sha256="0" * 64),), project_root=project, staging_parent=parent
        ),
    ):
        pytest.fail("Changed selection copied")
    assert not list(parent.iterdir())


def test_projection_refuses_staged_content_mutation(
    selected: tuple[SkillManifest, Path, Path],
) -> None:
    manifest, parent, project = selected
    with stage_skill_projection(
        (manifest,), project_root=project, staging_parent=parent
    ) as projection:
        path = projection.root / manifest.name / "SKILL.md"
        path.write_text(path.read_text() + "\nChanged instructions\n")
        with pytest.raises(CatalogBuildError, match="changed before loading"):
            projection.agent_skills()
    assert not list(parent.iterdir())


def test_projection_cleans_up_after_transfer_failure(
    selected: tuple[SkillManifest, Path, Path],
) -> None:
    manifest, parent, project = selected
    with (
        pytest.raises(RuntimeError, match="synthetic transfer"),
        stage_skill_projection(
            (manifest,), project_root=project, staging_parent=parent
        ) as projection,
    ):
        raise RuntimeError("synthetic transfer")
    assert not projection.root.exists()
    assert not list(parent.iterdir())


@pytest.mark.parametrize("location", ["project", "shared", "file", "alias"])
def test_projection_requires_private_staging_outside_project(
    selected: tuple[SkillManifest, Path, Path], location: str
) -> None:
    manifest, parent, project = selected
    if location == "project":
        parent = project
    elif location == "shared":
        parent.chmod(0o755)
    elif location == "alias":
        parent = parent / "alias"
        parent.symlink_to(project, target_is_directory=True)
    else:
        parent = parent / "file"
        parent.touch()
    with (
        pytest.raises(ValueError, match="Worker Skill staging"),
        stage_skill_projection((manifest,), project_root=project, staging_parent=parent),
    ):
        pytest.fail("Unsafe staging admitted")


def test_duplicate_names_fail_before_copy(selected: tuple[SkillManifest, Path, Path]) -> None:
    manifest, parent, project = selected
    with (
        pytest.raises(ValueError, match="unique"),
        stage_skill_projection((manifest, manifest), project_root=project, staging_parent=parent),
    ):
        pytest.fail("Duplicate names accepted")
    assert not list(parent.iterdir())


def test_empty_selection_creates_no_skill_content(
    selected: tuple[SkillManifest, Path, Path],
) -> None:
    _, parent, project = selected
    with stage_skill_projection((), project_root=project, staging_parent=parent) as projection:
        assert projection.agent_skills() == []
        assert not list(projection.root.iterdir())


def test_projection_bounds_package_count_before_copy(
    selected: tuple[SkillManifest, Path, Path],
) -> None:
    manifest, parent, project = selected
    manifests = tuple(replace(manifest, name=f"skill-{index}") for index in range(257))
    with (
        pytest.raises(ValueError, match="at most 256"),
        stage_skill_projection(manifests, project_root=project, staging_parent=parent),
    ):
        pytest.fail("Unbounded package selection accepted")
    assert not list(parent.iterdir())


def test_projection_bounds_total_content_across_packages(
    selected: tuple[SkillManifest, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from heartwood_skill_catalog import SkillFile, inspect_skill

    import heartwood.gateway.worker_skills as worker_skills

    manifest, parent, project = selected
    inspected = inspect_skill(manifest.root)
    files = tuple(
        SkillFile(
            path=f"file-{index}",
            size=8 * 1024 * 1024,
            sha256="a" * 64,
            role="asset",
            executable=False,
        )
        for index in range(5)
    )
    first = replace(manifest, name="first")
    second = replace(manifest, name="second", root=project)
    copied: list[str] = []

    def observe(source: Path) -> object:
        return inspected.model_copy(
            update={"name": "first" if source == manifest.root else "second", "files": files}
        )

    def copy(source: Path, destination: Path, *, expected_tree_sha256: str) -> Path:
        assert source == manifest.root
        assert expected_tree_sha256 == manifest.tree_sha256
        copied.append(destination.name)
        destination.mkdir()
        return destination

    monkeypatch.setattr(worker_skills, "inspect_skill", observe)
    monkeypatch.setattr(worker_skills, "copy_skill_tree", copy)
    with (
        pytest.raises(ValueError, match="transfer byte limit"),
        stage_skill_projection((first, second), project_root=project, staging_parent=parent),
    ):
        pytest.fail("Aggregate size exceeded the transfer budget")
    assert copied == ["first"]
    assert not list(parent.iterdir())
