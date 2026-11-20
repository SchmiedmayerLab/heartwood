# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""The estimate tool of the control service on the cluster.

A synthetic researcher signs in, the project client exchanges the token for the control
service, and the tool prices a synthetic plan from the chart's price sheet. The tool
answers only for the researcher and project of the token.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from identity import (
    CONTROL,
    FIRST,
    SESSIONS,
    exchange,
    launch_url,
    probe,
    request,
    secret,
    sign_in,
    spawner_token,
)
from kube import Cluster

_PLAN = (
    Path(__file__).resolve().parents[3]
    / "fixtures"
    / "synthetic"
    / "platform-contract"
    / "research-plan.r1.prediction.json"
)


class Researcher:
    """The first researcher with sessions in both projects and one token for each."""

    def __init__(self, cluster: Cluster, pod: str) -> None:
        self.cluster = cluster
        self.pod = pod
        self.sessions: dict[str, str] = {}
        self.tokens: dict[str, dict[str, str]] = {}

    def launch(self, project: str) -> None:
        status, launch = request(
            self.cluster, self.pod, "PUT", launch_url(FIRST, project), spawner_token(self.cluster)
        )
        assert status == 200, launch
        client = {
            "client_id": launch["client_id"],
            "client_secret": secret(
                self.cluster, SESSIONS, f"hw-gw-{launch['session']}", "keycloak-client-secret"
            ),
        }
        signed_in = sign_in(
            self.cluster,
            self.pod,
            username="researcher-one",
            redirect_uri=launch["redirect_uri"],
            **client,
        )
        self.sessions[project] = launch["session"]
        self.tokens[project] = {}
        for service in ("control", "data"):
            status, exchanged = exchange(
                self.cluster,
                self.pod,
                subject_token=signed_in["access_token"],
                service=service,
                audience=f"heartwood-{service}",
                **client,
            )
            assert status == 200, exchanged.get("error_description")
            self.tokens[project][service] = exchanged["access_token"]

    def plan(self, project: str) -> dict[str, Any]:
        """Return the synthetic plan, bound to the launch context of a session."""
        document = json.loads(_PLAN.read_text(encoding="utf-8"))
        document["project_context_id"] = self.sessions[project]
        for evidence in document["evidence"]:
            evidence["project_context_id"] = self.sessions[project]
        assert isinstance(document, dict)
        return document

    def call(self, token: str | None, method: str, params: dict[str, Any]) -> tuple[int, Any]:
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        return request(self.cluster, self.pod, "POST", f"{CONTROL}/mcp", token, body)

    def estimate(self, token: str, project: str, plan: dict[str, Any]) -> dict[str, Any]:
        status, answer = self.call(
            token,
            "tools/call",
            {
                "name": "estimate_workload",
                "arguments": {
                    "project_id": project,
                    "scope": "plan",
                    "prediction": plan,
                    "element_ids": None,
                },
            },
        )
        assert status == 200, answer
        result = answer["result"]
        assert isinstance(result, dict)
        return result


@pytest.fixture(scope="module")
def researcher(cluster: Cluster, probe_image: str) -> Iterator[Researcher]:
    with probe(cluster, "hw-estimate-probe", probe_image) as pod:
        found = Researcher(cluster, pod)
        try:
            for project in ("demo", "other"):
                found.launch(project)
            yield found
        finally:
            for project in ("demo", "other"):
                request(cluster, pod, "DELETE", launch_url(FIRST, project), spawner_token(cluster))


def _refusal(result: dict[str, Any]) -> str:
    assert result["isError"] is True, result
    assert result.get("structuredContent") is None
    return str(result["content"][0]["text"])


def test_tools_are_listed_read_only(researcher: Researcher) -> None:
    status, answer = researcher.call(researcher.tokens["demo"]["control"], "tools/list", {})
    assert status == 200, answer
    tools = answer["result"]["tools"]
    assert sorted(tool["name"] for tool in tools) == ["estimate_workload", "get_budgets"]
    assert all(tool["annotations"]["readOnlyHint"] is True for tool in tools)


def test_plan_is_priced_from_the_chart_price_sheet(researcher: Researcher) -> None:
    result = researcher.estimate(
        researcher.tokens["demo"]["control"], "demo", researcher.plan("demo")
    )
    assert result.get("isError") is not True, result
    estimate = result["structuredContent"]
    assert estimate["project_context_id"] == researcher.sessions["demo"]
    assert estimate["total"]["coverage"] == "complete"
    assert estimate["total"]["denomination"] == {"kind": "currency", "unit": "USD"}
    # The chart's default price sheet prices the synthetic plan at these amounts.
    assert (estimate["total"]["lower"], estimate["total"]["expected"]) == ("18.54", "24.7")
    assert set(estimate["covered_element_ids"]) == {"exploration", "batch", "inference"}
    assert estimate["focus_evidence"]
    assert estimate["valid_until"] > estimate["created_at"]


def test_tool_refuses_requests_without_a_token_for_the_control_service(
    researcher: Researcher,
) -> None:
    for token in (None, researcher.tokens["demo"]["data"]):
        for method in ("tools/list", "tools/call"):
            status, _ = researcher.call(token, method, {})
            assert status == 401


def test_tool_answers_only_for_the_project_of_the_token(researcher: Researcher) -> None:
    token = researcher.tokens["other"]["control"]
    # The client of project other names project demo with its own launch context.
    assert "project of the token" in _refusal(
        researcher.estimate(token, "demo", researcher.plan("other"))
    )
    # It names its own project and presents the launch context of project demo.
    assert "another project context" in _refusal(
        researcher.estimate(token, "other", researcher.plan("demo"))
    )
    accepted = researcher.estimate(token, "other", researcher.plan("other"))
    assert accepted.get("isError") is not True, accepted
