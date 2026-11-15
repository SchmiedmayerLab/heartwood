# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Protected relocation preserves history and never admits two project writers."""

from __future__ import annotations

import errno
import fcntl
import json
import os
import shutil
import threading
import tomllib
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

import pytest
import tomli_w

from heartwood.core_adapter import FileSessionStore, SessionService
from heartwood.gateway import ProjectContext, ProjectStateError, load_project_deployment
from heartwood.gateway import _project_migration as migration
from heartwood.gateway._experiment_store import ExperimentStore
from heartwood.gateway._project_activity import ProjectActivity, ProjectOwnershipError
from heartwood.gateway._project_config import LocalModelSelection, ProjectConfig, _config_mapping
from heartwood.gateway._session_catalog import SessionCatalog
from heartwood.gateway.experiments import ExperimentRecorder
from heartwood.persistence import write_private_json_atomic
from heartwood.schemas import PolicyProfile
from heartwood.schemas.experiments import ExperimentEvent
from heartwood.session import CommandKind, SessionCommand
from heartwood.skills import LocalSkillVerifier, SkillArtifactStore


def _fixture(tmp_path: Path) -> tuple[ProjectContext, Path, Path, Path]:
    workspace = tmp_path / "research"
    workspace.mkdir()
    source = ProjectContext(workspace)
    source.initialize()
    protected = tmp_path / "protected"
    protected.mkdir(mode=0o700)
    identity = str(uuid4())
    target = protected / identity
    backup = protected / "original"
    policy = PolicyProfile(policy_id="synthetic", platform_id="generic")
    manifest = tmp_path / "deployment.toml"
    manifest.write_text(
        tomli_w.dumps(
            {
                "schema_version": "heartwood.project-deployment.v1",
                "project_id": identity,
                "workspace": str(workspace),
                "state_root": str(target),
                "policy": {"policy_id": "synthetic", "platform_id": "generic"},
            }
        )
    )
    manifest.chmod(0o600)
    (source.models_dir / "synthetic.gguf").write_bytes(b"synthetic model bytes")
    (source.models_dir / "artifact.lock").write_bytes(b"")
    config = ProjectConfig(
        platform_id="generic",
        policy=policy,
        model_source="heartwood",
        local_model=LocalModelSelection(
            artifact_id="synthetic",
            path=".heartwood/models/synthetic.gguf",
            model_id="synthetic-model",
        ),
    )
    source.config_path.write_text(tomli_w.dumps(_config_mapping(config)))
    service = SessionService.synthetic_default(source.sessions_dir)
    try:
        service.handle(
            SessionCommand(
                command_id="synthetic-pause",
                session_id="session-synthetic-001",
                kind=CommandKind.PAUSE,
                actor_id="human",
                created_at="2026-01-01T00:00:00Z",
            )
        )
    finally:
        service.close()
    SessionCatalog(source.sessions_dir).ensure(service.store.session_id, title="Retained history")
    (source.root / "analysis.py").write_text("pass\n")
    recorder = ExperimentRecorder(source)
    definition = recorder.describe(
        actor_ref="synthetic", entry_point="analysis.py", inputs=(), outputs=(), parameters={}
    )
    with recorder.record(definition):
        pass
    return source, manifest, target, backup


def test_migration_preserves_canonical_records_models_and_original_backup(tmp_path: Path) -> None:
    source, manifest, target, backup = _fixture(tmp_path)
    original = migration._snapshot(source.state_root)
    journal = migration.migrate_project_state(
        deployment=manifest, workspace=source.root, backup=backup
    )
    assert journal.is_file()
    assert migration._snapshot(backup) == original
    assert not source.state_root.exists()
    with pytest.raises(ProjectStateError, match="retired"):
        source.initialize()
    managed = load_project_deployment(manifest, project_root=source.root).project
    assert managed.state_exists()
    copied = FileSessionStore(managed.sessions_dir, "session-synthetic-001")
    retained = FileSessionStore(backup / "sessions", copied.session_id)
    assert copied.replay_events() == retained.replay_events()
    assert copied.verified_head() == retained.verified_head()
    assert copied.audit_path.read_bytes() == retained.audit_path.read_bytes()
    assert (target / "experiments.jsonl").read_bytes() == (
        backup / "experiments.jsonl"
    ).read_bytes()
    assert (target / "models/synthetic.gguf").read_bytes() == b"synthetic model bytes"
    # Usage summaries from before and after migration share their project identifier.
    assert managed.usage_summary_id() == (backup / "usage-summary-id").read_text().strip()
    assert 'path = "models/synthetic.gguf"' in (target / "config.toml").read_text()
    assert 'path = ".heartwood/models/synthetic.gguf"' in (backup / "config.toml").read_text()
    migration.recover_project_migration(deployment=manifest, workspace=source.root)


