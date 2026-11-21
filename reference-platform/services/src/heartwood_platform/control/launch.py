# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""The launch flow: from an authorized researcher and project to a launch context."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from heartwood.schemas import PlatformLaunchContext
from heartwood_platform.control.keycloak import project_client
from heartwood_platform.control.registry import Project, Registry, Session


class ProjectClients(Protocol):
    """The identity provider operations that a launch needs."""

    def ensure_project_client(self, project: str) -> None:
        """Create the client of a project unless it exists."""

    def add_redirect_uri(self, project: str, uri: str) -> None:
        """Register a redirect URI with the client of a project."""

    def remove_redirect_uri(self, project: str, uri: str) -> None:
        """Remove a redirect URI from the client of a project."""


class Launch(BaseModel):
    """What the spawner needs to start the gateway of one researcher and project."""

    model_config = ConfigDict(frozen=True)

    session: str
    subject: str
    project: str
    client_id: str
    redirect_uri: str
    launch_context: PlatformLaunchContext


class NoSessionError(Exception):
    """The researcher has not launched a session in the project."""


class LaunchService:
    """Authorizes launches against the registry and prepares the project client."""

    def __init__(
        self,
        registry: Registry,
        clients: ProjectClients,
        platform_id: str,
        redirect_uri_template: str,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        """Launch sessions of one platform."""
        self._registry = registry
        self._clients = clients
        self._platform_id = platform_id
        self._redirect_uri_template = redirect_uri_template
        self._clock = clock

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
        """Prepare the session of a member, creating it at the first launch."""
        found = self._registry.project(subject, project)
        session = self._registry.session(subject, project, create=True)
        assert session is not None
        redirect_uri = self._redirect_uri(session)
        self._clients.ensure_project_client(project)
        self._clients.add_redirect_uri(project, redirect_uri)
        return Launch(
            session=session.id,
            subject=subject,
            project=project,
            client_id=project_client(project),
            redirect_uri=redirect_uri,
            launch_context=self._context(found, session),
        )

    def context(self, subject: str, project: str) -> PlatformLaunchContext:
        """Return the launch context of a member's existing session."""
        found = self._registry.project(subject, project)
        session = self._registry.session(subject, project)
        if session is None:
            raise NoSessionError
        return self._context(found, session)

    def delete(self, subject: str, project: str) -> None:
        """Remove the session of a researcher in a project, whether or not it exists."""
        session = self._registry.session(subject, project)
        if session is None:
            return
        self._clients.remove_redirect_uri(project, self._redirect_uri(session))
        self._registry.delete_session(subject, project)
