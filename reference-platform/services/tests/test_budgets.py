# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Allocations, reservations, settlement, admission and the budget overview."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from heartwood_platform.control.budgets import Allocation, InsufficientBudgetError, Ledger
from heartwood_platform.control.launch import session_holder
from heartwood_platform.control.planning import MCP_PATH
from starlette.testclient import TestClient
from test_control_app import Platform, _id

from heartwood.schemas import BudgetObservation

_ALICE = "0b9f5d1e-7c64-4f0e-9d53-6f6f3f0f1a01"
_BOB = "0b9f5d1e-7c64-4f0e-9d53-6f6f3f0f1a02"
_HUB = {"Authorization": "Bearer hub-token"}
_ACCEPT = {"Accept": "application/json, text/event-stream"}


@pytest.fixture
def ledger(tmp_path: Path) -> Ledger:
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    ledger.synchronize([Allocation("demo", "inference", Decimal(10))])
    return ledger


def _balance(ledger: Ledger, project: str = "demo") -> tuple[object, ...]:
    (balance,) = [item for item in ledger.balances(project) if item.category == "inference"]
    return balance.allocated, balance.reserved, balance.spent, balance.available


def test_a_reservation_holds_an_amount_until_it_is_settled(ledger: Ledger) -> None:
    held = ledger.reserve("demo", "inference", "session:a", Decimal(4))

    assert held.amount == Decimal(4)
    assert _balance(ledger) == (10, 4, 0, 6)
    # A holder keeps its reservation; asking again reserves nothing more.
    assert ledger.reserve("demo", "inference", "session:a", Decimal(9)) == held
    assert _balance(ledger) == (10, 4, 0, 6)

    settled = ledger.settle("session:a", Decimal("1.25"))

    assert settled is not None
    assert settled.incurred == Decimal("1.25")
    assert _balance(ledger) == (10, 0, Decimal("1.25"), Decimal("8.75"))
    assert ledger.held("session:a") is None
    assert ledger.settle("session:a", Decimal(1)) is None


def test_spending_above_a_reservation_is_recorded_in_full(ledger: Ledger) -> None:
    ledger.reserve("demo", "inference", "session:a", Decimal(4))
    ledger.reserve("demo", "inference", "session:b", Decimal(6))

    ledger.settle("session:a", Decimal("4.5"))

    assert _balance(ledger) == (10, 6, Decimal("4.5"), Decimal("-0.5"))
    with pytest.raises(InsufficientBudgetError):
        ledger.reserve("demo", "inference", "session:c", Decimal(1))


def test_a_reservation_is_bounded_by_availability_and_refused_at_the_minimum(
    ledger: Ledger,
) -> None:
    ledger.reserve("demo", "inference", "session:a", Decimal(9))

    partial = ledger.reserve("demo", "inference", "session:b", Decimal(4))
    assert partial.amount == Decimal(1)
    with pytest.raises(InsufficientBudgetError):
        ledger.reserve("demo", "inference", "session:c", Decimal(4))
    ledger.settle("session:a", Decimal("8.5"))
    with pytest.raises(InsufficientBudgetError):
        ledger.reserve("demo", "inference", "session:c", Decimal(4), minimum=Decimal("0.5"))
    assert ledger.reserve("demo", "inference", "session:c", Decimal(4)).amount == Decimal("0.5")
    # A budget that was never allocated holds nothing.
    with pytest.raises(InsufficientBudgetError):
        ledger.reserve("demo", "compute", "job:a", Decimal(1))
    with pytest.raises(ValueError, match="not negative"):
        ledger.settle("session:c", Decimal(-1))


def test_a_changed_allocation_changes_availability_and_the_revision(ledger: Ledger) -> None:
    ledger.reserve("demo", "inference", "session:a", Decimal(4))
    (before,) = ledger.balances("demo")

    ledger.synchronize([Allocation("demo", "inference", Decimal(10))])
    assert ledger.balances("demo") == [before]

    ledger.synchronize([Allocation("demo", "inference", Decimal(3))])
    (after,) = ledger.balances("demo")
    assert (after.allocated, after.available) == (Decimal(3), Decimal(-1))
    assert after.revision > before.revision
    # A budget that is no longer configured keeps its reservation and has nothing allocated.
    ledger.synchronize([])
    assert _balance(ledger) == (0, 4, 0, -4)


