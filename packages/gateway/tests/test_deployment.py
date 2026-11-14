# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Protected project bindings reject workspace-controlled state authority."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
import tomli_w

from heartwood.gateway import (
    ProjectConfigError,
    ProjectContext,
    ProjectStateError,
    SessionGateway,
    load_project_deployment,
)
from heartwood.schemas import PolicyProfile
from heartwood.session import CommandKind, SessionCommand


def _manifest(tmp_path: Path) -> tuple[Path, Path, dict[str, object]]:
    workspace = tmp_path / "research"
    workspace.mkdir()
    state_parent = tmp_path / "protected"
    state_parent.mkdir(mode=0o700)
    identity = str(uuid4())
    record: dict[str, object] = {
        "schema_version": "heartwood.project-deployment.v1",
        "project_id": identity,
        "workspace": str(workspace),
        "state_root": str(state_parent / identity),
        "policy": PolicyProfile(policy_id="deployment", platform_id="generic").model_dump(),
    }
    path = tmp_path / "deployment.toml"
    _write(path, record)
    return path, workspace, record


def _write(path: Path, record: dict[str, object]) -> None:
    # TOML has no null; optional policy notes are omitted by the manifest format.
    policy = record.get("policy")
    payload = dict(record)
    if isinstance(policy, dict):
        payload["policy"] = {key: value for key, value in policy.items() if value is not None}
    path.write_text(tomli_w.dumps(payload))
    path.chmod(0o600)


def test_gateway_keeps_canonical_state_outside_research_files_and_reopens_it(
    tmp_path: Path,
) -> None:
    manifest, workspace, _record = _manifest(tmp_path)
    deployment = load_project_deployment(manifest, project_root=workspace)
    project = deployment.project
    gateway = SessionGateway(
        project=project, deployment_policy=deployment.policy, env={}, backend_id="deterministic"
    )
    gateway.config_store.save(gateway.config_store.load())
    command = SessionCommand(
        command_id="audit-1",
        session_id="synthetic-session",
        kind=CommandKind.AUDIT_EXPORT,
        actor_id="synthetic-researcher",
        created_at="2026-11-12T00:00:00Z",
        payload={},
    )
    gateway.handle(command)
    projection = gateway.session_projection(session_id="synthetic-session")
    gateway.stop()
    assert not (workspace / ".heartwood").exists()
    assert project.config_path.is_file()
    assert (project.sessions_dir / "synthetic-session").is_dir()
    assert not project.contains(project.config_path, include_state=True)
    restored = load_project_deployment(manifest, project_root=workspace)
    reopened = SessionGateway(
        project=restored.project,
        deployment_policy=restored.policy,
        env={},
        backend_id="deterministic",
    )
    recovered = reopened.persisted_session_projection(session_id="synthetic-session")
    assert recovered.model_dump(exclude={"stream_epoch"}) == projection.model_dump(
        exclude={"stream_epoch"}
    )
    reopened.stop()


def test_protected_model_paths_resolve_inside_external_store(tmp_path: Path) -> None:
    manifest, workspace, _record = _manifest(tmp_path)
    deployment = load_project_deployment(manifest, project_root=workspace)
    project = deployment.project
    project.initialize()
    model = project.models_dir / "model.gguf"
    model.write_bytes(b"synthetic")
    gateway = SessionGateway(project=project, deployment_policy=deployment.policy, env={})
    selected = gateway.config_store.select_local_model(artifact_id="reviewed", path=model)
    assert selected.local_model is not None
    assert selected.local_model.path == "models/model.gguf"
    assert selected.local_model.resolved_path(project) == model
    with pytest.raises(ProjectConfigError, match="stored under"):
        gateway.config_store.select_local_model(
            artifact_id="outside", path=workspace / "model.gguf"
        )
    gateway.stop()


