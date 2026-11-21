# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Operator-owned project and policy binding for gateway launch."""

from __future__ import annotations

import hashlib
import json
import os
import re
import ssl
import stat
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError

from heartwood.adapters.platform import select_platform_adapter
from heartwood.gateway._diagnostics import diagnostic_for
from heartwood.gateway._identity import IdentityDeployment
from heartwood.gateway._mcp_policy import McpDestination, mcp_destinations_from_mapping
from heartwood.gateway._model_catalog import (
    ModelCatalogError,
    ModelConnection,
    model_connections_from_mapping,
)
from heartwood.gateway._platform_planning import PlanningDeployment
from heartwood.gateway._project import ProjectContext, ProjectStateError
from heartwood.gateway._project_config import ProjectConfig, ProjectConfigStore
from heartwood.gateway._readiness import (
    MODEL_SOURCE_OPTIONS,
    DeploymentControl,
    DeploymentReadiness,
    EffectivePolicy,
    inspect_deployment,
    platform_policy_ceiling,
)
from heartwood.gateway.checkpoint_deployment import (
    CheckpointEvidenceDeployment,
    validate_evidence_deployment,
)
from heartwood.gateway.worker_deployment import (
    OpenShellWorkerDeployment,
    WorkerDeployment,
    worker_deployment_from_mapping,
)
from heartwood.model_policy import (
    PolicyInputError,
    inherit_absent_fields,
    validate_policy_restriction,
)
from heartwood.persistence import read_private_text
from heartwood.schemas import PlatformLaunchContext, PlatformModelMetadataManifest, PolicyProfile

_SCHEMA = "heartwood.project-deployment.v1"
_FIELDS = {"schema_version", "project_id", "workspace", "state_root", "policy"}
_OPTIONAL_FIELDS = {
    "worker",
    "evidence",
    "requirements",
    "model_connections",
    "model_metadata",
    "mcp_destinations",
    "planning",
    "identity",
}


class DeploymentError(ProjectStateError):
    """A refused operator deployment, reported with its stable diagnostic code."""

    def __init__(self, check_id: str, message: str) -> None:
        self.code = diagnostic_for(check_id).code
        super().__init__(f"{self.code}: {message}")


