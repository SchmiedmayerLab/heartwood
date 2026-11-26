# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Project data volumes: a read-only copy of a project's catalog objects for each session.

At each launch the control service asks the data service for the access plan of the
researcher's project: every catalog object with its path, size, checksum and a presigned
URL. A copy job in the session namespace writes the objects into a new volume claim and
verifies them. The claim is named after the digest of its content, so a launch with an
unchanged catalog reuses it. Only a claim whose copy succeeded receives the labels with
which the OpenShell driver attaches it to the sandboxes of the session's workspace.
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from heartwood_platform.common.kubernetes import Kubernetes
from heartwood_platform.control.sessions import SessionError

_SESSION_LABEL = "heartwood.platform/session"
_PROJECT_LABEL = "heartwood.platform/project"
_ROLE_LABEL = "heartwood.platform/role"
_CONTENT = "heartwood.platform/content-sha256"
_VOLUME_ROLE = "project-data"
_COPY_ROLE = "project-data-copy"
# The labels with which the OpenShell driver accepts a claim as a sandbox volume.
_ATTACHABLE = "openshell.ai/sandbox-attachable"
_ATTACHABLE_WORKSPACE = "openshell.ai/sandbox-attachable-workspace"
_COPY_POLICY = "hw-project-data-copy-egress"
# The user of the platform services image.
_COPY_USER = 65532
_COPY_LIMITS = {"cpu": "500m", "memory": "256Mi"}
# Extra room above the summed data size, for directories and file system overhead.
_STORAGE_MARGIN = 64 * 1024 * 1024