@pytest.mark.parametrize("boundary", ["retirement", "activation"])
def test_interrupted_publication_is_fenced_and_can_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    source, manifest, target, backup = _fixture(tmp_path)
    original = migration._snapshot(source.state_root)
    rename = Path.rename

    def interrupt(path: Path, destination: Path) -> Path:
        if destination == (backup if boundary == "retirement" else target):
            raise OSError("synthetic interruption")
        return rename(path, destination)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "rename", interrupt)
        with pytest.raises(OSError, match="synthetic interruption"):
            migration.migrate_project_state(
                deployment=manifest, workspace=source.root, backup=backup
            )
    with pytest.raises(ProjectStateError):
        source.initialize()
    with pytest.raises(ProjectStateError, match="operator recovery"):
        load_project_deployment(manifest, project_root=source.root)
    migration.recover_project_migration(deployment=manifest, workspace=source.root)
    assert load_project_deployment(manifest, project_root=source.root).project.state_exists()
    assert migration._snapshot(backup) == original


def test_disk_full_during_staging_leaves_local_state_usable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, manifest, target, backup = _fixture(tmp_path)
    original = migration._snapshot(source.state_root)

    def disk_full(_source: Path, _target: Path) -> None:
        raise OSError(errno.ENOSPC, "synthetic disk full")

    monkeypatch.setattr(migration, "_copy_file", disk_full)
    with pytest.raises(OSError, match="disk full"):
        migration.migrate_project_state(deployment=manifest, workspace=source.root, backup=backup)
    assert source.state_exists()
    assert migration._snapshot(source.state_root) == original
    assert not target.exists()
    assert not backup.exists()


def test_active_gateway_excludes_migration(tmp_path: Path) -> None:
    source, manifest, target, backup = _fixture(tmp_path)
    activity = ProjectActivity(source)
    activity.acquire_existing()
    try:
        with pytest.raises(ProjectOwnershipError, match="activity to stop"):
            migration.migrate_project_state(
                deployment=manifest, workspace=source.root, backup=backup
            )
    finally:
        activity.close()
    assert source.state_exists()
    assert not target.exists()


def test_running_recorded_experiment_excludes_migration(tmp_path: Path) -> None:
    source, manifest, _target, backup = _fixture(tmp_path)
    recorder = ExperimentRecorder(source)
    definition = recorder.describe(
        actor_ref="synthetic", entry_point="analysis.py", inputs=(), outputs=(), parameters={}
    )
    with recorder.record(definition), pytest.raises(ProjectOwnershipError):
        migration.migrate_project_state(deployment=manifest, workspace=source.root, backup=backup)
    assert source.state_exists()


@pytest.mark.parametrize("record", ["experiments.jsonl", "skills/index.json"])
def test_corrupt_canonical_records_prevent_retirement(tmp_path: Path, record: str) -> None:
    source, manifest, target, backup = _fixture(tmp_path)
    (source.state_root / record).write_text("invalid\n")
    with pytest.raises(ValueError, match=r"integrity recovery|index is invalid"):
        migration.migrate_project_state(deployment=manifest, workspace=source.root, backup=backup)
    assert source.state_exists()
    assert not backup.exists()
    assert not target.exists()


def test_copied_workspace_does_not_gain_the_operator_binding(tmp_path: Path) -> None:
    source, manifest, _target, backup = _fixture(tmp_path)
    migration.migrate_project_state(deployment=manifest, workspace=source.root, backup=backup)
    copied = tmp_path / "copied"
    shutil.copytree(source.root, copied)
    with pytest.raises(ProjectStateError, match="retired"):
        ProjectContext(copied).initialize()
    with pytest.raises(ProjectStateError, match="does not authorize"):
        load_project_deployment(manifest, project_root=copied)


