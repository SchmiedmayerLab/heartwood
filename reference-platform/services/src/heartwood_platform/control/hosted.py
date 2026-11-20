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

_DEPLOYMENT = "/etc/heartwood/deployment"
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
    # The effective sandbox policy digest and the policy file, from qualification.
    sandbox_policy_sha256: Digest
    policy: dict[str, Any]
    cpu: str = Field(pattern=r"^[1-9][0-9]*m?$")
    memory: str = Field(pattern=r"^[1-9][0-9]*(Mi|Gi)$")


class ModelRouteSettings(_Settings):
    """The model route that the manifest names for the worker."""

    profile_id: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=256)
    endpoint: str = Field(pattern=r"^https://[^\s?#]+$")
    provider: str = Field(pattern=r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
    placeholder_env: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")


class HostedGatewaySettings(_Settings):
    """How the control service configures the hosted gateway of each session."""

    # The control service's own HTTPS URL, which gateways call for planning.
    control_url: str = Field(pattern=r"^https://[^/\s]+$")
    worker: HostedWorkerSettings
    model_route: ModelRouteSettings


def sandbox_digests(openshell_objects: str) -> tuple[str, str]:
    """Return the supervisor and sandbox runtime image digests of the OpenShell objects."""
    found = dict(_DIGEST.findall(openshell_objects))
    return found["supervisor"], found["sandbox"]


def _toml(value: object) -> str:
    # JSON strings, numbers, booleans and arrays of them are valid TOML values.
    return json.dumps(value)


def _table(name: str, values: dict[str, object]) -> str:
    lines = [f"[{name}]"] if name else []
    lines += [f"{key} = {_toml(item)}" for key, item in values.items()]
    return "\n".join(lines) + "\n"


class HostedGateway:
    """Render the files of the hosted gateway of one session."""

    def __init__(
        self, settings: HostedGatewaySettings, *, issuer: str, supervisor: str, sandbox: str
    ) -> None:
        """Configure gateways for ``issuer`` and the OpenShell images of the sessions."""
        self._settings = settings
        self._issuer = issuer
        self._supervisor = supervisor
        self._sandbox = sandbox

    def files(
        self,
        *,
        session: str,
        subject: str,
        client_id: str,
        namespace: str,
        launch_context: PlatformLaunchContext,
    ) -> dict[str, str]:
        """Return the deployment manifest and the files it names, by Secret key."""
        settings = self._settings
        worker, route = settings.worker, settings.model_route
        project_id = str(uuid.uuid5(_PROJECT_NAMESPACE, session))
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
                        "allowed_model_catalog_endpoints": [],
                        "allowed_capability_tiers": ["supervised"],
                        "allowed_action_review_modes": ["review-all"],
                        "credential_allowlist": [],
                    },
                ),
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
                        "workspace": "research",
                        "sandbox_policy_sha256": worker.sandbox_policy_sha256,
                        "policy_path": f"{_DEPLOYMENT}/worker-policy.json",
                        "policy_file_sha256": hashlib.sha256(policy.encode()).hexdigest(),
                        "profile_id": route.profile_id,
                        "model": route.model,
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
