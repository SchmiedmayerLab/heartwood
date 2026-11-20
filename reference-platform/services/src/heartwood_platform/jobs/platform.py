# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""What the job service asks of the control service: launch contexts and job budgets.

The job service never forwards a researcher's token. It calls the control service with
its own projected service account token and passes subject and project as data.
"""

from __future__ import annotations

import ssl
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

import httpx


class NotAMemberError(Exception):
    """The subject is not a member of the project."""


class NoSessionError(Exception):
    """The researcher has no session, and so no launch context, in the project."""


class ProjectContexts(Protocol):
    """The registry's answer to who may act in a project, and in which launch context."""

    def context(self, subject: str, project: str) -> str:
        """Return the launch context identifier of a member's session.

        Raise one of the errors above for a non-member or a member without a session.
        """


@dataclass(frozen=True)
class JobUsage:
    """Resources of a task over a duration: its limits at reservation, its use at settlement."""

    runtime_profile_ref: str
    cpu_cores: int
    ram_gb: float
    gpu_count: int
    seconds: int


class BudgetDeniedError(Exception):
    """The budget admits no reservation for the task; the message says why."""


class JobBudgets(Protocol):
    """Reservations of a project's compute budget, held by one operation each."""

    def reserve(self, project: str, holder: str, usage: JobUsage) -> str:
        """Reserve for the hard limits of a task and return the reservation's reference.

        A holder that already holds a reservation keeps it. Raise
        :class:`BudgetDeniedError` when the budget cannot hold it.
        """

    def settle(self, holder: str, usage: JobUsage) -> None:
        """Record what the task used and release the rest of its reservation."""


class ControlContexts:
    """Launch contexts from the control service's registry."""

    def __init__(self, url: str, ca_file: Path, token: Callable[[], str]) -> None:
        """Call the control service at ``url`` with the service account token ``token`` reads."""
        self._client = httpx.Client(
            base_url=url, verify=ssl.create_default_context(cafile=ca_file), timeout=30
        )
        self._token = token

    def context(self, subject: str, project: str) -> str:
        """Return the launch context identifier of a member's session."""
        # The subject travels in the body, so that no subject appears in a URL.
        response = self._client.post(
            f"/v1/projects/{quote(project, safe='')}/launch-context",
            json={"subject": subject},
            headers={"Authorization": f"Bearer {self._token()}"},
        )
        if response.status_code == httpx.codes.FORBIDDEN:
            raise NotAMemberError
        if response.status_code == httpx.codes.NOT_FOUND:
            raise NoSessionError
        response.raise_for_status()
        context = response.json().get("project_context_id")
        if not isinstance(context, str) or not context:
            raise NoSessionError
        return context