def test_explicit_rollback_restores_original_and_retains_both_copies(tmp_path: Path) -> None:
    source, manifest, target, backup = _fixture(tmp_path)
    original = migration._snapshot(source.state_root)
    migration.migrate_project_state(deployment=manifest, workspace=source.root, backup=backup)
    migration.recover_project_migration(deployment=manifest, workspace=source.root, rollback=True)
    assert source.state_exists()
    assert migration._snapshot(source.state_root) == original
    assert migration._snapshot(backup) == original
    assert not target.exists()
    assert target.with_name(f".{target.name}.rolled-back").is_dir()
    migration.recover_project_migration(deployment=manifest, workspace=source.root, rollback=True)


def test_rollback_refuses_a_target_with_new_records(tmp_path: Path) -> None:
    source, manifest, target, backup = _fixture(tmp_path)
    migration.migrate_project_state(deployment=manifest, workspace=source.root, backup=backup)
    (target / "new-record").write_text("new state")
    with pytest.raises(ProjectStateError, match="target changed"):
        migration.recover_project_migration(
            deployment=manifest, workspace=source.root, rollback=True
        )
    assert target.is_dir()
    assert backup.is_dir()
    with pytest.raises(ProjectStateError, match="retired"):
        source.initialize()


def test_migration_reverifies_installed_skill_metadata_and_content(tmp_path: Path) -> None:
    source, manifest, target, backup = _fixture(tmp_path)
    skill = Path(__file__).resolve().parents[3] / "vendor/heartwood-skills/skills/aggregate-export"
    verified = LocalSkillVerifier(skill.parent).load_manifest(skill)
    store = SkillArtifactStore(source.skills_dir)
    installed = store.install_local(skill, expected_tree_sha256=verified.tree_sha256)
    migration.migrate_project_state(deployment=manifest, workspace=source.root, backup=backup)
    migrated = SkillArtifactStore(target / "skills")
    assert migrated.records() == (installed,)
    assert migrated.manifest(installed).tree_sha256 == verified.tree_sha256
    assert SkillArtifactStore(backup / "skills").records() == (installed,)


def test_changed_staged_model_is_rejected_before_retirement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, manifest, target, backup = _fixture(tmp_path)
    copy = migration._copy_file

    def corrupt(original: Path, destination: Path) -> None:
        copy(original, destination)
        if destination.suffix == ".gguf":
            destination.write_bytes(b"changed")

    monkeypatch.setattr(migration, "_copy_file", corrupt)
    with pytest.raises(ProjectStateError, match="differ from their source"):
        migration.migrate_project_state(deployment=manifest, workspace=source.root, backup=backup)
    assert source.state_exists()
    assert not target.exists()
    assert not backup.exists()


def test_interrupted_rollback_reuses_verified_restore_without_overwriting_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, manifest, _target, backup = _fixture(tmp_path)
    migration.migrate_project_state(deployment=manifest, workspace=source.root, backup=backup)
    rename = Path.rename

    def interrupt(path: Path, destination: Path) -> Path:
        if destination == source.state_root:
            raise OSError("synthetic restore interruption")
        return rename(path, destination)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "rename", interrupt)
        with pytest.raises(OSError, match="restore interruption"):
            migration.recover_project_migration(
                deployment=manifest, workspace=source.root, rollback=True
            )
    with pytest.raises(ProjectStateError, match="retired"):
        source.initialize()
    migration.recover_project_migration(deployment=manifest, workspace=source.root, rollback=True)
    assert source.state_exists()
    assert migration._snapshot(source.state_root) == migration._snapshot(backup)


def test_project_migration_cli_uses_only_the_explicit_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from heartwood.cli import main

    source, manifest, _target, backup = _fixture(tmp_path)
    monkeypatch.chdir(source.root)
    assert main(["project", "migrate", "--deployment", str(manifest), "--backup", str(backup)]) == 0
    assert "Migration evidence:" in capsys.readouterr().out
    assert main(["project", "recover-migration", "--deployment", str(manifest)]) == 0
    assert main(["project", "recover-migration", "--deployment", str(manifest), "--rollback"]) == 0


