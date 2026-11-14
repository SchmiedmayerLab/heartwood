# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Explicit, recoverable retirement of local state into an operator deployment."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tomllib
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from uuid import uuid4

import tomli_w

from heartwood.core_adapter import session_maintenance
from heartwood.gateway._deployment import _read_project_deployment, _validate_trusted_parents
from heartwood.gateway._experiment_store import ExperimentStore
from heartwood.gateway._project import ProjectContext, ProjectStateError, _current_state_payload
from heartwood.gateway._project_activity import project_gateway_maintenance
from heartwood.gateway._project_config import project_config_from_mapping
from heartwood.gateway._session_migration import staged_session_records
from heartwood.model_policy import validate_policy_restriction
from heartwood.persistence import (
    fsync_directory,
    native_file_lock,
    read_private_json,
    read_private_text,
    write_private_json_atomic,
    write_private_text_atomic,
)
from heartwood.skills import SkillArtifactStore

_SCHEMA = "heartwood.project-migration.v1"
_DIRECTORIES = ("models", "skills", "audit", "runtime", "logs", "cache")


def migrate_project_state(*, deployment: Path, workspace: Path, backup: Path) -> Path:
    """Stage and verify state, retain the original, and activate one trusted binding.

    The operator must first stop every gateway, runtime and recorded experiment.
    An interrupted retirement stays fenced until explicit recovery. No backup or
    failed stage is deleted automatically.
    """
    bound = _read_project_deployment(deployment, project_root=workspace, validate_state=False)
    target = bound.project
    source = ProjectContext(workspace)
    journal = _journal(target)
    _require_backup(source, target, backup)
    with native_file_lock(journal.with_suffix(".lock"), timeout=0, reentrant=False):
        if journal.exists() or target.state_root.exists() or backup.exists():
            raise ProjectStateError("migration destination already exists; inspect or recover it")
        with staged_session_records(source, target) as stage, ExitStack() as leases:
            for path in sorted(source.state_root.glob(".experiment-*.lock")):
                leases.enter_context(native_file_lock(path, timeout=0, reentrant=False))
            leases.enter_context(native_file_lock(source.skills_dir / ".install.lock"))
            leases.enter_context(native_file_lock(source.state_root / ".experiments.jsonl.lock"))
            _verify_canonical(source)
            if source.config_path.exists():
                config = project_config_from_mapping(
                    tomllib.loads(read_private_text(source.config_path)), project=source
                )
                validate_policy_restriction(config.policy, bound.policy)
            original = _snapshot(source.state_root)
            for name in _DIRECTORIES:
                destination = stage / name
                if name in {"runtime", "logs", "cache"}:
                    destination.mkdir(mode=0o700)
                else:
                    _copy_tree(source.state_root / name, destination)
            for name in ("config.toml", "experiments.jsonl"):
                path = source.state_root / name
                if path.exists():
                    _copy_file(path, stage / name)
            if (stage / "config.toml").exists():
                _rebind_model(source, target, stage / "config.toml")
            write_private_json_atomic(stage / "state.json", _current_state_payload())
            write_private_text_atomic(stage / ".gitignore", "*\n")
            _verify_canonical(target, state_root=stage)
            for relative, identity in _snapshot(stage).items():
                if (
                    relative not in {"config.toml", "state.json", ".gitignore", "receipt.json"}
                    and original.get(relative) != identity
                ):
                    raise ProjectStateError("staged records differ from their source")
            if _snapshot(source.state_root) != original:
                raise ProjectStateError("source state changed during migration staging")
            # The complete receipt retains historical byte identities without
            # promoting execution recovery metadata into the active state.
            (stage / "receipt.json").unlink()
            retained_stage = target.state_root.with_name(f".{target.project_id}.migration-stage")
            receipt: dict[str, Any] = {
                "schema_version": _SCHEMA,
                "workspace": str(source.root),
                "project_id": str(target.project_id),
                "state_root": str(target.state_root),
                "backup": str(backup),
                "phase": "complete",
                "assurance": "legacy-history-only",
                "source_files": original,
                "target_files": _snapshot(stage),
                "stage": str(retained_stage),
            }
            write_private_json_atomic(stage / "migration.json", receipt)
            if retained_stage.exists():
                raise ProjectStateError("a retained migration stage already exists")
            stage.rename(retained_stage)
            fsync_directory(retained_stage.parent)
            write_private_json_atomic(journal, receipt)
            _publish(source, target, receipt)
    return journal


