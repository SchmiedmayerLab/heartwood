# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""The files of a hosted Heartwood gateway, rendered for one session.

The control service writes them into the session's gateway Secret. The init container
of the gateway pod copies them to the paths below, so the manifest names fixed paths:

- /etc/heartwood/deployment: the deployment manifest, the project client's secret, the
  platform CA certificate, the kubeconfig, the worker policy and the launch context;
- /etc/heartwood/native: the registration of the session's OpenShell gateway;
- /research and /var/lib/heartwood: the research and state directories on the server's
  volume.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from heartwood.schemas import PlatformLaunchContext
from heartwood_platform.control.inference import RouteModel
from heartwood_platform.control.project_data import ProjectDataVolume

_DEPLOYMENT = "/etc/heartwood/deployment"
# The OpenShell workspace of the session's workers, in which its provider lives and to
# whose sandboxes project data volumes are attached.
WORKER_WORKSPACE = "research"
# The model connection of the route, whose identifier is the worker's profile.
_MODELS_PATH = "/chat/completions"
_PENDING_DIGEST = "0" * 64
# Session gateways derive their Heartwood project identity from the session in this
# namespace, so a restarted gateway keeps its project state.
_PROJECT_NAMESPACE = uuid.UUID("1f9b6c2e-6d35-4f1e-9a52-4e0b8a7c3d21")
_DIGEST = re.compile(r"/(supervisor|sandbox)@(sha256:[0-9a-f]{64})")

Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
ImageReference = Annotated[str, StringConstraints(pattern=r"^[^@\s]+@sha256:[0-9a-f]{64}$")]


class _Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class HostedWorkerSettings(_Settings):
    """The worker that a hosted gateway launches in its session namespace."""

    image: ImageReference
    # The sandbox policy of the workers.
    policy: dict[str, Any]
    cpu: str = Field(pattern=r"^[1-9][0-9]*m?$")
    memory: str = Field(pattern=r"^[1-9][0-9]*(Mi|Gi)$")


class DataHandlingSettings(_Settings):
    """The declared recipient and retention authority of the route."""

    destination: str = Field(
        pattern=r"^(same-compute|institution-managed|provider|operator-defined)$"
    )
    retention_authority: str = Field(
        pattern=r"^(deployment|institution|provider|service-operator)$"
    )
    route: str = Field(min_length=1, max_length=240, pattern=r"\S")