def test_retained_stage_recovers_when_external_journal_publication_was_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, manifest, target, backup = _fixture(tmp_path)
    write = write_private_json_atomic
    journal = target.with_name(f".{target.name}.migration.json")

    def interrupt(path: Path, value: Mapping[str, object], **options: bool) -> None:
        if path == journal:
            raise OSError("synthetic journal interruption")
        write(path, value, **options)

    with monkeypatch.context() as patch:
        patch.setattr(migration, "write_private_json_atomic", interrupt)
        with pytest.raises(OSError, match="journal interruption"):
            migration.migrate_project_state(
                deployment=manifest, workspace=source.root, backup=backup
            )
    assert source.state_exists()
    assert not journal.exists()
    migration.recover_project_migration(deployment=manifest, workspace=source.root)
    assert journal.is_file()
    assert load_project_deployment(manifest, project_root=source.root).project.state_exists()


def test_project_migration_cli_reports_active_gateway_without_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from heartwood.cli import main

    source, manifest, _target, backup = _fixture(tmp_path)
    activity = ProjectActivity(source)
    activity.acquire_existing()
    monkeypatch.chdir(source.root)
    try:
        assert (
            main(["project", "migrate", "--deployment", str(manifest), "--backup", str(backup)])
            == 75
        )
    finally:
        activity.close()
    assert "Project unavailable:" in capsys.readouterr().err


def test_retry_preserves_partial_restore_and_copies_to_a_fresh_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, manifest, _target, backup = _fixture(tmp_path)
    migration.migrate_project_state(deployment=manifest, workspace=source.root, backup=backup)
    copy = migration._copy_file

    def disk_full(original: Path, destination: Path) -> None:
        if destination.name == "synthetic.gguf":
            destination.write_bytes(b"partial")
            raise OSError(errno.ENOSPC, "synthetic restore disk full")
        copy(original, destination)

    with monkeypatch.context() as patch:
        patch.setattr(migration, "_copy_file", disk_full)
        with pytest.raises(OSError, match="restore disk full"):
            migration.recover_project_migration(
                deployment=manifest, workspace=source.root, rollback=True
            )
    partial = source.root / ".heartwood-restore/models/synthetic.gguf"
    assert partial.read_bytes() == b"partial"
    migration.recover_project_migration(deployment=manifest, workspace=source.root, rollback=True)
    assert source.state_exists()
    assert partial.read_bytes() == b"partial"
    assert migration._snapshot(source.state_root) == migration._snapshot(backup)


@pytest.mark.parametrize("kind", ["file", "symlink", "dangling", "linked-content"])
def test_rollback_preserves_an_unusable_restore_path_and_copies_to_a_fresh_directory(
    tmp_path: Path, kind: str
) -> None:
    source, manifest, _target, backup = _fixture(tmp_path)
    migration.migrate_project_state(deployment=manifest, workspace=source.root, backup=backup)
    occupied = source.root / ".heartwood-restore"
    if kind == "file":
        occupied.write_text("research content")
    elif kind == "symlink":
        occupied.symlink_to(backup, target_is_directory=True)
    elif kind == "dangling":
        occupied.symlink_to(source.root / "absent")
    else:
        occupied.mkdir()
        (occupied / "link").symlink_to(source.root / "analysis.py")
    migration.recover_project_migration(deployment=manifest, workspace=source.root, rollback=True)
    assert source.state_exists()
    assert not source.state_root.is_symlink()
    assert migration._snapshot(source.state_root) == migration._snapshot(backup)
    assert occupied.is_symlink() == (kind in {"symlink", "dangling"})
    if kind == "file":
        assert occupied.read_text() == "research content"