def test_copied_workspace_and_local_identity_hints_do_not_select_protected_state(
    tmp_path: Path,
) -> None:
    manifest, workspace, _record = _manifest(tmp_path)
    copied = tmp_path / "copied"
    (copied / ".heartwood").mkdir(parents=True)
    (copied / ".heartwood" / "project.toml").write_bytes(manifest.read_bytes())
    with pytest.raises(ProjectStateError, match="does not authorize"):
        load_project_deployment(manifest, project_root=copied)
    with pytest.raises(ProjectStateError, match="outside the research"):
        load_project_deployment(copied / ".heartwood" / "project.toml", project_root=copied)
    assert not tuple((tmp_path / "protected").iterdir())
    assert load_project_deployment(manifest, project_root=workspace).project.root == workspace


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", "unknown"),
        ("project_id", "not-a-uuid"),
        ("state_root", "relative/state"),
        ("policy", {"deny_egress_by_default": False}),
        ("unexpected_authority", True),
    ],
)
def test_invalid_manifests_fail_before_creating_state(
    tmp_path: Path, field: str, value: object
) -> None:
    manifest, workspace, record = _manifest(tmp_path)
    _write(manifest, {**record, field: value})
    with pytest.raises(ProjectStateError):
        load_project_deployment(manifest, project_root=workspace)
    assert not tuple((tmp_path / "protected").iterdir())


def test_missing_manifest_never_falls_back_to_project_state(tmp_path: Path) -> None:
    workspace = tmp_path / "research"
    workspace.mkdir()
    with pytest.raises(ProjectStateError, match="unavailable"):
        load_project_deployment(tmp_path / "missing.toml", project_root=workspace)
    assert not (workspace / ".heartwood").exists()


def test_manifest_and_parent_permissions_are_checked(tmp_path: Path) -> None:
    manifest, workspace, _record = _manifest(tmp_path)
    manifest.chmod(0o666)
    with pytest.raises(ProjectStateError, match="write-protected"):
        load_project_deployment(manifest, project_root=workspace)
    manifest.chmod(0o600)
    (tmp_path / "protected").chmod(0o777)
    with pytest.raises(ProjectStateError, match="writable parent"):
        load_project_deployment(manifest, project_root=workspace)


def test_manifest_links_and_state_links_are_rejected(tmp_path: Path) -> None:
    manifest, workspace, record = _manifest(tmp_path)
    alias = tmp_path / "alias.toml"
    alias.symlink_to(manifest)
    with pytest.raises(ProjectStateError, match="non-symlink"):
        load_project_deployment(alias, project_root=workspace)
    state = Path(str(record["state_root"]))
    state.symlink_to(workspace, target_is_directory=True)
    with pytest.raises(ProjectStateError, match="non-symlink"):
        load_project_deployment(manifest, project_root=workspace)


def test_external_state_rejects_legacy_state_without_migration(tmp_path: Path) -> None:
    manifest, workspace, _record = _manifest(tmp_path)
    legacy = ProjectContext(workspace)
    legacy.initialize()
    before = legacy.state_path.read_bytes()
    with pytest.raises(ProjectStateError, match="requires migration"):
        load_project_deployment(manifest, project_root=workspace)
    assert legacy.state_path.read_bytes() == before
    assert not tuple((tmp_path / "protected").iterdir())


def test_project_binding_rejects_overlapping_state_and_mismatched_identity(tmp_path: Path) -> None:
    manifest, workspace, record = _manifest(tmp_path)
    for state in (workspace / str(record["project_id"]), tmp_path / "protected" / str(uuid4())):
        _write(manifest, {**record, "state_root": str(state)})
        with pytest.raises(ProjectStateError, match=r"disjoint|match project identity"):
            load_project_deployment(manifest, project_root=workspace)


def test_state_parent_redirection_is_rejected_after_launch(tmp_path: Path) -> None:
    manifest, workspace, _record = _manifest(tmp_path)
    project = load_project_deployment(manifest, project_root=workspace).project
    project.initialize()
    parent = project.state_root.parent
    moved = tmp_path / "moved-protected"
    parent.rename(moved)
    parent.symlink_to(moved, target_is_directory=True)
    with pytest.raises(ProjectStateError, match="changed after launch"):
        project.initialize()
    assert (moved / str(project.project_id) / "state.json").is_file()