def test_concurrent_reservations_never_exceed_the_allocation(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite3"
    Ledger(path).synchronize([Allocation("demo", "inference", Decimal(10))])
    reserved: list[Decimal] = []
    refused: list[str] = []
    start = threading.Barrier(24)

    def reserve(index: int) -> None:
        # Each thread opens the database itself, as separate requests would.
        ledger = Ledger(path)
        start.wait()
        try:
            reserved.append(ledger.reserve("demo", "inference", f"s:{index}", Decimal(1)).amount)
        except InsufficientBudgetError:
            refused.append(f"s:{index}")

    threads = [threading.Thread(target=reserve, args=(index,)) for index in range(24)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(reserved) == Decimal(10)
    assert len(reserved) == 10
    assert len(refused) == 14
    assert _balance(Ledger(path)) == (10, 10, 0, 0)


# The control service with its launch flow.


@pytest.fixture
def platform(tmp_path: Path) -> Platform:
    return Platform(tmp_path)


def test_a_launch_reserves_and_a_relaunch_keeps_the_reservation(platform: Platform) -> None:
    session = str(platform.launch(_BOB, "demo")["session"])
    platform.launch(_BOB, "demo")
    platform.launch(_ALICE, "demo")

    assert _balance(platform.ledger) == (5, 2, 0, 3)
    held = platform.ledger.held(session_holder(session))
    assert held is not None
    assert held.amount == Decimal(1)


def test_a_launch_without_an_available_amount_is_refused_before_provisioning(
    platform: Platform,
) -> None:
    platform.launch(_ALICE, "other")
    platform.ledger.synchronize(
        [Allocation("demo", "inference", Decimal(0)), Allocation("other", "inference", Decimal(1))]
    )
    namespaces = set(platform.kubernetes.kind("Namespace"))

    response = platform.client.put(
        f"/v1/researchers/{_id(_BOB)}/projects/demo/launch", headers=_HUB
    )

    assert response.status_code == 409
    assert response.json() == {"error": "the project's inference budget has nothing available"}
    assert set(platform.kubernetes.kind("Namespace")) == namespaces
    # The session that already holds its reservation still launches.
    platform.launch(_ALICE, "other")


def test_deletion_settles_what_the_session_incurred_and_releases_the_rest(
    platform: Platform,
) -> None:
    bob = str(platform.launch(_BOB, "demo")["session"])
    alice = str(platform.launch(_ALICE, "demo")["session"])
    platform.inference.incurred[bob] = Decimal("1.4")

    for subject in (_BOB, _ALICE):
        response = platform.client.delete(
            f"/v1/researchers/{_id(subject)}/projects/demo/launch", headers=_HUB
        )
        assert response.status_code == 204

    assert platform.inference.settled == [bob, alice]
    assert _balance(platform.ledger) == (5, 0, Decimal("1.4"), Decimal("3.6"))
    # A repeated deletion settles nothing again.
    platform.client.delete(f"/v1/researchers/{_id(_BOB)}/projects/demo/launch", headers=_HUB)
    assert platform.inference.settled == [bob, alice]


class Overview:
    """Bob's session in demo and Alice's in other, read over MCP."""

    def __init__(self, platform: Platform, client: TestClient) -> None:
        self.platform = platform
        self.client = client
        self.bob = str(platform.launch(_BOB, "demo")["session"])
        self.alice = str(platform.launch(_ALICE, "other")["session"])

    def budgets(self, subject: str, project: str, project_id: str) -> Any:  # noqa: ANN401
        request = {"name": "get_budgets", "arguments": {"project_id": project_id}}
        response = self.client.post(
            MCP_PATH,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": request},
            headers={**self.platform.researcher(subject, project), **_ACCEPT},
        )
        assert response.status_code == 200, response.text
        return response.json()["result"]


@pytest.fixture
def overview(platform: Platform) -> Iterator[Overview]:
    with TestClient(platform.client.app) as client:
        yield Overview(platform, client)


def test_a_member_reads_the_budgets_of_the_project_with_its_launch_context(
    overview: Overview,
) -> None:
    result = overview.budgets(_BOB, "demo", "demo")

    assert result["isError"] is False
    budgets = [
        BudgetObservation.model_validate_json(json.dumps(item))
        for item in result["structuredContent"]["budgets"]
    ]
    amounts = {
        item.budget_id: (item.allocated, item.reserved, item.spent, item.available)
        for item in budgets
    }
    assert amounts == {
        "demo-compute": ("50", "0", "0", "50"),
        "demo-inference": ("5", "1", "0", "4"),
    }
    assert {item.project_context_id for item in budgets} == {overview.bob}
    assert {item.denomination.unit for item in budgets} == {"USD"}


def test_the_overview_follows_reservations_and_spending(overview: Overview) -> None:
    def inference() -> BudgetObservation:
        budgets = overview.budgets(_BOB, "demo", "demo")["structuredContent"]["budgets"]
        (item,) = [entry for entry in budgets if entry["budget_id"] == "demo-inference"]
        return BudgetObservation.model_validate_json(json.dumps(item))

    before = inference()
    alice = str(overview.platform.launch(_ALICE, "demo")["session"])
    overview.platform.inference.incurred[alice] = Decimal("0.75")
    overview.client.delete(f"/v1/researchers/{_id(_ALICE)}/projects/demo/launch", headers=_HUB)

    after = inference()
    assert (after.reserved, after.spent, after.available) == ("1", "0.75", "3.25")
    assert after.platform_record_ref != before.platform_record_ref


@pytest.mark.parametrize(
    ("subject", "project", "project_id", "message"),
    [
        # Another project named by a member of both: the token's project decides.
        (_ALICE, "other", "demo", "project_id is not the project of the token"),
        (_BOB, "demo", "other", "project_id is not the project of the token"),
        # A member of the project without a session has no launch context.
        (_ALICE, "demo", "demo", "no session in the project"),
    ],
)
def test_another_project_or_a_missing_session_is_refused(
    overview: Overview, subject: str, project: str, project_id: str, message: str
) -> None:
    result = overview.budgets(subject, project, project_id)

    assert result["isError"] is True
    assert result["content"][0]["text"] == message


def test_a_rotation_reports_a_session_without_a_reservation_and_rewrites_the_others(
    platform: Platform,
) -> None:
    alice = str(platform.launch(_ALICE, "demo")["session"])
    platform.ledger.synchronize([Allocation("demo", "inference", Decimal(1))])
    refused = platform.client.put(f"/v1/researchers/{_id(_BOB)}/projects/demo/launch", headers=_HUB)
    assert refused.status_code == 409
    bob = platform.registry.session(_BOB, "demo")
    assert bob is not None

    response = platform.client.post(
        "/v1/projects/demo/client-secret", headers={"Authorization": "Bearer operator-token"}
    )

    assert response.status_code == 200
    assert response.json() == {"sessions": [alice], "failed": [bob.id]}
