# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""The budgets of the control service on the cluster.

A launch reserves an amount of the project's inference budget, which the session keeps
until its deletion. A researcher reads the budgets of the project of an exchanged token,
and only of that project.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest
from identity import FIRST, SECOND, launch_url, probe, request, spawner_token
from kube import Cluster
from test_estimates import Researcher


@pytest.fixture(scope="module")
def researcher(cluster: Cluster, staging_image: str) -> Iterator[Researcher]:
    with probe(cluster, "hw-budget-probe", staging_image) as pod:
        found = Researcher(cluster, pod)
        try:
            for project in ("demo", "other"):
                found.launch(project)
            yield found
        finally:
            for subject, project in ((FIRST, "demo"), (FIRST, "other"), (SECOND, "demo")):
                request(
                    cluster, pod, "DELETE", launch_url(subject, project), spawner_token(cluster)
                )


def _budgets(researcher: Researcher, project: str, project_id: str) -> dict[str, Any]:
    status, answer = researcher.call(
        researcher.tokens[project]["control"],
        "tools/call",
        {"name": "get_budgets", "arguments": {"project_id": project_id}},
    )
    assert status == 200, answer
    result = answer["result"]
    assert isinstance(result, dict)
    return result


def _inference(researcher: Researcher, project: str = "demo") -> dict[str, str]:
    result = _budgets(researcher, project, project)
    assert result.get("isError") is not True, result
    (budget,) = [
        item
        for item in result["structuredContent"]["budgets"]
        if item["budget_id"] == f"{project}-inference"
    ]
    assert isinstance(budget, dict)
    return budget


def test_the_overview_holds_the_chart_allocations_and_the_session_reservation(
    researcher: Researcher,
) -> None:
    result = _budgets(researcher, "demo", "demo")
    assert result.get("isError") is not True, result
    budgets = {item["budget_id"]: item for item in result["structuredContent"]["budgets"]}
    assert set(budgets) == {"demo-compute", "demo-inference"}
    assert budgets["demo-compute"]["allocated"] == "50"
    inference = budgets["demo-inference"]
    assert inference["project_context_id"] == researcher.sessions["demo"]
    assert inference["denomination"] == {"kind": "currency", "unit": "USD"}
    assert inference["allocated"] == "5"
    assert Decimal(inference["reserved"]) >= 1
    assert Decimal(inference["available"]) == (
        Decimal(inference["allocated"])
        - Decimal(inference["reserved"])
        - Decimal(inference["spent"])
    )


def test_a_launch_reserves_and_a_deletion_releases(researcher: Researcher) -> None:
    before = _inference(researcher)
    status, _ = request(
        researcher.cluster,
        researcher.pod,
        "PUT",
        launch_url(SECOND, "demo"),
        spawner_token(researcher.cluster),
    )
    assert status == 200
    launched = _inference(researcher)
    assert Decimal(launched["reserved"]) == Decimal(before["reserved"]) + 1
    assert launched["platform_record_ref"] != before["platform_record_ref"]

    status, _ = request(
        researcher.cluster,
        researcher.pod,
        "DELETE",
        launch_url(SECOND, "demo"),
        spawner_token(researcher.cluster),
    )
    assert status == 204
    # Without an inference route the session incurred nothing.
    after = _inference(researcher)
    assert (after["reserved"], after["spent"]) == (before["reserved"], before["spent"])


def test_the_overview_answers_only_for_the_project_of_the_token(researcher: Researcher) -> None:
    # The client of project other names project demo.
    refused = _budgets(researcher, "other", "demo")
    assert refused["isError"] is True
    assert refused["content"][0]["text"] == "project_id is not the project of the token"
    assert _inference(researcher, "other")["allocated"] == "1.5"