class ModelRouteSettings(_Settings):
    """The model route that the manifest names for the worker and for model discovery."""

    # The model connection's identifier and the worker's model profile.
    profile_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    # The model of the worker: a model name of the route's LiteLLM proxy.
    model: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    # The proxy's chat completions endpoint.
    endpoint: str = Field(pattern=r"^https://[^\s?#]+/chat/completions$")
    provider: str = Field(pattern=r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
    placeholder_env: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    # Programs in the worker image that may send the route's key.
    binaries: tuple[Annotated[str, StringConstraints(pattern=r"^/\S+$")], ...] = Field(min_length=1)
    data_handling: DataHandlingSettings


class HostedGatewaySettings(_Settings):
    """How the control service configures the hosted gateway of each session."""

    # The control service's own HTTPS URL, which gateways call for planning.
    control_url: str = Field(pattern=r"^https://[^/\s]+$")
    # The data service's HTTPS origin, whose catalog tools researchers may enable.
    data_url: str | None = Field(default=None, pattern=r"^https://[^/\s]+$")
    worker: HostedWorkerSettings
    model_route: ModelRouteSettings


def sandbox_digests(openshell_objects: str) -> tuple[str, str]:
    """Return the supervisor and sandbox runtime image digests of the OpenShell objects."""
    found = dict(_DIGEST.findall(openshell_objects))
    return found["supervisor"], found["sandbox"]


def _toml(value: object) -> str:
    # JSON strings, numbers, booleans and arrays of them are valid TOML values. Characters
    # outside ASCII stay literal, because TOML refuses the surrogate pairs of JSON escapes;
    # JSON escapes the other control characters, and TOML needs DEL escaped too.
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


def _table(name: str, values: dict[str, object]) -> str:
    lines = [f"[{name}]"] if name else []
    lines += [f"{key} = {_toml(item)}" for key, item in values.items()]
    return "\n".join(lines) + "\n"


def _catalog_destination(origin: str) -> str:
    """The data catalog as a research MCP destination, reached with an exchanged token."""
    destination: dict[str, object] = {
        "service_id": "heartwood-data",
        "origin": origin,
        "tools": ["list_datasets", "resolve_object"],
        "observation_tool": "get_operation",
        "private_address": True,
        "ca_file": f"{_DEPLOYMENT}/platform-ca.crt",
    }
    exchange: dict[str, object] = {"scope": "svc-data", "audience": "heartwood-data"}
    return (
        "[[mcp_destinations]]\n"
        + _table("", destination)
        + _table("mcp_destinations.token_exchange", exchange)
    )


class HostedGateway:
    """Render the files of the hosted gateway of one session."""

    def __init__(
        self,
        settings: HostedGatewaySettings,
        *,
        issuer: str,
        supervisor: str,
        sandbox: str,
        models: tuple[RouteModel, ...],
    ) -> None:
        """Configure gateways for ``issuer`` and the OpenShell images of the sessions.

        ``models`` are the models of the route, which the manifest declares.
        """
        self._settings = settings
        self._models = models
        self._issuer = issuer
        self._supervisor = supervisor
        self._sandbox = sandbox

    def _connections(self, base_url: str, catalog: str, discovery_key: str) -> str:
        """Return the model connection of the route and the declarations of its models.

        The gateway discovers the models with the session's discovery key, which may only
        list them, and trusts the platform CA alone for it.
        """
        route = self._settings.model_route
        handling = route.data_handling
        connection: dict[str, object] = {
            "connection_id": route.profile_id,
            "label": "Platform models",
            "protocol": "openai-compatible",
            "model_prefix": "openai/",
            "credential_kind": "file",
            "api_key_file": discovery_key,
            "ca_file": f"{_DEPLOYMENT}/platform-ca.crt",
            "base_url": base_url,
            "catalog_endpoint": catalog,
            "policy_endpoint": route.endpoint,
        }
        sections = [
            _table("model_connections", {"schema_version": "heartwood.model-connections.v1"}),
            "[[model_connections.connections]]\n" + _table("", connection),
            _table(
                "model_connections.connections.data_handling",
                {
                    "destination": handling.destination,
                    "retention_authority": handling.retention_authority,
                    "route": handling.route,
                },
            ),
            _table("model_metadata", {"schema_version": "heartwood.platform-model-metadata.v1"}),
        ]
        for model in self._models:
            sections.append(
                "[[model_metadata.models]]\n"
                + _table(
                    "",
                    {
                        "connection_id": route.profile_id,
                        "model_id": model.name,
                        "protocols": ["chat"],
                        "context_window_tokens": model.max_input_tokens + model.max_output_tokens,
                        "max_input_tokens": model.max_input_tokens,
                        "max_output_tokens": model.max_output_tokens,
                        "supports_tool_calls": model.supports_tool_calls,
                    },
                )
            )
        return "\n".join(sections)

    def files(
        self,
        *,
        session: str,
        subject: str,
        client_id: str,
        namespace: str,
        launch_context: PlatformLaunchContext,
        project_data: ProjectDataVolume | None = None,
    ) -> dict[str, str]:
        """Return the deployment manifest and the files it names, by Secret key."""
        settings = self._settings
        worker, route = settings.worker, settings.model_route
        project_id = str(uuid.uuid5(_PROJECT_NAMESPACE, session))
        base_url = route.endpoint.removesuffix(_MODELS_PATH)
        catalog = f"{base_url}/models"
        discovery_key = f"{_DEPLOYMENT}/model-discovery-key"
        policy = json.dumps(worker.policy, sort_keys=True, separators=(",", ":"))
        repository, _, digest = worker.image.partition("@")
        # Pods reference the worker by digest alone, so a tag in the reference is dropped.
        path, slash, name = repository.rpartition("/")
        repository = f"{path}{slash}{name.partition(':')[0]}"
        manifest = "\n".join(
            [
                _table(
                    "",
                    {
                        "schema_version": "heartwood.project-deployment.v1",
                        "project_id": project_id,
                        "workspace": "/research",
                        "state_root": f"/var/lib/heartwood/{project_id}",
                    },
                ),
                _table(
                    "policy",
                    {
                        "policy_id": "heartwood-reference-platform",
                        "platform_id": "generic",
                        "deny_egress_by_default": True,
                        "allowed_model_endpoints": [route.endpoint],
                        "allowed_model_catalog_endpoints": [catalog],
                        "allowed_capability_tiers": ["supervised"],
                        "allowed_action_review_modes": ["review-all"],
                        "credential_allowlist": [discovery_key],
                        "allowed_data_destinations": [route.data_handling.destination],
                    },
                ),
                self._connections(base_url, catalog, discovery_key),
                _table(
                    "identity",
                    {
                        "issuer": self._issuer,
                        "client_id": client_id,
                        "client_secret_file": f"{_DEPLOYMENT}/client-secret",
                        "actor_key_file": f"{_DEPLOYMENT}/actor-key",
                        "subject": subject,
                        "ca_file": f"{_DEPLOYMENT}/platform-ca.crt",
                    },
                ),
                _table(
                    "worker",
                    {
                        "driver": "openshell-kubernetes",
                        "gateway": "openshell",
                        "native_config_home": "/etc/heartwood/native",
                        "openshell_cli": "/usr/local/bin/openshell",
                        "kubectl_cli": "/usr/local/bin/kubectl",
                        "kubeconfig": f"{_DEPLOYMENT}/kubeconfig",
                        "namespace": namespace,
                        "image_repository": repository,
                        "image_digest": digest,
                        "supervisor_image_digest": self._supervisor,
                        "sandbox_image_digest": self._sandbox,
                        "workspace": WORKER_WORKSPACE,
                        # The init container of the gateway pod replaces it with the
                        # digest that the session's OpenShell gateway admits the policy with.
                        "sandbox_policy_sha256": _PENDING_DIGEST,
                        "policy_path": f"{_DEPLOYMENT}/worker-policy.json",
                        "policy_file_sha256": hashlib.sha256(policy.encode()).hexdigest(),
                        "profile_id": route.profile_id,
                        "model": f"openai/{route.model}",
                        "model_endpoint": route.endpoint,
                        "provider": route.provider,
                        "placeholder_env": route.placeholder_env,
                        "cpu": worker.cpu,
                        "memory": worker.memory,
                    },
                ),
                _table(
                    "worker.inputs",
                    {"schema_version": "heartwood.worker-inputs.v1", "files": []},
                ),
                # The session's read-only copy of the project's data, which admission checks.
                ""
                if project_data is None
                else _table(
                    "worker.project_data",
                    {
                        "claim_name": project_data.claim_name,
                        "claim_uid": project_data.claim_uid,
                        "project": launch_context.project_id,
                        "content_sha256": project_data.content_sha256,
                        "mount_path": project_data.mount_path,
                    },
                ),
                _table(
                    "planning",
                    {
                        "url": f"{settings.control_url}/mcp",
                        "ca_file": f"{_DEPLOYMENT}/platform-ca.crt",
                        "private_address": True,
                        "launch_context_file": f"{_DEPLOYMENT}/launch-context.json",
                    },
                ),
                _table(
                    "planning.token_exchange",
                    {"scope": "svc-control", "audience": "heartwood-control"},
                ),
                "" if settings.data_url is None else _catalog_destination(settings.data_url),
            ]
        )
        # The token file and the cluster CA come from the pod's projected ServiceAccount
        # volume, which kubelet keeps current.
        account = "/var/run/secrets/kubernetes.io/serviceaccount"
        kubeconfig = {
            "apiVersion": "v1",
            "kind": "Config",
            "clusters": [
                {
                    "name": "cluster",
                    "cluster": {
                        "server": "https://kubernetes.default.svc",
                        "certificate-authority": f"{account}/ca.crt",
                    },
                }
            ],
            "users": [{"name": "gateway", "user": {"tokenFile": f"{account}/token"}}],
            "contexts": [
                {
                    "name": "session",
                    "context": {"cluster": "cluster", "user": "gateway", "namespace": namespace},
                }
            ],
            "current-context": "session",
        }
        return {
            "deployment.toml": manifest,
            "kubeconfig": json.dumps(kubeconfig, indent=2) + "\n",
            "worker-policy.json": policy,
            "launch-context.json": launch_context.model_dump_json(),
        }