@pytest.mark.parametrize("damage", ["changed", "removed"])
def test_rollback_detects_model_artifacts_even_when_named_like_locks(
    tmp_path: Path, damage: str
) -> None:
    source, manifest, target, backup = _fixture(tmp_path)
    migration.migrate_project_state(deployment=manifest, workspace=source.root, backup=backup)
    artifact = target / "models/artifact.lock"
    if damage == "changed":
        artifact.write_bytes(b"new model metadata")
    else:
        artifact.unlink()
    with pytest.raises(ProjectStateError, match="target changed"):
        migration.recover_project_migration(
            deployment=manifest, workspace=source.root, rollback=True
        )


@pytest.mark.parametrize("destination", ["workspace", "target", "existing", "relative"])
def test_unsafe_backup_destinations_are_rejected_without_changes(
    tmp_path: Path, destination: str
) -> None:
    source, manifest, target, backup = _fixture(tmp_path)
    if destination == "workspace":
        backup = source.root / "backup"
    elif destination == "target":
        backup = target
    elif destination == "existing":
        backup.mkdir()
        (backup / "keep").write_text("preserve")
    else:
        backup = Path("relative-backup")
    before = migration._snapshot(source.state_root)
    with pytest.raises(ProjectStateError, match="backup"):
        migration.migrate_project_state(deployment=manifest, workspace=source.root, backup=backup)
    assert migration._snapshot(source.state_root) == before
    assert not target.exists()


@pytest.mark.parametrize("existing", ["journal", "target", "stage"])
def test_existing_migration_destinations_are_never_overwritten(
    tmp_path: Path, existing: str
) -> None:
    source, manifest, target, backup = _fixture(tmp_path)
    if existing == "journal":
        destination = target.with_name(f".{target.name}.migration.json")
        destination.write_text("preserve")
    else:
        destination = (
            target if existing == "target" else target.with_name(f".{target.name}.migration-stage")
        )
        destination.mkdir()
        (destination / "keep").write_text("preserve")
    with pytest.raises(ProjectStateError, match="already exists"):
        migration.migrate_project_state(deployment=manifest, workspace=source.root, backup=backup)
    assert (
        destination if destination.is_file() else destination / "keep"
    ).read_text() == "preserve"
    assert source.state_exists()
    assert not backup.exists()


@pytest.mark.parametrize("damage", ["identity", "stage", "backup", "missing-backup"])
def test_recovery_refuses_conflicting_evidence_and_changed_originals(
    tmp_path: Path, damage: str
) -> None:
    source, manifest, target, backup = _fixture(tmp_path)
    journal = migration.migrate_project_state(
        deployment=manifest, workspace=source.root, backup=backup
    )
    if damage in {"identity", "stage"}:
        receipt = json.loads(journal.read_text())
        receipt["project_id" if damage == "identity" else "stage"] = str(uuid4())
        journal.write_text(json.dumps(receipt))
    elif damage == "backup":
        (backup / "models/synthetic.gguf").write_bytes(b"changed backup")
    else:
        backup.rename(backup.with_name("retained-elsewhere"))
    with pytest.raises(
        ProjectStateError, match=r"binding|trusted target|records changed|unavailable"
    ):
        migration.recover_project_migration(
            deployment=manifest, workspace=source.root, rollback=True
        )
    assert target.is_dir()
    assert not source.state_root.exists()


@pytest.mark.parametrize(
    "record", ["symbolic-link", "hard-link", "special-file", "pending", "migration"]
)
def test_unsettled_or_redirected_source_records_are_not_promoted(
    tmp_path: Path, record: str
) -> None:
    source, manifest, target, backup = _fixture(tmp_path)
    if record == "symbolic-link":
        (source.models_dir / "redirect").symlink_to(source.root / "analysis.py")
    elif record == "hard-link":
        (source.models_dir / "redirect").hardlink_to(source.root / "analysis.py")
    elif record == "special-file":
        os.mkfifo(source.models_dir / "pipe")
    else:
        name = ".experiments.jsonl.pending" if record == "pending" else "migration.json"
        (source.state_root / name).write_text("{}")
    with pytest.raises(ProjectStateError, match=r"links|special files|interrupted|conflicting"):
        migration.migrate_project_state(deployment=manifest, workspace=source.root, backup=backup)
    assert source.state_exists()
    assert not target.exists()
    assert not backup.exists()