class DeploymentRequirements(BaseModel):
    """Controls the operator requires before any project in the deployment starts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # A separately mediated executor: the complete agent worker runs in the sandbox.
    isolated_worker: bool = False
    checkpoint_evidence: bool = False


@dataclass(frozen=True, slots=True)
class ProjectDeployment:
    """One trusted launch binding, never derived from research-project content."""

    project: ProjectContext
    policy: PolicyProfile
    worker: WorkerDeployment | None = None
    evidence: CheckpointEvidenceDeployment | None = None
    requirements: DeploymentRequirements = field(default_factory=DeploymentRequirements)
    # Platform connections, with any model declarations, that the manifest supplies.
    model_connections: tuple[ModelConnection, ...] = ()
    # Origins the gateway may connect to as an MCP client; empty refuses every connection.
    mcp_destinations: tuple[McpDestination, ...] = ()
    # The OpenID Connect client and the one researcher an identity-proxy gateway accepts.
    identity: IdentityDeployment | None = None
    # The planning service and the launch context the platform issued for this gateway.
    planning: PlanningDeployment | None = None
    platform_launch: PlatformLaunchContext | None = None
    manifest: Path | None = None
    manifest_sha256: str | None = None

    def controls(self) -> dict[str, DeploymentControl]:
        """Report each operator requirement as required, configured or not configured."""
        configured = {
            "isolated_worker": self.worker is not None,
            "checkpoint_evidence": self.evidence is not None,
        }
        return {
            name: "required"
            if getattr(self.requirements, name)
            else "configured"
            if configured[name]
            else "not-configured"
            for name in DeploymentRequirements.model_fields
        }


def inspect_project_deployment(
    deployment: ProjectDeployment, env: Mapping[str, str] | None = None
) -> DeploymentReadiness:
    """Inspect a deployment's project when its stored configuration stops the gateway.

    The manifest ceiling bounds the configuration store exactly as in the gateway, so a
    stored selection the ceiling no longer permits is reported instead of loaded.
    """
    adapter = select_platform_adapter(dict(os.environ if env is None else env))
    store = ProjectConfigStore(
        deployment.project,
        ProjectConfig(platform_id=adapter.adapter_id, policy=deployment.policy),
        deployment_policy=deployment.policy,
    )
    policy = EffectivePolicy(
        "deployment",
        deployment.policy,
        manifest=None if deployment.manifest is None else str(deployment.manifest),
        manifest_sha256=deployment.manifest_sha256,
        **deployment.controls(),
    )
    return inspect_deployment(
        deployment.project,
        env,
        policy=policy,
        config_store=store,
        deployment_connections=deployment.model_connections,
    )


def load_project_deployment(
    path: Path, *, project_root: Path, env: Mapping[str, str] | None = None
) -> ProjectDeployment:
    """Read a bounded operator manifest and reject missing or conflicting authority."""
    return _read_project_deployment(path, project_root=project_root, validate_state=True, env=env)


def read_manifest_sha256(path: Path) -> str:
    """Return the digest of the manifest bytes a trusted launch would read."""
    try:
        return hashlib.sha256(read_private_text(path, max_bytes=64 * 1024).encode()).hexdigest()
    except (OSError, ValueError) as error:
        raise DeploymentError(
            "deployment-manifest", "deployment manifest is unavailable"
        ) from error


def _read_project_deployment(
    path: Path,
    *,
    project_root: Path,
    validate_state: bool,
    env: Mapping[str, str] | None = None,
) -> ProjectDeployment:
    """Validate operator authority independently of state during explicit migration."""
    try:
        deployment = _read_manifest(path, project_root=project_root, env=env)
    except DeploymentError:
        raise
    except ProjectStateError as error:
        raise DeploymentError("deployment-manifest", str(error)) from error
    except (OSError, ValueError, TypeError, AttributeError) as error:
        raise DeploymentError(
            "deployment-manifest", "deployment manifest is unavailable or invalid"
        ) from error
    # Fail before opening gateway stores, including when legacy state is present.
    if validate_state:
        deployment.project.state_exists()
    return deployment


def _read_manifest(
    path: Path, *, project_root: Path, env: Mapping[str, str] | None
) -> ProjectDeployment:
    workspace = project_root.resolve(strict=True)
    if not path.is_absolute() or path.is_symlink():
        raise ProjectStateError("deployment manifest must be an absolute non-symlink path")
    resolved = path.resolve()
    if resolved != path:
        raise ProjectStateError("deployment manifest must not traverse symbolic links")
    if resolved == workspace or workspace in resolved.parents:
        raise ProjectStateError("deployment manifest must be outside the research directory")
    _validate_trusted_parents(resolved.parent)
    metadata = resolved.stat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or metadata.st_uid not in {0, os.geteuid()}
    ):
        raise ProjectStateError("deployment manifest must be operator-owned and write-protected")
    content = read_private_text(resolved, max_bytes=64 * 1024)
    record = tomllib.loads(content)
    if (
        not set(record) >= _FIELDS
        or set(record) - _FIELDS - _OPTIONAL_FIELDS
        or record["schema_version"] != _SCHEMA
        or not isinstance(record["policy"], dict)
    ):
        raise ValueError("invalid deployment schema")
    identity = UUID(record["project_id"])
    if str(identity) != record["project_id"]:
        raise ValueError("noncanonical project identity")
    bound_workspace = Path(record["workspace"])
    state_root = Path(record["state_root"])
    if bound_workspace != workspace:
        raise ProjectStateError("deployment manifest does not authorize this research directory")
    project = ProjectContext(workspace, protected_state=state_root, project_id=identity)
    _validate_trusted_parents(project.state_root.parent)
    if resolved == project.state_root or project.state_root in resolved.parents:
        raise ProjectStateError("deployment manifest must remain separate from project state")
    requirements = DeploymentRequirements.model_validate(record.get("requirements", {}))
    worker = worker_deployment_from_mapping(record["worker"]) if "worker" in record else None
    evidence = (
        CheckpointEvidenceDeployment.model_validate(record["evidence"])
        if "evidence" in record
        else None
    )
    policy = _platform_restricted_policy(
        record["policy"], isolated_worker=worker is not None, env=env
    )
    if requirements.isolated_worker and worker is None:
        raise DeploymentError(
            "deployment-requirement", "the deployment requires an isolated worker"
        )
    if requirements.checkpoint_evidence and (evidence is None or evidence.mode != "required"):
        raise DeploymentError(
            "deployment-requirement", "the deployment requires required-mode checkpoint evidence"
        )
    signers = policy.allowed_checkpoint_signer_profiles
    if evidence is not None and signers is not None and evidence.signer_profile not in signers:
        raise DeploymentError(
            "deployment-policy", "checkpoint evidence uses a signer profile the policy excludes"
        )
    if worker is not None:
        validate_worker_deployment(worker, project)
    if evidence is not None:
        if worker is None:
            raise ProjectStateError(
                "managed checkpoint evidence requires complete worker isolation"
            )
        validate_evidence_deployment(evidence, project)
    mcp_destinations: tuple[McpDestination, ...] = ()
    if "mcp_destinations" in record:
        try:
            mcp_destinations = mcp_destinations_from_mapping(record["mcp_destinations"])
        except ValueError as error:
            raise DeploymentError("deployment-manifest", str(error)) from None
        for destination in mcp_destinations:
            if destination.ca_file is not None:
                validate_operator_file(destination.ca_file, project)
                try:
                    ssl.create_default_context(cafile=str(destination.ca_file))
                except (OSError, ValueError):
                    raise DeploymentError(
                        "deployment-manifest",
                        f"the certificate authority file of {destination.service_id} is unusable",
                    ) from None
        if "identity" not in record and any(item.token_exchange for item in mcp_destinations):
            raise DeploymentError(
                "deployment-manifest", "an MCP destination's token exchange requires [identity]"
            )
        # Research services run only through outside review, which needs the worker boundary.
        if worker is None:
            raise DeploymentError(
                "deployment-manifest", "MCP destinations require complete worker isolation"
            )
    planning: PlanningDeployment | None = None
    platform_launch: PlatformLaunchContext | None = None
    if "planning" in record:
        if "identity" not in record:
            raise DeploymentError("deployment-manifest", "the planning service requires [identity]")
        planning, platform_launch = _planning(record["planning"], project)
    connections = _model_connections(record.get("model_connections"), record.get("model_metadata"))
    for connection in connections:
        if connection.ca_file is not None:
            validate_operator_file(Path(connection.ca_file), project)
            try:
                ssl.create_default_context(cafile=connection.ca_file)
            except (OSError, ValueError):
                raise DeploymentError(
                    "deployment-manifest",
                    f"model connection {connection.connection_id} has an unusable ca_file",
                ) from None
    return ProjectDeployment(
        project=project,
        policy=policy,
        identity=_identity(record["identity"], project) if "identity" in record else None,
        worker=worker,
        evidence=evidence,
        requirements=requirements,
        model_connections=connections,
        mcp_destinations=mcp_destinations,
        planning=planning,
        platform_launch=platform_launch,
        manifest=resolved,
        manifest_sha256=hashlib.sha256(content.encode()).hexdigest(),
    )


def _identity(table: object, project: ProjectContext) -> IdentityDeployment:
    """Load the identity table and check the operator files it names."""
    try:
        identity = IdentityDeployment.model_validate(table)
    except ValidationError as error:
        details = "; ".join(
            f"{'.'.join(str(part) for part in item['loc']) or 'identity'}: {item['msg']}"
            for item in error.errors(include_input=False, include_url=False)
        )
        raise DeploymentError("deployment-manifest", f"invalid identity table: {details}") from None
    for path, name in (
        (identity.client_secret_file, "client secret"),
        (identity.actor_key_file, "actor key"),
    ):
        validate_operator_file(path, project)
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise ProjectStateError(f"identity {name} requires owner-only access")
    if identity.ca_file is not None:
        validate_operator_file(identity.ca_file, project)
    return identity


def _planning(
    table: object, project: ProjectContext
) -> tuple[PlanningDeployment, PlatformLaunchContext]:
    """Load the planning table, its certificate authorities and its launch context."""
    try:
        planning = PlanningDeployment.model_validate(table)
    except ValidationError as error:
        details = "; ".join(
            f"{'.'.join(str(part) for part in item['loc']) or 'planning'}: {item['msg']}"
            for item in error.errors(include_input=False, include_url=False)
        )
        raise DeploymentError("deployment-manifest", f"invalid planning table: {details}") from None
    validate_operator_file(planning.launch_context_file, project)
    if planning.ca_file is not None:
        validate_operator_file(planning.ca_file, project)
        try:
            ssl.create_default_context(cafile=str(planning.ca_file))
        except (OSError, ValueError):
            raise DeploymentError(
                "deployment-manifest", "the planning certificate authority file is unusable"
            ) from None
    try:
        launch = PlatformLaunchContext.model_validate_json(
            read_private_text(planning.launch_context_file, max_bytes=64 * 1024)
        )
    except (OSError, ValueError):
        raise DeploymentError(
            "deployment-manifest", "the platform launch context file is unusable"
        ) from None
    return planning, launch


def _model_connections(connections: object, metadata: object) -> tuple[ModelConnection, ...]:
    """Load the manifest's platform connections and attach each model declaration."""
    if connections is None:
        if metadata is not None:
            raise DeploymentError(
                "deployment-manifest", "model metadata requires model connections"
            )
        return ()
    try:
        loaded = tuple(
            connection
            for connection in model_connections_from_mapping(connections)
            if connection.source == "platform"
        )
        # The schema is a JSON document; TOML dates and times become ISO 8601 strings.
        declared = (
            PlatformModelMetadataManifest.model_validate_json(
                json.dumps(metadata, default=lambda value: value.isoformat())
            ).models
            if metadata is not None
            else ()
        )
    except ModelCatalogError as error:
        raise DeploymentError("deployment-manifest", str(error)) from None
    except ValidationError as error:
        details = "; ".join(
            f"{'.'.join(str(part) for part in item['loc']) or 'manifest'}: {item['msg']}"
            for item in error.errors(include_input=False, include_url=False)
        )
        raise DeploymentError("deployment-manifest", f"invalid model metadata: {details}") from None
    identities = {connection.connection_id for connection in loaded}
    # A platform connection identifier becomes the project's model source when selected.
    reserved = {
        identity
        for option in MODEL_SOURCE_OPTIONS
        for identity in (option.source_id, option.connection_id)
    }
    if invalid := sorted(
        identity
        for identity in identities
        if identity in reserved or re.fullmatch(r"[a-z0-9][a-z0-9_-]*", identity) is None
    ):
        raise DeploymentError(
            "deployment-manifest",
            "model connection identifiers must be lowercase and unused by Heartwood's model "
            f"sources: {', '.join(invalid)}",
        )
    declared_models = [
        (item.connection_id, item.model_id.removeprefix(connection.model_prefix))
        for connection in loaded
        for item in declared
        if item.connection_id == connection.connection_id
    ]
    if len(declared_models) != len(set(declared_models)):
        raise DeploymentError(
            "deployment-manifest", "model metadata declares one model more than once"
        )
    if unknown := sorted({item.connection_id for item in declared} - identities):
        raise DeploymentError(
            "deployment-manifest",
            f"model metadata names undeclared connections: {', '.join(unknown)}",
        )
    return tuple(
        replace(
            connection,
            model_metadata=tuple(
                item for item in declared if item.connection_id == connection.connection_id
            ),
        )
        for connection in loaded
    )