class ProjectDataSettings(BaseModel):
    """How the control service fills project data volumes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # The data service's HTTPS URL and the projected token the control service presents.
    data_url: str = Field(pattern=r"^https://[^/\s]+$")
    token_file: Path
    # The platform services image, which runs the copy.
    copy_image: str = Field(pattern=r"^[^@\s]+@sha256:[0-9a-f]{64}$")
    storage_class: str
    # The OpenShell workspace whose sandboxes may attach the volume.
    workspace: str
    # Where workers find the data.
    mount_path: str = Field(pattern=r"^/[a-z0-9][a-z0-9/_-]*$")
    # The namespace and labels of the object store's pods, which copy pods may reach.
    object_store_namespace: str
    object_store_labels: dict[str, str]
    object_store_port: int
    attempts: int = Field(default=3, ge=1, le=10)


class ProjectDataVolume(BaseModel):
    """A filled project data volume of a session."""

    model_config = ConfigDict(frozen=True)

    claim_name: str
    claim_uid: str
    content_sha256: str
    mount_path: str
    objects: int


class _Planned(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    id: str
    path: str
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    url: str
    expires_at: AwareDatetime


class _Plan(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    project: str
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    objects: tuple[_Planned, ...]


def claim_name(content_sha256: str) -> str:
    """Return the name of the claim that holds the content with this digest."""
    return f"hw-project-data-{content_sha256[:16]}"


class ProjectData:
    """Fills the project data volume of a session through a copy job."""

    def __init__(
        self,
        kubernetes: Kubernetes,
        settings: ProjectDataSettings,
        client: httpx.Client,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Copy with the control service's rights; ``client`` reaches the data service."""
        self._kubernetes = kubernetes
        self._settings = settings
        self._client = client
        self._sleep = sleep
        self._clock = clock

    def _plan(self, subject: str, project: str) -> _Plan:
        token = self._settings.token_file.read_text(encoding="utf-8").strip()
        response = self._client.post(
            f"{self._settings.data_url}/v1/volume-plans",
            headers={"Authorization": f"Bearer {token}"},
            json={"subject": subject, "project": project},
        )
        response.raise_for_status()
        try:
            plan = _Plan.model_validate_json(response.content)
        except ValidationError:
            raise SessionError("the data service answered with an invalid plan") from None
        if plan.project != project:
            raise SessionError("the data service planned another project")
        return plan

    def _copy_policy(self, namespace: str) -> dict[str, Any]:
        settings = self._settings
        store = {"kubernetes.io/metadata.name": settings.object_store_namespace}
        return {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": _COPY_POLICY, "namespace": namespace},
            "spec": {
                "podSelector": {"matchLabels": {_ROLE_LABEL: _COPY_ROLE}},
                "policyTypes": ["Egress"],
                "egress": [
                    {
                        "to": [
                            {
                                "namespaceSelector": {
                                    "matchLabels": {"kubernetes.io/metadata.name": "kube-system"}
                                },
                                "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
                            }
                        ],
                        "ports": [
                            {"protocol": "UDP", "port": 53},
                            {"protocol": "TCP", "port": 53},
                        ],
                    },
                    {
                        "to": [
                            {
                                "namespaceSelector": {"matchLabels": store},
                                "podSelector": {"matchLabels": settings.object_store_labels},
                            }
                        ],
                        "ports": [{"protocol": "TCP", "port": settings.object_store_port}],
                    },
                ],
            },
        }

    def _claim(self, namespace: str, session: str, project: str, plan: _Plan) -> dict[str, Any]:
        size = sum(item.size for item in plan.objects) + _STORAGE_MARGIN
        return {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {
                "name": claim_name(plan.content_sha256),
                "namespace": namespace,
                "labels": {
                    _SESSION_LABEL: session,
                    _PROJECT_LABEL: project,
                    _ROLE_LABEL: _VOLUME_ROLE,
                },
                "annotations": {_CONTENT: plan.content_sha256},
            },
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "storageClassName": self._settings.storage_class,
                "resources": {"requests": {"storage": str(size)}},
            },
        }

    def _job(self, namespace: str, session: str, name: str, plan: _Plan) -> dict[str, Any]:
        labels = {_SESSION_LABEL: session, _ROLE_LABEL: _COPY_ROLE}
        return {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": name, "namespace": namespace, "labels": labels},
            "spec": {
                "backoffLimit": 0,
                # A job that outlives a control service restart is removed with its plan,
                # which the job owns.
                "ttlSecondsAfterFinished": 600,
                # The job may run as long as its URLs stay valid, which the data service
                # sized to the objects.
                "activeDeadlineSeconds": self._deadline(plan),
                "template": {
                    "metadata": {"labels": labels},
                    "spec": {
                        "restartPolicy": "Never",
                        "automountServiceAccountToken": False,
                        "enableServiceLinks": False,
                        "securityContext": {
                            "runAsNonRoot": True,
                            "runAsUser": _COPY_USER,
                            "runAsGroup": _COPY_USER,
                            "fsGroup": _COPY_USER,
                            "seccompProfile": {"type": "RuntimeDefault"},
                        },
                        "containers": [
                            {
                                "name": "copy",
                                "image": self._settings.copy_image,
                                "args": [
                                    "heartwood_platform.data.copy",
                                    "/etc/heartwood-copy/plan/plan.json",
                                    "/etc/heartwood-copy/ca/ca.crt",
                                    "/data",
                                ],
                                "securityContext": {
                                    "allowPrivilegeEscalation": False,
                                    "capabilities": {"drop": ["ALL"]},
                                    "readOnlyRootFilesystem": True,
                                },
                                "resources": {"limits": _COPY_LIMITS, "requests": _COPY_LIMITS},
                                "volumeMounts": [
                                    {
                                        "name": "plan",
                                        "mountPath": "/etc/heartwood-copy/plan",
                                        "readOnly": True,
                                    },
                                    {
                                        "name": "ca",
                                        "mountPath": "/etc/heartwood-copy/ca",
                                        "readOnly": True,
                                    },
                                    {"name": "data", "mountPath": "/data"},
                                ],
                            }
                        ],
                        "volumes": [
                            {"name": "plan", "secret": {"secretName": name}},
                            {"name": "ca", "configMap": {"name": "platform-ca"}},
                            {
                                "name": "data",
                                "persistentVolumeClaim": {
                                    "claimName": claim_name(plan.content_sha256)
                                },
                            },
                        ],
                    },
                },
            },
        }

    @staticmethod
    def _deadline(plan: _Plan) -> int:
        """Return the seconds until the plan's last URL expires, at least one minute."""
        now = datetime.now(UTC)
        latest = max((item.expires_at for item in plan.objects), default=now)
        return max(60, int((latest - now).total_seconds()))

    def _remove_copy(self, namespace: str, name: str) -> None:
        """Delete the copy job, its plan and its pods, and wait until none of them is left.

        The job is deleted in the background and its pods are awaited by its label, which
        does not depend on the garbage collector finishing a foreground deletion.
        """
        self._kubernetes.delete("Job", name, namespace, propagation="Background")
        self._kubernetes.delete("Secret", name, namespace)
        deadline = self._clock() + 120
        while self._kubernetes.get("Job", name, namespace) is not None or self._kubernetes.list(
            "Pod", namespace, f"job-name={name}"
        ):
            if self._clock() >= deadline:
                raise SessionError("an earlier project data copy is still being removed")
            self._sleep(1.0)

    def _run_copy(self, namespace: str, session: str, plan: _Plan) -> bool:
        """Run one copy job to its end and return whether it succeeded."""
        name = f"hw-project-data-copy-{plan.content_sha256[:16]}"
        self._remove_copy(namespace, name)
        if not self._kubernetes.create(self._job(namespace, session, name, plan)):
            raise SessionError("a project data copy job exists under its name")
        deadline = self._clock() + self._deadline(plan) + 60
        try:
            self._write_plan(namespace, session, name, plan)
            while True:
                job = self._kubernetes.get("Job", name, namespace) or {}
                conditions = {
                    item.get("type"): item.get("status")
                    for item in job.get("status", {}).get("conditions", [])
                }
                if conditions.get("Complete") == "True":
                    return True
                if conditions.get("Failed") == "True" or self._clock() >= deadline:
                    return False
                self._sleep(2.0)
        finally:
            # The plan's URLs stay readable in the Secret until they expire, so it is
            # removed as soon as the job ends.
            self._remove_copy(namespace, name)

    def _write_plan(self, namespace: str, session: str, name: str, plan: _Plan) -> None:
        """Write the plan into a Secret that the copy job owns.

        The job exists first, so the Secret can name it as its owner, and deleting the job
        deletes the plan too; the job's pod waits until the Secret it mounts exists.
        """
        job = self._kubernetes.get("Job", name, namespace)
        if job is None:
            raise SessionError("the project data copy job disappeared")
        content = json.dumps(plan.model_dump(mode="json"))
        self._kubernetes.create(
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {
                    "name": name,
                    "namespace": namespace,
                    "labels": {_SESSION_LABEL: session, _ROLE_LABEL: _COPY_ROLE},
                    "ownerReferences": [
                        {
                            "apiVersion": "batch/v1",
                            "kind": "Job",
                            "name": name,
                            "uid": job["metadata"]["uid"],
                        }
                    ],
                },
                "type": "Opaque",
                "data": {"plan.json": base64.b64encode(content.encode()).decode()},
            }
        )

    def _complete(self, claim: dict[str, Any], content: str) -> bool:
        metadata = claim.get("metadata", {})
        labels = metadata.get("labels") or {}
        return (
            labels.get(_ATTACHABLE) == "true"
            and labels.get(_ATTACHABLE_WORKSPACE) == self._settings.workspace
            and (metadata.get("annotations") or {}).get(_CONTENT) == content
        )

    def fill(
        self, session: str, namespace: str, subject: str, project: str
    ) -> ProjectDataVolume | None:
        """Bring the session's project data volume to the catalog's current content.

        Returns None when the project has no catalog objects. Each step can be repeated:
        a launch that failed midway is completed by the next one.
        """
        kubernetes = self._kubernetes
        plan = self._plan(subject, project)
        if not plan.objects:
            return None
        name = claim_name(plan.content_sha256)
        kubernetes.apply(self._copy_policy(namespace))
        claim = kubernetes.get("PersistentVolumeClaim", name, namespace)
        if claim is not None and claim["metadata"].get("deletionTimestamp") is not None:
            raise SessionError("the project data volume is being deleted")
        if (
            claim is not None
            and (claim["metadata"].get("annotations") or {}).get(_CONTENT) != plan.content_sha256
        ):
            raise SessionError("a project data volume of other content has the volume's name")
        if claim is None or not self._complete(claim, plan.content_sha256):
            if claim is None:
                kubernetes.create(self._claim(namespace, session, project, plan))
            for attempt in range(self._settings.attempts):
                if attempt:
                    # A failed copy may have outlived its URLs, so each attempt gets new ones.
                    plan = self._plan(subject, project)
                    if claim_name(plan.content_sha256) != name:
                        raise SessionError("the project's catalog changed during the copy")
                if self._run_copy(namespace, session, plan):
                    break
            else:
                raise SessionError("the project data could not be copied")
            kubernetes.apply(
                {
                    "apiVersion": "v1",
                    "kind": "PersistentVolumeClaim",
                    "metadata": {
                        "name": name,
                        "namespace": namespace,
                        "labels": {
                            _ATTACHABLE: "true",
                            _ATTACHABLE_WORKSPACE: self._settings.workspace,
                        },
                    },
                }
            )
            claim = kubernetes.get("PersistentVolumeClaim", name, namespace)
            if claim is None or not self._complete(claim, plan.content_sha256):
                raise SessionError("the project data volume did not keep its labels")
        self._remove_earlier(namespace, session, name)
        return ProjectDataVolume(
            claim_name=name,
            claim_uid=claim["metadata"]["uid"],
            content_sha256=plan.content_sha256,
            mount_path=self._settings.mount_path,
            objects=len(plan.objects),
        )

    def _remove_earlier(self, namespace: str, session: str, current: str) -> None:
        """Delete the session's volumes of earlier content.

        A volume that a running sandbox still mounts stays until that sandbox ends.
        """
        selector = f"{_SESSION_LABEL}={session},{_ROLE_LABEL}={_VOLUME_ROLE}"
        for claim in self._kubernetes.list("PersistentVolumeClaim", namespace, selector):
            found = claim["metadata"]["name"]
            if found != current:
                self._kubernetes.delete("PersistentVolumeClaim", found, namespace)
