# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Command-line presentation of audit exports, checkpoints, and checkpoint signers."""

from __future__ import annotations

import argparse
import os
from datetime import UTC, datetime
from pathlib import Path

import uvicorn

from heartwood.gateway import (
    CheckpointSignerError,
    CheckpointSignerRegistry,
    LocalCheckpointSignerApp,
    LocalEd25519CheckpointSigner,
    ProjectContext,
    SessionGateway,
    checkpoint_public_key_fingerprint,
    initialize_local_checkpoint_signer,
    local_checkpoint_signer_socket,
    local_checkpoint_socket_path,
    user_checkpoint_signer_registry_path,
)
from heartwood.session import CommandKind, JsonValue, SessionCommand, new_command_id


def configure_audit(audit: argparse.ArgumentParser) -> None:
    """Attach audit export, verification, and checkpoint commands."""
    audit_subparsers = audit.add_subparsers(dest="audit_command", metavar="<audit-command>")
    audit_export = audit_subparsers.add_parser("export", help="Export scrubbed audit JSONL.")
    audit_export.add_argument("--output", type=Path, help="Optional copy destination.")
    audit_subparsers.add_parser("verify", help="Fully verify the current session audit history.")
    audit_signer = audit_subparsers.add_parser(
        "signer",
        help="Inspect or select deployment-approved checkpoint signing.",
    )
    audit_signer_subparsers = audit_signer.add_subparsers(
        dest="audit_signer_command",
        metavar="<signer-command>",
    )
    audit_signer_subparsers.add_parser("list", help="List approved signer profiles.")
    audit_signer_select = audit_signer_subparsers.add_parser(
        "select",
        help="Select an approved signer profile for this project.",
    )
    audit_signer_select.add_argument("profile_id")
    audit_signer_subparsers.add_parser(
        "default",
        help="Use the deployment default signer for this project.",
    )
    audit_checkpoint = audit_subparsers.add_parser(
        "checkpoint",
        help="Create a signed authoritative audit bundle outside the project.",
    )
    audit_checkpoint.add_argument("--output", type=Path, required=True)
    audit_checkpoint.add_argument("--deployment-id", required=True)
    audit_checkpoint.add_argument("--retention-policy", required=True)
    audit_checkpoint.add_argument(
        "--include-experiments",
        action="store_true",
        help="Retain and bind the project's current experiment records to the checkpoint",
    )
    audit_checkpoint.add_argument(
        "--retain-until",
        required=True,
        help="Retention end date in YYYY-MM-DD format.",
    )
    audit_checkpoint_verify = audit_subparsers.add_parser(
        "verify-checkpoint",
        help="Verify a signed audit bundle with a trusted public key.",
    )
    audit_checkpoint_verify.add_argument("bundle", type=Path)
    audit_checkpoint_verify.add_argument(
        "--public-key",
        type=Path,
        help="Advanced: independently trusted key; defaults to the active signer profile.",
    )


def configure_signer(signer: argparse.ArgumentParser) -> None:
    """Attach the local checkpoint signer commands."""
    signer_subparsers = signer.add_subparsers(dest="signer_command", metavar="<signer-command>")
    signer_init = signer_subparsers.add_parser(
        "init-local",
        help="Create local development signer material outside the project.",
    )
    signer_init.add_argument("--directory", type=Path)
    signer_init.add_argument("--profile", default="local-development")
    signer_init.add_argument("--key-version", default="v1")
    signer_serve = signer_subparsers.add_parser(
        "serve-local",
        help="Run an initialized signer on its private Unix socket.",
    )
    signer_serve.add_argument("--registry", type=Path)
    signer_serve.add_argument("--profile")
    signer_serve.add_argument("--private-key", type=Path)