def _platform_restricted_policy(
    value: dict[str, object], *, isolated_worker: bool, env: Mapping[str, str] | None
) -> PolicyProfile:
    """Accept the manifest policy only as a restriction of the detected platform's policy."""
    adapter = select_platform_adapter(os.environ if env is None else env)
    requested = PolicyProfile.model_validate(value)
    ceiling = platform_policy_ceiling(adapter, requested)
    if isolated_worker:
        # The worker boundary, not the platform adapter, is what makes this mode applicable.
        ceiling = ceiling.model_copy(
            update={
                "allowed_action_review_modes": (
                    *ceiling.allowed_action_review_modes,
                    "review-outside-effects",
                )
            }
        )
    # An omitted field that would permit anything follows the platform instead.
    policy = inherit_absent_fields(requested, ceiling, value)
    try:
        validate_policy_restriction(policy, ceiling)
    except PolicyInputError as error:
        raise DeploymentError(
            "deployment-policy",
            f"the manifest policy widens the {adapter.adapter_id} platform policy "
            + (f"in {error.field}" if error.field else f"({error})"),
        ) from error
    return policy


def _validate_trusted_parents(directory: Path) -> None:
    for parent in (directory, *directory.parents):
        metadata = parent.stat()
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid not in {0, os.geteuid()}:
            raise ProjectStateError("deployment paths require operator-owned parent directories")
        writable = metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        system_temporary_root = metadata.st_uid == 0 and metadata.st_mode & stat.S_ISVTX
        if writable and not system_temporary_root:
            raise ProjectStateError("deployment paths have a writable parent directory")


