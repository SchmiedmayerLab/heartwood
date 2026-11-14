# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Protected relocation preserves history and never admits two project writers."""

from __future__ import annotations

import errno
import json
import os
import shutil
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
                "policy": policy.model_dump(exclude_none=True),
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

    def interrupt(path: Path, value: Mapping[str, object]) -> None:
        if path == journal:
            raise OSError("synthetic journal interruption")
        write(path, value)

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
