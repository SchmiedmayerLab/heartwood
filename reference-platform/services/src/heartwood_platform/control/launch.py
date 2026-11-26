# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""The launch flow: from an authorized researcher and project to a launch context."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from heartwood.schemas import PlatformLaunchContext
from heartwood_platform.control.keycloak import project_client
from heartwood_platform.control.registry import Project, Registry, Session
from heartwood_platform.control.sessions import ProvisionedSession


class ProjectClients(Protocol):
    """The identity provider operations that a launch needs."""

    def ensure_project_client(self, project: str) -> None:
        """Create the client of a project unless it exists."""

    def add_redirect_uri(self, project: str, uri: str) -> None:
        """Register a redirect URI with the client of a project."""

    def remove_redirect_uri(self, project: str, uri: str) -> None:
        """Remove a redirect URI from the client of a project."""

    def client_secret(self, project: str) -> str:
        """Return the secret of the client of a project."""


class SessionNamespaces(Protocol):
    """The cluster operations that a launch needs."""

    def provision(self, session: str) -> ProvisionedSession:
        """Bring the namespace of a session to its full state."""

    def write_gateway_secret(self, session: str, files: dict[str, str]) -> None:
        """Replace the configuration Secret of a session's Heartwood gateway."""

    def remove(self, session: str) -> None:
        """Delete everything of a session."""


class Launch(BaseModel):
    """What the spawner needs to start the gateway of one researcher and project."""

    model_config = ConfigDict(frozen=True)

    session: str
    subject: str
    project: str
    client_id: str
    redirect_uri: str
    launch_context: PlatformLaunchContext
    namespace: str
    namespace_uid: str
    # Identifies the cluster in the infrastructure identity of the gateway.
    kube_system_uid: str
    gateway_service_account: str
    openshell_endpoint: str


class NoSessionError(Exception):
    """The researcher has not launched a session in the project."""


class LaunchService:
    """Authorizes launches against the registry and prepares the project client."""

    def __init__(
        self,
        registry: Registry,
        clients: ProjectClients,
        sessions: SessionNamespaces,
        platform_id: str,
        redirect_uri_template: str,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        """Launch sessions of one platform."""
        self._registry = registry
        self._clients = clients
        self._sessions = sessions
        self._platform_id = platform_id
        self._redirect_uri_template = redirect_uri_template
        self._clock = clock
        # A launch waits for the session's gateway. A deletion that ran meanwhile
        # would leave what the launch writes afterwards, so both hold one lock for
        # each researcher and project.
        self._locks: dict[tuple[str, str], threading.Lock] = {}
        self._locks_guard = threading.Lock()

    @contextmanager
    def _serialized(self, subject: str, project: str) -> Iterator[None]:
        with self._locks_guard:
            lock = self._locks.setdefault((subject, project), threading.Lock())
        with lock:
            yield

    def projects(self, subject: str) -> list[str]:
        """Return the identifiers of the projects that a subject may launch."""
        return [project.id for project in self._registry.projects_of(subject)]

    def _redirect_uri(self, session: Session) -> str:
        return self._redirect_uri_template.format(subject=session.subject, project=session.project)

    def _context(self, project: Project, session: Session) -> PlatformLaunchContext:
        # The billing binding comes from the registry and never from the caller.
        return PlatformLaunchContext(
            project_context_id=session.id,
            platform_id=self._platform_id,
            project_id=project.id,
            billing_attribution_ref=project.billing_attribution_ref,
            authority_ref=self._platform_id,
            policy_ref=project.policy_ref,
            issued_at=self._clock(),
        )

    def prepare(self, subject: str, project: str) -> Launch:
        """Prepare the session of a member, creating it at the first launch.

        Every step can be repeated, so a launch that failed midway is completed by the
        next one.
        """
        found = self._registry.project(subject, project)
        with self._serialized(subject, project):
            return self._prepare(found, subject, project)

    def _prepare(self, found: Project, subject: str, project: str) -> Launch:
        session = self._registry.session(subject, project, create=True)
        assert session is not None
        provisioned = self._sessions.provision(session.id)
        redirect_uri = self._redirect_uri(session)
        self._clients.ensure_project_client(project)
        self._clients.add_redirect_uri(project, redirect_uri)
        launch = Launch(
            session=session.id,
            subject=subject,
            project=project,
            client_id=project_client(project),
            redirect_uri=redirect_uri,
            launch_context=self._context(found, session),
            namespace=provisioned.namespace,
            namespace_uid=provisioned.namespace_uid,
            kube_system_uid=provisioned.kube_system_uid,
            gateway_service_account=provisioned.gateway_account,
            openshell_endpoint=provisioned.openshell_endpoint,
        )
        # The gateway pod mounts this Secret. Only it carries credentials; the reply
        # to the spawner carries none.
        self._sessions.write_gateway_secret(
            session.id,
            {
                "launch.json": launch.model_dump_json(),
                "keycloak-client-secret": self._clients.client_secret(project),
                **{
                    f"openshell-{name}": value
                    for name, value in provisioned.openshell_client.items()
                },
            },
        )
        return launch

    def context(self, subject: str, project: str) -> PlatformLaunchContext:
        """Return the launch context of a member's existing session."""
        found = self._registry.project(subject, project)
        session = self._registry.session(subject, project)
        if session is None:
            raise NoSessionError
        return self._context(found, session)

    def delete(self, subject: str, project: str) -> None:
        """Remove the session of a researcher in a project, whether or not it exists."""
        with self._serialized(subject, project):
            session = self._registry.session(subject, project)
            if session is None:
                return
            self._sessions.remove(session.id)
            self._clients.remove_redirect_uri(project, self._redirect_uri(session))
            self._registry.delete_session(subject, project)