def test_out_of_protocol_source_write_during_staging_prevents_retirement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, manifest, target, backup = _fixture(tmp_path)
    copy = migration._copy_file

    def change_after_copy(original: Path, destination: Path) -> None:
        copy(original, destination)
        if original.name == "synthetic.gguf":
            original.write_bytes(b"concurrent external change")

    monkeypatch.setattr(migration, "_copy_file", change_after_copy)
    with pytest.raises(ProjectStateError, match="source state changed"):
        migration.migrate_project_state(deployment=manifest, workspace=source.root, backup=backup)
    assert source.state_exists()
    assert not target.exists()
    assert not backup.exists()


@pytest.mark.parametrize("configuration", ["absent", "no-local-model"])
def test_migration_preserves_projects_without_optional_configuration(
    tmp_path: Path, configuration: str
) -> None:
    source, manifest, target, backup = _fixture(tmp_path)
    (source.state_root / "experiments.jsonl").unlink()
    if configuration == "absent":
        source.config_path.unlink()
    else:
        value = tomllib.loads(source.config_path.read_text())
        value.pop("local_model")
        value.pop("model_source")
        source.config_path.write_text(tomli_w.dumps(value))
    migration.migrate_project_state(deployment=manifest, workspace=source.root, backup=backup)
    assert load_project_deployment(manifest, project_root=source.root).project.state_exists()
    assert (target / "config.toml").exists() == (configuration != "absent")
    assert not (target / "experiments.jsonl").exists()


def test_unfinished_experiment_requires_reconciliation_before_migration(tmp_path: Path) -> None:
    source, manifest, target, backup = _fixture(tmp_path)
    definition = ExperimentRecorder(source).runs()[0].definition
    ExperimentStore(source.state_root / "experiments.jsonl").append(
        ExperimentEvent(
            event_id=uuid4(),
            run_id=uuid4(),
            status="started",
            at=datetime(2026, 11, 14, tzinfo=UTC),
            attempt=1,
            definition=definition,
        )
    )
    with pytest.raises(ProjectStateError, match="unfinished recorded experiments"):
        migration.migrate_project_state(deployment=manifest, workspace=source.root, backup=backup)
    assert source.state_exists()
    assert not target.exists()


def test_copy_write_corruption_is_rejected_before_retirement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, manifest, target, backup = _fixture(tmp_path)

    def corrupt(_reader: BinaryIO, writer: BinaryIO, **_options: object) -> None:
        writer.write(b"corrupt copy")

    monkeypatch.setattr(shutil, "copyfileobj", corrupt)
    with pytest.raises(ProjectStateError, match="staged record differs"):
        migration.migrate_project_state(deployment=manifest, workspace=source.root, backup=backup)
    assert source.state_exists()
    assert not target.exists()


def test_existing_retired_target_is_preserved_on_rollback(tmp_path: Path) -> None:
    source, manifest, target, backup = _fixture(tmp_path)
    migration.migrate_project_state(deployment=manifest, workspace=source.root, backup=backup)
    retained = target.with_name(f".{target.name}.rolled-back")
    retained.mkdir()
    (retained / "keep").write_text("preserve")
    with pytest.raises(ProjectStateError, match="rollback destination already exists"):
        migration.recover_project_migration(
            deployment=manifest, workspace=source.root, rollback=True
        )
    assert (retained / "keep").read_text() == "preserve"
    assert target.is_dir()