def validate_worker_deployment(worker: WorkerDeployment, project: ProjectContext) -> None:
    """Reject workspace authority and changed policy before native clients are constructed."""
    if isinstance(worker, OpenShellWorkerDeployment):
        driver_cli = worker.docker_cli
    else:
        driver_cli = worker.kubectl_cli
    for path in (worker.openshell_cli, driver_cli, worker.policy_path):
        validate_operator_file(path, project)
    for binary in (worker.openshell_cli, driver_cli):
        if not os.access(binary, os.X_OK):
            raise ProjectStateError("native worker tools must be executable")
    if isinstance(worker, OpenShellWorkerDeployment):
        _validate_docker_socket(worker, project)
    else:
        # The cluster credential may be embedded in this file or referenced from it.
        validate_operator_file(worker.kubeconfig, project)
        if stat.S_IMODE(worker.kubeconfig.stat().st_mode) & 0o077:
            raise ProjectStateError("Kubernetes client configuration requires owner-only access")
    home = worker.native_config_home
    _validate_operator_location(home, project)
    _validate_trusted_parents(home)
    gateway = home / "openshell" / "gateways" / worker.gateway
    for name in ("metadata.json", "mtls/ca.crt", "mtls/tls.crt", "mtls/tls.key"):
        path = gateway / name
        validate_operator_file(path, project)
        if name.endswith(".key") and stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise ProjectStateError("native gateway keys require owner-only access")
    content = read_private_text(worker.policy_path, max_bytes=64 * 1024)
    if hashlib.sha256(content.encode()).hexdigest() != worker.policy_file_sha256:
        raise ProjectStateError("native worker policy changed after qualification")