def recover_project_migration(*, deployment: Path, workspace: Path, rollback: bool = False) -> Path:
    """Resume a verified interrupted publication, or restore an untouched original.

    Rollback is refused after any canonical target mutation. Both the retired
    target and protected backup are retained for explicit operator inspection.
    """
    bound = _read_project_deployment(deployment, project_root=workspace, validate_state=False)
    target = bound.project
    source = ProjectContext(workspace)
    journal = _journal(target)
    with native_file_lock(journal.with_suffix(".lock"), timeout=0, reentrant=False):
        expected_stage = target.state_root.with_name(f".{target.project_id}.migration-stage")
        receipt = read_private_json(
            journal if journal.exists() else expected_stage / "migration.json"
        )
        if (
            receipt.get("schema_version") != _SCHEMA
            or receipt.get("workspace") != str(source.root)
            or receipt.get("project_id") != str(target.project_id)
            or receipt.get("state_root") != str(target.state_root)
            or receipt.get("phase") != "complete"
            or not isinstance(receipt.get("backup"), str)
            or not isinstance(receipt.get("stage"), str)
            or not isinstance(receipt.get("source_files"), dict)
            or not isinstance(receipt.get("target_files"), dict)
        ):
            raise ProjectStateError("migration journal does not match the operator binding")
        backup = Path(receipt["backup"])
        _require_backup(source, target, backup, existing=True)
        stage = Path(receipt["stage"])
        if stage != expected_stage or stage.is_symlink():
            raise ProjectStateError("migration stage does not match its trusted target")
        with ExitStack() as leases:
            if target.state_root.exists():
                leases.enter_context(project_gateway_maintenance(target))
                leases.enter_context(session_maintenance(target.sessions_dir))
                leases.enter_context(native_file_lock(target.config_lock_path, timeout=0))
                leases.enter_context(native_file_lock(target.skills_dir / ".install.lock"))
                leases.enter_context(
                    native_file_lock(target.state_root / ".experiments.jsonl.lock")
                )
                if _snapshot(target.state_root) != receipt["target_files"]:
                    raise ProjectStateError("target changed after migration; rollback is unsafe")
            else:
                retained = target.state_root.with_name(f".{target.project_id}.rolled-back")
                candidate = retained if rollback and retained.exists() else stage
                if not candidate.exists() or _snapshot(candidate) != receipt["target_files"]:
                    raise ProjectStateError("verified migration stage is unavailable or changed")
            original = backup if backup.exists() else source.state_root
            _lock_retired_state(leases, original)
            if rollback and original != source.state_root and source.state_root.exists():
                _lock_retired_state(leases, source.state_root)
            if _snapshot(original) != receipt["source_files"]:
                raise ProjectStateError("original migration records changed; recovery stopped")
            if not journal.exists():
                write_private_json_atomic(journal, receipt)
            if rollback:
                _rollback(source, target, receipt)
            else:
                _publish(source, target, receipt)
    return journal


def _publish(source: ProjectContext, target: ProjectContext, receipt: dict[str, Any]) -> None:
    fence = source.root / ".heartwood-migration.json"
    if not fence.exists():
        write_private_json_atomic(fence, {"schema_version": _SCHEMA, "retired": True})
    backup = Path(receipt["backup"])
    if source.state_root.exists():
        if backup.exists():
            raise ProjectStateError("source and backup both exist; recovery requires inspection")
        source.state_root.rename(backup)
        fsync_directory(source.root)
        fsync_directory(backup.parent)
    if not target.state_root.exists():
        Path(receipt["stage"]).rename(target.state_root)
        fsync_directory(target.state_root.parent)
    if not target.state_exists():
        raise ProjectStateError("protected state did not activate")


def _rollback(source: ProjectContext, target: ProjectContext, receipt: dict[str, Any]) -> None:
    backup = Path(receipt["backup"])
    if target.state_root.exists():
        retired = target.state_root.with_name(f".{target.project_id}.rolled-back")
        if retired.exists():
            raise ProjectStateError("retained rollback destination already exists")
        target.state_root.rename(retired)
        fsync_directory(retired.parent)
    if backup.exists():
        if source.state_root.exists():
            if _snapshot(source.state_root) != receipt["source_files"]:
                raise ProjectStateError(
                    "local state already exists; original will not be overwritten"
                )
        else:
            # Restore a copy so the protected original remains available for inspection.
            restore = source.root / ".heartwood-restore"
            if restore.exists() and _snapshot(restore) != receipt["source_files"]:
                restore = source.root / f".heartwood-restore-{uuid4().hex}"
            if not restore.exists():
                _copy_tree(backup, restore)
            if _snapshot(restore) != receipt["source_files"]:
                raise ProjectStateError("restored records differ from the protected original")
            restore.rename(source.state_root)
            fsync_directory(source.root)
    fence = source.root / ".heartwood-migration.json"
    fence.unlink(missing_ok=True)
    fsync_directory(source.root)
    if not source.state_exists():
        raise ProjectStateError("original state did not restore")


def _journal(target: ProjectContext) -> Path:
    _validate_trusted_parents(target.state_root.parent)
    return target.state_root.with_name(f".{target.project_id}.migration.json")


def _lock_retired_state(leases: ExitStack, root: Path) -> None:
    if root.is_symlink() or not root.is_dir():
        raise ProjectStateError("original migration state is unavailable")
    leases.enter_context(native_file_lock(root / ".state.lock", timeout=0))
    leases.enter_context(native_file_lock(root / ".config.lock", timeout=0))
    leases.enter_context(session_maintenance(root / "sessions"))
    for path in sorted((root / "runtime" / "gateway-leases").glob("*.lock")):
        leases.enter_context(native_file_lock(path, timeout=0, reentrant=False))
    for path in sorted(root.glob(".experiment-*.lock")):
        leases.enter_context(native_file_lock(path, timeout=0, reentrant=False))
    leases.enter_context(native_file_lock(root / "skills" / ".install.lock"))
    leases.enter_context(native_file_lock(root / ".experiments.jsonl.lock"))


