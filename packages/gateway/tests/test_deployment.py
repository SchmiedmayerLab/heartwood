# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Protected project bindings reject workspace-controlled state authority."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
import tomli_w

from heartwood.adapters.platform.generic import GenericPlatformAdapter
from heartwood.audit import CheckpointSignerError, CheckpointSignerProfile, CheckpointSignerRegistry
from heartwood.gateway import (
    ProjectConfigError,
    ProjectContext,
    ProjectStateError,
    SessionGateway,
    load_project_deployment,
)
from heartwood.gateway._credentials import CredentialStoreError
from heartwood.gateway._deployment import DeploymentRequirements
from heartwood.gateway._project_config import LocalModelSelection
from heartwood.model_policy import POLICY_RESTRICTIONS
from heartwood.schemas import EffectivePolicyResponse, JsonValue
from heartwood.session import CommandKind, EventKind, SessionCommand


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
        # Omitted ceiling lists follow the detected platform policy.
        "policy": {"policy_id": "deployment", "platform_id": "generic"},
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
    selected = gateway.config_store.select_local_model(
        LocalModelSelection(artifact_id="reviewed", path=str(model))
    )
    assert selected.local_model is not None
    assert selected.local_model.path == "models/model.gguf"
    assert selected.local_model.resolved_path(project) == model
    with pytest.raises(ProjectConfigError, match="stored under"):
        gateway.config_store.select_local_model(
            LocalModelSelection(artifact_id="outside", path=str(workspace / "model.gguf"))
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
    with pytest.raises(ProjectStateError, match="HW-DEPLOY-001"):
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
    with pytest.raises(ProjectStateError, match=r"HW-PROJECT-004: .*requires migration"):
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


@pytest.mark.parametrize(
    ("update", "env"),
    [
        ({"allowed_capability_tiers": ["autonomous"]}, {}),
        ({"allowed_action_review_modes": ["review-all", "review-outside-effects"]}, {}),
        ({"allowed_credential_backends": ["managed-identity"]}, {}),
        (
            {"allowed_model_endpoints": ["https://unlisted.example/v1/chat/completions"]},
            {"HEARTWOOD_PLATFORM": "carina"},
        ),
        ({"credential_allowlist": ["UNLISTED_KEY"]}, {"HEARTWOOD_PLATFORM": "carina"}),
    ],
)
def test_manifest_policy_that_widens_the_platform_policy_is_refused(
    tmp_path: Path, update: dict[str, object], env: dict[str, str]
) -> None:
    manifest, workspace, record = _manifest(tmp_path)
    platform = env.get("HEARTWOOD_PLATFORM", "generic")
    policy = {**cast(dict[str, object], record["policy"]), "platform_id": platform, **update}
    _write(manifest, {**record, "policy": policy})
    with pytest.raises(ProjectStateError, match="HW-DEPLOY-002"):
        load_project_deployment(manifest, project_root=workspace, env=env)
    assert not tuple((tmp_path / "protected").iterdir())


def test_manifest_policy_narrows_and_omitted_lists_follow_the_platform(tmp_path: Path) -> None:
    manifest, workspace, record = _manifest(tmp_path)
    platform = GenericPlatformAdapter().default_policy_profile()
    policy = load_project_deployment(manifest, project_root=workspace, env={}).policy
    assert policy.allowed_action_review_modes == platform.allowed_action_review_modes
    assert policy.allowed_credential_backends == platform.allowed_credential_backends
    narrowed = {
        **cast(dict[str, object], record["policy"]),
        "allowed_action_review_modes": ["review-all"],
        "allowed_credential_backends": ["mounted-file"],
        # A platform with Custom API may name any compatible endpoint and credential.
        "allowed_model_endpoints": ["https://approved.example/v1/chat/completions"],
        "credential_allowlist": ["APPROVED_KEY"],
    }
    _write(manifest, {**record, "policy": narrowed})
    policy = load_project_deployment(manifest, project_root=workspace, env={}).policy
    assert policy.allowed_action_review_modes == ("review-all",)
    assert policy.allowed_credential_backends == ("mounted-file",)


@pytest.mark.parametrize("requirement", ["isolated_worker", "checkpoint_evidence"])
def test_manifest_requirements_refuse_a_profile_without_the_control(
    tmp_path: Path, requirement: str
) -> None:
    manifest, workspace, record = _manifest(tmp_path)
    _write(manifest, {**record, "requirements": {requirement: True}})
    with pytest.raises(ProjectStateError, match="HW-DEPLOY-003"):
        load_project_deployment(manifest, project_root=workspace)
    assert not tuple((tmp_path / "protected").iterdir())
    _write(manifest, {**record, "requirements": {"unknown_control": True}})
    with pytest.raises(ProjectStateError, match="HW-DEPLOY-001"):
        load_project_deployment(manifest, project_root=workspace)


def _command(kind: CommandKind, payload: dict[str, JsonValue] | None = None) -> SessionCommand:
    return SessionCommand(
        command_id=str(uuid4()),
        session_id="synthetic-session",
        kind=kind,
        actor_id="synthetic-researcher",
        created_at="2026-11-15T00:00:00Z",
        payload=payload or {},
    )


def test_manifest_and_preference_changes_during_a_session_are_journaled_before_dispatch(
    tmp_path: Path,
) -> None:
    manifest, workspace, record = _manifest(tmp_path)
    wider = {
        **cast(dict[str, object], record["policy"]),
        "allowed_capability_tiers": ["supervised", "experimental"],
    }
    _write(manifest, {**record, "policy": wider})
    deployment = load_project_deployment(manifest, project_root=workspace, env={})
    gateway = SessionGateway(deployment=deployment, env={}, backend_id="deterministic")
    try:
        gateway.select_action_review_mode("review-changes")
        gateway.handle(_command(CommandKind.CHAT, {"prompt": "Summarize the synthetic data."}))
        policy = {
            **cast(dict[str, object], record["policy"]),
            "allowed_capability_tiers": ["supervised"],
        }
        _write(manifest, {**record, "policy": policy})
        gateway.handle(_command(CommandKind.CHAT, {"prompt": "Continue."}))
        effective = gateway.effective_policy().safe_dict()
        assert effective["capability_tiers"] == ["supervised"]
        assert effective["manifest_sha256"] == hashlib.sha256(manifest.read_bytes()).hexdigest()
        # A stricter preference selected through any interface is journaled the same way.
        gateway.select_action_review_mode("review-all")
        gateway.handle(_command(CommandKind.CHAT, {"prompt": "Continue again."}))
        events = gateway._service("synthetic-session").replay_events()
        changes = [
            (index, event.payload)
            for index, event in enumerate(events)
            if event.kind == EventKind.POLICY_DECISION_RECORDED
        ]
        assert len(changes) == 2
        for index, _payload in changes:
            assert events[index + 1].kind == EventKind.MODEL_CALL_DECISION_RECORDED
        (_, manifest_change), (_, preference_change) = changes
        assert manifest_change["policy_sha256"] != manifest_change["previous_policy_sha256"]
        assert manifest_change["action_review_mode"] == "review-changes"
        assert preference_change["policy_sha256"] == preference_change["previous_policy_sha256"]
        assert preference_change["previous_action_review_mode"] == "review-changes"
        assert preference_change["action_review_mode"] == "review-all"
    finally:
        gateway.stop()


def test_a_narrower_manifest_that_excludes_the_stored_selection_applies_review_all(
    tmp_path: Path,
) -> None:
    manifest, workspace, record = _manifest(tmp_path)
    gateway = SessionGateway(
        deployment=load_project_deployment(manifest, project_root=workspace, env={}),
        env={},
        backend_id="deterministic",
    )
    try:
        gateway.select_action_review_mode("review-changes")
        gateway.handle(_command(CommandKind.CHAT, {"prompt": "Start."}))
        policy = {
            **cast(dict[str, object], record["policy"]),
            "allowed_action_review_modes": ["review-all"],
        }
        _write(manifest, {**record, "policy": policy})
        result = gateway.handle(_command(CommandKind.CHAT, {"prompt": "Continue."}))
        assert not any(
            str(event.payload.get("reason")).startswith("HW-DEPLOY-004") for event in result.events
        )
        # The narrowed mode is recorded before the next model dispatch, which proceeds.
        kinds = [event.kind for event in result.events]
        decision = next(
            event for event in result.events if event.kind == EventKind.POLICY_DECISION_RECORDED
        )
        assert decision.payload["action_review_mode"] == "review-all"
        assert kinds.index(EventKind.POLICY_DECISION_RECORDED) < kinds.index(
            EventKind.MODEL_CALL_DECISION_RECORDED
        )
        assert gateway.action_settings()["review_mode"] == "review-all"
    finally:
        gateway.stop()


@pytest.mark.parametrize("change", ["wider", "binding", "backends", "unreadable"])
def test_other_manifest_changes_stop_dispatch_until_restart(tmp_path: Path, change: str) -> None:
    manifest, workspace, record = _manifest(tmp_path)
    narrow = {
        **cast(dict[str, object], record["policy"]),
        "allowed_action_review_modes": ["review-all"],
    }
    _write(manifest, {**record, "policy": narrow})
    gateway = SessionGateway(
        deployment=load_project_deployment(manifest, project_root=workspace, env={}),
        env={},
        backend_id="deterministic",
    )
    try:
        gateway.handle(_command(CommandKind.CHAT, {"prompt": "Start."}))
        if change == "wider":
            _write(manifest, record)
        elif change == "binding":
            identity = str(uuid4())
            state_root = tmp_path / "protected" / identity
            _write(
                manifest,
                {**record, "policy": narrow, "project_id": identity, "state_root": str(state_root)},
            )
        elif change == "backends":
            _write(
                manifest,
                {**record, "policy": {**narrow, "allowed_credential_backends": ["mounted-file"]}},
            )
        else:
            manifest.unlink()
        result = gateway.handle(_command(CommandKind.CHAT, {"prompt": "Continue."}))
        reasons = [str(event.payload.get("reason")) for event in result.events]
        code = "HW-DEPLOY-001" if change == "unreadable" else "HW-DEPLOY-004"
        assert any(reason.startswith(code) for reason in reasons)
        assert not any(event.kind == EventKind.USER_MESSAGE_RECORDED for event in result.events)
        assert gateway.action_settings()["review_mode"] == "review-all"
        readiness = gateway.project_readiness()
        assert readiness["state"] == "recovery-required"
        assert readiness["policy"]["change_refused"] is not None
        # Restoring the manifest does not resume dispatch; only a restart applies the change.
        _write(manifest, {**record, "policy": narrow})
        refused = gateway.handle(_command(CommandKind.CHAT, {"prompt": "Again."}))
        assert not any(event.kind == EventKind.USER_MESSAGE_RECORDED for event in refused.events)
    finally:
        gateway.stop()


# A narrower value for every field whose absence from the manifest permits anything.
_NARROWED: dict[str, list[str]] = {
    "allowed_action_review_modes": ["review-all"],
    "allowed_credential_backends": ["mounted-file"],
    "allowed_checkpoint_signer_profiles": ["research-records"],
    "allowed_data_destinations": ["same-compute"],
}


def test_every_field_where_absent_means_any_is_checked_for_live_removal() -> None:
    assert set(_NARROWED) == {
        name for name, rule in POLICY_RESTRICTIONS.items() if rule.absent_means_any
    }


@pytest.mark.parametrize("field", sorted(_NARROWED))
def test_removing_a_manifest_restriction_while_running_never_applies(
    tmp_path: Path, field: str
) -> None:
    manifest, workspace, record = _manifest(tmp_path)
    narrowed = {**cast(dict[str, object], record["policy"]), field: _NARROWED[field]}
    _write(manifest, {**record, "policy": narrowed})
    gateway = SessionGateway(
        deployment=load_project_deployment(manifest, project_root=workspace, env={}),
        env={},
        backend_id="deterministic",
    )
    try:
        gateway.handle(_command(CommandKind.CHAT, {"prompt": "Start."}))
        # Removing the line widens the field to anything the platform or registry permits.
        _write(manifest, record)
        result = gateway.handle(_command(CommandKind.CHAT, {"prompt": "Continue."}))
        reasons = [str(event.payload.get("reason")) for event in result.events]
        assert any(reason.startswith("HW-DEPLOY-004") for reason in reasons)
        assert not any(event.kind == EventKind.USER_MESSAGE_RECORDED for event in result.events)
        reported = gateway.effective_policy().safe_dict()
        assert reported[cast(str, POLICY_RESTRICTIONS[field].reported_as)] == _NARROWED[field]
    finally:
        gateway.stop()


def test_interfaces_report_every_policy_field_and_deployment_requirement(tmp_path: Path) -> None:
    manifest, workspace, _record = _manifest(tmp_path)
    gateway = SessionGateway(
        deployment=load_project_deployment(manifest, project_root=workspace, env={}),
        env={},
        backend_id="deterministic",
    )
    try:
        reported = gateway.effective_policy().safe_dict()
    finally:
        gateway.stop()
    policy_names = {
        rule.reported_as for rule in POLICY_RESTRICTIONS.values() if rule.reported_as is not None
    }
    # Fields without a reported name are identifiers or prose and grant no authority.
    assert {name for name, rule in POLICY_RESTRICTIONS.items() if rule.reported_as is None} == {
        "schema_version",
        "notes",
    }
    assert policy_names | set(DeploymentRequirements.model_fields) <= set(reported)
    assert set(reported) == set(EffectivePolicyResponse.__annotations__)


def _signer(tmp_path: Path, profile_id: str) -> CheckpointSignerProfile:
    return CheckpointSignerProfile(
        profile_id=profile_id,
        mode="production",
        endpoint=f"https://{profile_id}.example.invalid/v1/checkpoints/sign",
        signer_id=profile_id,
        key_id="audit-signing",
        key_version="v1",
        algorithm="ed25519",
        public_key_sha256="0" * 64,
        trusted_public_key=tmp_path / f"{profile_id}.pem",
    )


def test_policy_fixes_the_checkpoint_signer_and_a_project_cannot_select_another(
    tmp_path: Path,
) -> None:
    manifest, workspace, record = _manifest(tmp_path)
    policy = {
        **cast(dict[str, object], record["policy"]),
        "allowed_checkpoint_signer_profiles": ["research-records"],
    }
    _write(manifest, {**record, "policy": policy})
    registry = CheckpointSignerRegistry(
        profiles=(_signer(tmp_path, "research-records"), _signer(tmp_path, "other")),
        default_profile="other",
    )
    gateway = SessionGateway(
        deployment=load_project_deployment(manifest, project_root=workspace),
        checkpoint_signer_registry=registry,
        env={},
        backend_id="deterministic",
    )
    try:
        assert gateway.active_checkpoint_signer().profile_id == "research-records"
        with pytest.raises(CheckpointSignerError, match="does not permit"):
            gateway.select_checkpoint_signer("other")
        with pytest.raises(ProjectConfigError, match="signer profile is not allowed"):
            gateway.config_store.select_checkpoint_signer("other")
        assert gateway.select_checkpoint_signer(None).profile_id == "research-records"
        assert gateway.effective_policy().safe_dict()["checkpoint_signer_profiles"] == [
            "research-records"
        ]
    finally:
        gateway.stop()


def test_credential_backends_outside_the_policy_cannot_hold_credentials(tmp_path: Path) -> None:
    manifest, workspace, record = _manifest(tmp_path)
    policy = {
        **cast(dict[str, object], record["policy"]),
        "allowed_credential_backends": ["mounted-file"],
    }
    _write(manifest, {**record, "policy": policy})
    gateway = SessionGateway(
        deployment=load_project_deployment(manifest, project_root=workspace),
        env={"OPERATOR_KEY": "synthetic-operator-binding"},
        backend_id="deterministic",
    )
    try:
        availability = gateway.credential_store.availability()
        assert availability.backends == ()
        assert not availability.persistence_available
        with pytest.raises(CredentialStoreError, match="deployment policy"):
            gateway.credential_store.save("ENTERED_KEY", "synthetic-entered-value")
        with pytest.raises(CredentialStoreError, match="persistence is unavailable"):
            gateway.credential_store.save("ENTERED_KEY", "synthetic-value", remember=True)
        assert gateway.credential_store.resolve("OPERATOR_KEY") == "synthetic-operator-binding"
    finally:
        gateway.stop()