def test_cross_filesystem_retirement_is_rejected_before_copying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, manifest, target, backup = _fixture(tmp_path)
    original_stat = Path.stat

    def different_device(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        metadata = original_stat(path, follow_symlinks=follow_symlinks)
        if path == backup.parent:
            values = list(metadata)
            values[2] += 1
            return os.stat_result(values)
        return metadata

    monkeypatch.setattr(Path, "stat", different_device)
    with pytest.raises(ProjectStateError, match="same filesystem"):
        migration.migrate_project_state(deployment=manifest, workspace=source.root, backup=backup)
    assert source.state_exists()
    assert not target.exists()


def test_migration_and_recovery_preserve_directory_modes_heartwood_does_not_own(
    tmp_path: Path,
) -> None:
    source, manifest, target, backup = _fixture(tmp_path)
    source.root.chmod(0o775)
    target.parent.chmod(0o755)

    def modes() -> tuple[int, int]:
        return source.root.stat().st_mode & 0o7777, target.parent.stat().st_mode & 0o7777

    migration.migrate_project_state(deployment=manifest, workspace=source.root, backup=backup)
    assert modes() == (0o775, 0o755)
    assert target.stat().st_mode & 0o777 == 0o700
    migration.recover_project_migration(deployment=manifest, workspace=source.root)
    assert modes() == (0o775, 0o755)
    migration.recover_project_migration(deployment=manifest, workspace=source.root, rollback=True)
    assert modes() == (0o775, 0o755)
    assert source.state_root.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("boundary", ["staging", "publication"])
def test_gateway_started_during_migration_is_refused_with_a_stable_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    source, manifest, target, backup = _fixture(tmp_path)
    reached, release = threading.Event(), threading.Event()
    verify, rename = migration._verify_canonical, Path.rename

    def hold() -> None:
        reached.set()
        assert release.wait(30)

    def held_verification(project: ProjectContext, *, state_root: Path | None = None) -> None:
        if boundary == "staging" and state_root is None:
            hold()
        verify(project, state_root=state_root)

    def held_rename(path: Path, destination: Path) -> Path:
        if boundary == "publication" and destination == target:
            hold()
        return rename(path, destination)

    monkeypatch.setattr(migration, "_verify_canonical", held_verification)
    monkeypatch.setattr(Path, "rename", held_rename)
    outcome: list[BaseException | Path] = []

    def migrate() -> None:
        try:
            outcome.append(
                migration.migrate_project_state(
                    deployment=manifest, workspace=source.root, backup=backup
                )
            )
        except BaseException as error:
            outcome.append(error)

    worker = threading.Thread(target=migrate)
    worker.start()
    try:
        assert reached.wait(30)
        with pytest.raises(ProjectStateError, match="HW-PROJECT-004") as refused:
            load_project_deployment(manifest, project_root=source.root)
        assert "Traceback" not in str(refused.value)
        if boundary == "staging":
            # A local launch is excluded by the migration's project maintenance lease.
            with pytest.raises(ProjectOwnershipError):
                ProjectActivity(source).acquire_existing()
    finally:
        release.set()
        worker.join(60)
    assert len(outcome) == 1
    assert isinstance(outcome[0], Path)
    assert load_project_deployment(manifest, project_root=source.root).project.state_exists()


_SYNTHETIC_FICLONE = 0x40049409


@pytest.mark.parametrize("cloned", ["intact", "corrupt", "unsupported"])
def test_model_artifacts_are_cloned_when_supported_and_always_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cloned: str
) -> None:
    source, manifest, target, backup = _fixture(tmp_path)
    copied: list[int] = []
    copy = shutil.copyfileobj

    def counted_copy(reader: BinaryIO, writer: BinaryIO, length: int = 0) -> None:
        copied.append(1)
        copy(reader, writer, length)

    def clone(descriptor: int, request: int, source_descriptor: int) -> int:
        assert request == _SYNTHETIC_FICLONE
        if cloned == "unsupported":
            raise OSError(errno.EOPNOTSUPP, "synthetic filesystem without clones")
        content = os.pread(source_descriptor, 1 << 20, 0)
        os.write(descriptor, content if cloned == "intact" else content[::-1] + b"!")
        return 0

    monkeypatch.setattr(fcntl, "FICLONE", _SYNTHETIC_FICLONE, raising=False)
    monkeypatch.setattr(fcntl, "ioctl", clone)
    monkeypatch.setattr(shutil, "copyfileobj", counted_copy)
    if cloned == "corrupt":
        with pytest.raises(ProjectStateError, match="differs from its original"):
            migration.migrate_project_state(
                deployment=manifest, workspace=source.root, backup=backup
            )
        assert source.state_exists()
        assert not target.exists()
        return
    migration.migrate_project_state(deployment=manifest, workspace=source.root, backup=backup)
    assert (target / "models" / "synthetic.gguf").read_bytes() == b"synthetic model bytes"
    assert bool(copied) == (cloned == "unsupported")
