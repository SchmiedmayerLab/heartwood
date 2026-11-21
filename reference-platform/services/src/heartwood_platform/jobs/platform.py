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
    """The run time limit or observed duration on a task runtime profile."""

    runtime_profile_ref: str
    seconds: int


@dataclass(frozen=True)
class BudgetAmount:
    """An amount of a project's compute budget in the denomination of the price sheet."""

    budget_id: str
    amount: str
    unit: str

    def __str__(self) -> str:
        """Return the amount with its unit."""
        return f"{self.amount} {self.unit}"


@dataclass(frozen=True)
class JobSettlement:
    """What a settled reservation held and what the task incurred."""

    reserved: BudgetAmount
    incurred: BudgetAmount


class BudgetDeniedError(Exception):
    """The budget admits no reservation for the task; the message says why."""


class JobBudgets(Protocol):
    """Reservations of a project's compute budget, held by one operation each."""

    def quote(self, project: str, usage: JobUsage) -> BudgetAmount:
        """Return what a reservation for this usage would hold now; nothing is reserved.

        Raise :class:`BudgetDeniedError` when the usage has no price.
        """

    def reserve(self, project: str, operation_id: str, usage: JobUsage) -> BudgetAmount:
        """Reserve the price of a task's run time limit for an operation.

        An operation that already holds a reservation keeps it. Raise
        :class:`BudgetDeniedError` when the budget does not cover it, when the usage has no
        price, or when the operation's reservation was settled already.
        """

    def settle(self, project: str, operation_id: str, usage: JobUsage) -> JobSettlement:
        """Record what the task used and release the rest of its reservation.

        Settling is idempotent per operation and ends it: settling again returns the first
        settlement, settling an operation that never reserved records that, and a later
        reservation for a settled operation is refused. The job service settles whenever it
        cannot tell whether a reservation reached the budget.
        """


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


class ControlBudgets:
    """Job reservations on the compute budgets of the control service's ledger."""

    def __init__(self, client: httpx.Client, token: Callable[[], str]) -> None:
        """Use the job service account for ledger requests."""
        self._client = client
        self._token = token

    def _call(
        self, method: str, path: str, usage: JobUsage, *, admission: bool = True
    ) -> dict[str, object]:
        response = self._client.request(
            method,
            path,
            json={"runtime_profile_ref": usage.runtime_profile_ref, "seconds": usage.seconds},
            headers={"Authorization": f"Bearer {self._token()}"},
        )

        if not admission or response.status_code != httpx.codes.CONFLICT:
            response.raise_for_status()
        try:
            document = response.json()
        except ValueError as error:
            raise httpx.DecodingError("invalid budget reply") from error
        if not isinstance(document, dict):
            raise httpx.DecodingError("the reply is not an object")
        if response.status_code == httpx.codes.CONFLICT:
            raise self._denied(document, usage)
        return document

    @staticmethod
    def _amount(document: dict[str, object], field: str = "amount") -> BudgetAmount:
        denomination = document.get("denomination")
        unit = denomination.get("unit") if isinstance(denomination, dict) else None
        budget, amount = document.get("budget_id"), document.get(field)
        if not all(isinstance(value, str) and value for value in (budget, amount, unit)):
            raise httpx.DecodingError("the reply names no budget amount")
        return BudgetAmount(str(budget), str(amount), str(unit))

    @staticmethod
    def _denied(document: dict[str, object], usage: JobUsage) -> BudgetDeniedError:
        error = document.get("error")
        if error == "unpriced":
            return BudgetDeniedError(f"{document.get('reason')} The budget cannot price the task.")
        if error == "settled":
            return BudgetDeniedError("The operation's reservation was settled already.")
        if error == "insufficient":
            needed = ControlBudgets._amount(document)
            available = document.get("available")
            if available is None:
                return BudgetDeniedError(f"The project has no budget {needed.budget_id}.")
            return BudgetDeniedError(
                f"Budget {needed.budget_id} has {available} {needed.unit} available, and the"
                f" run time limit of {usage.seconds} seconds needs {needed}."
            )
        raise httpx.DecodingError("the refusal names no reason")

    def quote(self, project: str, usage: JobUsage) -> BudgetAmount:
        """Return what a reservation for this usage would hold now."""
        path = f"/v1/projects/{quote(project, safe='')}/job-quotes"
        return self._amount(self._call("POST", path, usage))

    def reserve(self, project: str, operation_id: str, usage: JobUsage) -> BudgetAmount:
        """Reserve the price of a task's run time limit for an operation."""
        path = (
            f"/v1/projects/{quote(project, safe='')}/job-reservations/"
            f"{quote(operation_id, safe='')}"
        )
        return self._amount(self._call("PUT", path, usage))

    def settle(self, project: str, operation_id: str, usage: JobUsage) -> JobSettlement:
        """Record what the task used and release the rest of its reservation."""
        path = (
            f"/v1/projects/{quote(project, safe='')}/job-reservations/"
            f"{quote(operation_id, safe='')}/settlement"
        )
        document = self._call("POST", path, usage, admission=False)
        return JobSettlement(self._amount(document), self._amount(document, "incurred"))