def _validate_docker_socket(worker: OpenShellWorkerDeployment, project: ProjectContext) -> None:
    socket_path = Path(urlsplit(worker.docker_host).path)
    _validate_operator_location(socket_path, project)
    _validate_trusted_parents(socket_path.parent)
    socket_metadata = socket_path.stat()
    if (
        not stat.S_ISSOCK(socket_metadata.st_mode)
        or socket_metadata.st_uid not in {0, os.geteuid()}
        or socket_metadata.st_mode & stat.S_IWOTH
        or (
            worker.docker_socket_group is not None
            and socket_metadata.st_gid != worker.docker_socket_group
        )
        or (socket_metadata.st_mode & stat.S_IWGRP and worker.docker_socket_group is None)
    ):
        raise ProjectStateError("native Docker access requires an operator-owned local socket")


def _validate_operator_location(path: Path, project: ProjectContext) -> None:
    if (
        not path.is_absolute()
        or path.resolve(strict=True) != path
        or path.is_relative_to(project.root)
        or path.is_relative_to(project.state_root)
    ):
        raise ProjectStateError("deployment authority must be outside project files and state")


def validate_operator_file(path: Path, project: ProjectContext) -> None:
    """Validate an operator control file without disclosing its contents."""
    _validate_operator_location(path, project)
    _validate_trusted_parents(path.parent)
    metadata = path.stat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or metadata.st_uid not in {0, os.geteuid()}
        or metadata.st_nlink != 1
    ):
        raise ProjectStateError(
            "deployment authority files must be operator-owned and write-protected"
        )