def _handle_audit_export(
    gateway: SessionGateway,
    *,
    session_id: str,
    output: Path | None,
) -> int:
    events = gateway.handle(_command(session_id=session_id, kind=CommandKind.AUDIT_EXPORT)).events
    export_path = Path(str(events[-1].payload["path"]))
    if output is not None:
        output = gateway.copy_audit_export(session_id, output)
    print(f"Audit export: {output or export_path}")
    return 0


def _handle_audit_verify(gateway: SessionGateway, *, session_id: str) -> int:
    verification = gateway.verify_audit(session_id)
    print(
        "\n".join(
            (
                "Audit history verified",
                f"Events: {verification.event_count}",
                f"Terminal hash: {verification.terminal_event_hash or 'none'}",
                f"Export digest: {verification.content_sha256}",
            )
        )
    )
    return 0


def _handle_audit_signer(
    gateway: SessionGateway,
    *,
    command: str | None,
    args: argparse.Namespace,
) -> int:
    if command == "select":
        profile = gateway.select_checkpoint_signer(args.profile_id)
        print(f"Checkpoint signer selected: {profile.profile_id}")
        return 0
    if command == "default":
        profile = gateway.select_checkpoint_signer(None)
        print(f"Checkpoint signer: {profile.profile_id} (deployment default)")
        return 0
    profiles = gateway.checkpoint_signers()
    if not profiles:
        print("No deployment checkpoint signer registry is installed.")
        print("Production checkpoints remain unavailable until an operator configures one.")
        return 1
    active = gateway.active_checkpoint_signer().profile_id
    default = gateway.default_checkpoint_signer().profile_id
    print("Checkpoint signers")
    for profile in profiles:
        markers = []
        if profile.profile_id == active:
            markers.append("active")
        if profile.profile_id == default:
            markers.append("deployment default")
        suffix = f" ({', '.join(markers)})" if markers else ""
        print(f"  {profile.profile_id}{suffix}: {profile.mode}, {profile.algorithm}")
    return 0


def _handle_audit_checkpoint(
    gateway: SessionGateway,
    *,
    session_id: str,
    output: Path,
    deployment_id: str,
    retention_policy_id: str,
    retain_until: str,
    include_experiments: bool = False,
) -> int:
    verification = gateway.create_audit_checkpoint(
        session_id=session_id,
        output=output,
        deployment_id=deployment_id,
        retention_policy_id=retention_policy_id,
        retain_until=retain_until,
        include_experiments=include_experiments,
    )
    statement = verification.checkpoint.statement
    signature = verification.checkpoint.signature
    if verification.experiments is not None:
        print(f"Experiment export digest: {verification.experiments.sha256}")
    print(
        "\n".join(
            (
                f"Audit checkpoint: {output.expanduser().resolve()}",
                f"Events: {statement.audit_event_count}",
                f"Signer: {signature.signer_id}",
                f"Signing key: {signature.key_id} ({signature.key_version})",
                f"Retention: {statement.retention.policy_id} through "
                f"{statement.retention.retain_until}",
            )
        )
    )
    return 0


def _handle_audit_checkpoint_verification(
    gateway: SessionGateway,
    *,
    bundle: Path,
    public_key: Path | None,
) -> int:
    verification = gateway.verify_audit_checkpoint(bundle=bundle, public_key=public_key)
    if verification.experiments is not None:
        print(f"Experiment export verified: {verification.experiments.sha256}")
    statement = verification.checkpoint.statement
    print(
        "\n".join(
            (
                "Audit checkpoint verified",
                f"Deployment: {statement.deployment_id}",
                f"Session: {statement.session_id}",
                f"Events: {statement.audit_event_count}",
                f"Retention: {statement.retention.policy_id} through "
                f"{statement.retention.retain_until}",
            )
        )
    )
    return 0