def _require_backup(
    source: ProjectContext, target: ProjectContext, backup: Path, *, existing: bool = False
) -> None:
    if (
        not backup.is_absolute()
        or backup.resolve() != backup
        or backup.is_symlink()
        or backup.is_relative_to(source.root)
        or source.root.is_relative_to(backup)
        or backup.is_relative_to(target.state_root)
        or target.state_root.is_relative_to(backup)
    ):
        raise ProjectStateError("backup must be a separate absolute protected directory")
    _validate_trusted_parents(backup.parent)
    if backup.parent.stat().st_dev != source.root.stat().st_dev:
        raise ProjectStateError("atomic source retirement requires a backup on the same filesystem")
    if not existing and (backup.exists() or backup.is_symlink()):
        raise ProjectStateError("backup destination already exists")


def _snapshot(root: Path) -> dict[str, dict[str, Any]]:
    files: dict[str, dict[str, Any]] = {}
    if root.is_symlink() or not root.is_dir():
        raise ProjectStateError("migration records require a real directory")
    for path in sorted(root.rglob("*")):
        metadata = path.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode))
            or (stat.S_ISREG(metadata.st_mode) and metadata.st_nlink != 1)
        ):
            raise ProjectStateError("migration cannot follow links or copy special files")
        if (
            path.is_dir()
            or (metadata.st_size == 0 and _coordination_lock(path.relative_to(root)))
            or path == root / "migration.json"
        ):
            continue
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        files[path.relative_to(root).as_posix()] = {"bytes": metadata.st_size, "sha256": digest}
    return files


def _coordination_lock(relative: Path) -> bool:
    if relative.as_posix() in {
        ".state.lock",
        ".config.lock",
        ".experiments.jsonl.lock",
        "sessions/.writers.lock",
        "skills/.install.lock",
    }:
        return True
    if relative.parent == Path() and relative.name.startswith(".experiment-"):
        return relative.suffix == ".lock"
    if relative.parent == Path("runtime/gateway-leases"):
        return relative.suffix == ".lock"
    return (
        len(relative.parts) == 3
        and relative.parts[0] == "sessions"
        and relative.name
        in {".writer.lock", ".snapshot.lock", ".metadata.lock", ".audit.jsonl.lock"}
    )


def _copy_tree(source: Path, target: Path) -> None:
    target.mkdir(mode=0o700)
    for path in sorted(source.iterdir()):
        if path.is_symlink():
            raise ProjectStateError("migration cannot copy symbolic links")
        if path.is_dir():
            _copy_tree(path, target / path.name)
        elif path.is_file():
            _copy_file(path, target / path.name)
        else:
            raise ProjectStateError("migration cannot copy special files")
    fsync_directory(target)


def _copy_file(source: Path, target: Path) -> None:
    with source.open("rb") as reader, target.open("xb") as writer:
        os.fchmod(writer.fileno(), 0o600)
        shutil.copyfileobj(reader, writer, length=1024 * 1024)
        writer.flush()
        os.fsync(writer.fileno())
    with source.open("rb") as reader, target.open("rb") as copied:
        if (
            hashlib.file_digest(reader, "sha256").digest()
            != hashlib.file_digest(copied, "sha256").digest()
        ):
            raise ProjectStateError("staged record differs from its original")


def _rebind_model(source: ProjectContext, target: ProjectContext, path: Path) -> None:
    value = tomllib.loads(read_private_text(path))
    config = project_config_from_mapping(value, project=source)
    if config.local_model is not None:
        resolved = config.local_model.resolved_path(source)
        relative = resolved.relative_to(source.models_dir)
        value["local_model"]["path"] = str(Path("models") / relative)
    project_config_from_mapping(value, project=target)
    write_private_text_atomic(path, tomli_w.dumps(value))


def _verify_canonical(project: ProjectContext, *, state_root: Path | None = None) -> None:
    root = state_root or project.state_root
    if state_root is None and (root / "migration.json").exists():
        raise ProjectStateError("local state contains conflicting migration evidence")
    _snapshot(root)
    experiments = root / "experiments.jsonl"
    pending = root / ".experiments.jsonl.pending"
    if pending.exists() or pending.is_symlink():
        raise ProjectStateError("recover the interrupted experiment append before migration")
    if experiments.exists():
        runs = ExperimentStore(experiments).runs()
        if any(run.status in {"started", "resumed"} for run in runs):
            raise ProjectStateError("reconcile unfinished recorded experiments before migration")
    skills = SkillArtifactStore(root / "skills")
    for record in skills.records():
        skills.manifest(record)
    if (root / "config.toml").exists():
        project_config_from_mapping(
            tomllib.loads(read_private_text(root / "config.toml")), project=project
        )