def _handle_local_signer_init(
    *,
    project: ProjectContext,
    directory: Path | None,
    profile_id: str,
    key_version: str,
) -> int:
    root = (
        user_checkpoint_signer_registry_path(os.environ).parent
        if directory is None
        else directory.expanduser()
    )
    _require_deployment_owned_cli_path(project, root, label="local signer directory")
    setup = initialize_local_checkpoint_signer(
        directory=root,
        profile_id=profile_id,
        key_version=key_version,
    )
    print(
        "\n".join(
            (
                "Local checkpoint signer initialized",
                f"Registry: {setup.registry}",
                f"Profile: {setup.profile_id}",
                "",
                "Start it with:",
                f"heartwood signer serve-local --registry {setup.registry} "
                f"--profile {setup.profile_id}",
                "",
                f"Then select it in a project with: heartwood audit signer select "
                f"{setup.profile_id}",
            )
        )
    )
    return 0


def _handle_local_signer_serve(
    *,
    project: ProjectContext,
    registry: CheckpointSignerRegistry,
    profile_id: str | None,
    private_key: Path | None,
) -> int:
    if registry.source is not None:
        _require_deployment_owned_cli_path(
            project,
            registry.source,
            label="checkpoint signer registry",
        )
    profile = registry.profile(profile_id)
    if profile.mode != "development":
        raise CheckpointSignerError("serve-local requires a development signer profile")
    _require_deployment_owned_cli_path(
        project,
        profile.trusted_public_key,
        label="trusted checkpoint public key",
    )
    if profile.authorization_token_file is not None:
        _require_deployment_owned_cli_path(
            project,
            profile.authorization_token_file,
            label="checkpoint signer authorization token",
        )
    try:
        socket_path = local_checkpoint_socket_path(profile.endpoint)
    except ValueError as error:
        raise CheckpointSignerError(str(error)) from error
    _require_deployment_owned_cli_path(project, socket_path, label="local signer socket")
    if private_key is None:
        if registry.source is None:
            raise CheckpointSignerError(
                "local signer private key must be provided when the registry has no source"
            )
        private_key = registry.source.parent / "local-checkpoint-private.pem"
    resolved_private_key = _require_deployment_owned_cli_path(
        project,
        private_key,
        label="local signer private key",
    )
    signer = LocalEd25519CheckpointSigner(
        private_key=resolved_private_key,
        signer_id=profile.signer_id,
        key_id=profile.key_id,
        key_version=profile.key_version,
    )
    if checkpoint_public_key_fingerprint(signer.public_key) != profile.public_key_sha256:
        raise CheckpointSignerError("local signer private key does not match the registry")
    token = profile.authorization_token()
    if token is None:  # pragma: no cover - registry validation enforces this
        raise CheckpointSignerError("local signer authorization token is unavailable")
    print(f"Local checkpoint signer: {profile.endpoint}")
    print("Keep this process running while creating checkpoints.")
    try:
        with local_checkpoint_signer_socket(profile.endpoint) as listener:
            uvicorn.run(
                LocalCheckpointSignerApp(signer, authorization_token=token),
                fd=listener.fileno(),
                log_level="warning",
                access_log=False,
                proxy_headers=False,
            )
    except (OSError, ValueError) as error:
        raise CheckpointSignerError(
            "local signer socket cannot be served; "
            "verify its private directory and existing listener"
        ) from error
    return 0


def _require_deployment_owned_cli_path(
    project: ProjectContext,
    path: Path,
    *,
    label: str,
) -> Path:
    expanded = path.expanduser()
    resolved = expanded if expanded.is_absolute() else (Path.cwd() / expanded)
    resolved = resolved.resolve()
    if resolved == project.root or project.root in resolved.parents:
        raise CheckpointSignerError(f"{label} must be outside the Heartwood project")
    return resolved


def _command(
    *,
    session_id: str,
    kind: CommandKind,
    payload: dict[str, JsonValue] | None = None,
) -> SessionCommand:
    return SessionCommand(
        command_id=new_command_id(session_id, kind),
        session_id=session_id,
        kind=kind,
        actor_id="human",
        created_at=_utc_now(),
        payload={} if payload is None else payload,
    )


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
