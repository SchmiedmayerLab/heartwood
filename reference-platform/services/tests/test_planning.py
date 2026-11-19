# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""The estimate tool of the control service over MCP."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from heartwood_platform.control.planning import MAX_ELEMENTS, MCP_PATH, UNAVAILABLE
from starlette.testclient import TestClient
from test_control_app import Platform
from test_prices import prediction

from heartwood.schemas import PlatformEstimate

_ALICE = "0b9f5d1e-7c64-4f0e-9d53-6f6f3f0f1a01"
_BOB = "0b9f5d1e-7c64-4f0e-9d53-6f6f3f0f1a02"
_ACCEPT = {"Accept": "application/json, text/event-stream"}


class Planning:
    """The control service with Bob's session in demo and Alice's in other."""

    def __init__(self, directory: Path) -> None:
        self.platform = Platform(directory)
        self.bob = str(self.platform.launch(_BOB, "demo")["session"])
        self.alice = str(self.platform.launch(_ALICE, "other")["session"])

    def arguments(self, session: str, project: str = "demo", **changes: Any) -> dict[str, Any]:  # noqa: ANN401
        return {
            "project_id": project,
            "scope": "plan",
            "prediction": prediction(project_context_id=session).model_dump(mode="json"),
            "element_ids": None,
            **changes,
        }

    def call(self, client: TestClient, headers: dict[str, str], arguments: dict[str, Any]) -> Any:  # noqa: ANN401
        request = {"name": "estimate_workload", "arguments": arguments}
        return client.post(
            MCP_PATH,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": request},
            headers={**headers, **_ACCEPT},
        )


@pytest.fixture
def planning(tmp_path: Path) -> Iterator[tuple[Planning, TestClient]]:
    service = Planning(tmp_path)
    with TestClient(service.platform.client.app) as client:
        yield service, client


def test_member_receives_an_estimate_of_the_launch_context(
    planning: tuple[Planning, TestClient],
) -> None:
    service, client = planning
    headers = service.platform.researcher(_BOB, "demo")

    response = service.call(client, headers, service.arguments(service.bob))

    result = response.json()["result"]
    assert result["isError"] is False
    quote = PlatformEstimate.model_validate_json(json.dumps(result["structuredContent"]))
    assert quote.project_context_id == service.bob
    assert quote.total.expected == "24.7"


@pytest.mark.parametrize(
    "claims",
    [
        {"aud": "heartwood-data"},
        {"aud": ["heartwood-control", "heartwood-data"]},
        {"azp": "heartwood-cli"},
        {"azp": None},
        {"typ": "ID"},
        {"iss": "https://keycloak.example.test/realms/other"},
        {"exp": 1},
        {"sub": "unknown"},
        {"azp": "hw-proj-missing"},
    ],
)
def test_requests_without_a_token_of_a_project_member_are_refused(
    planning: tuple[Planning, TestClient], claims: dict[str, Any]
) -> None:
    service, client = planning
    token = service.platform.key.token(**{"sub": _BOB, **claims})
    listing = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}

    for headers in ({}, {"Authorization": f"Bearer {token}"}):
        call = service.call(client, headers, service.arguments(service.bob))
        assert call.status_code == 401
        assert (
            client.post(MCP_PATH, json=listing, headers={**headers, **_ACCEPT}).status_code == 401
        )


def test_a_removed_member_is_refused_at_the_next_request(
    planning: tuple[Planning, TestClient],
) -> None:
    service, client = planning
    headers = service.platform.researcher(_BOB, "demo")
    service.platform.registry.synchronize([])

    assert service.call(client, headers, service.arguments(service.bob)).status_code == 401


def _refusal(response: Any) -> str:  # noqa: ANN401
    result = response.json()["result"]
    assert result["isError"] is True
    assert "structuredContent" not in result or result["structuredContent"] is None
    return str(result["content"][0]["text"])


def test_another_project_or_launch_context_is_refused(
    planning: tuple[Planning, TestClient],
) -> None:
    service, client = planning
    bob, alice = (
        service.platform.researcher(_BOB, "demo"),
        service.platform.researcher(_ALICE, "other"),
    )

    other_project = service.arguments(service.bob, project="other")
    assert "project of the token" in _refusal(service.call(client, bob, other_project))
    # Alice's launch context in another project, presented with Bob's token.
    substituted = service.arguments(service.alice)
    assert "another project context" in _refusal(service.call(client, bob, substituted))
    # A member of demo without a session there.
    alice_in_demo = service.platform.researcher(_ALICE, "demo")
    response = service.call(client, alice_in_demo, service.arguments(service.alice))
    assert "no session" in _refusal(response)
    # Alice's own context in other is accepted.
    own = service.call(client, alice, service.arguments(service.alice, project="other"))
    assert own.json()["result"]["isError"] is False


def test_another_researchers_launch_context_in_the_same_project_is_refused(
    planning: tuple[Planning, TestClient],
) -> None:
    service, client = planning
    alice = str(service.platform.launch(_ALICE, "demo")["session"])
    bob = service.platform.researcher(_BOB, "demo")

    response = service.call(client, bob, service.arguments(alice))

    assert "another project context" in _refusal(response)


def test_prediction_with_more_elements_than_the_limit_is_refused(
    planning: tuple[Planning, TestClient],
) -> None:
    service, client = planning
    headers = service.platform.researcher(_BOB, "demo")

    def elements(count: int) -> dict[str, Any]:
        arguments = service.arguments(service.bob)
        (batch,) = [e for e in arguments["prediction"]["elements"] if e["kind"] == "batch"]
        arguments["prediction"]["elements"] = [
            {**batch, "element_id": f"batch-{index}"} for index in range(count)
        ]
        return arguments

    accepted = service.call(client, headers, elements(MAX_ELEMENTS)).json()["result"]
    assert accepted["isError"] is False
    assert len(accepted["structuredContent"]["covered_element_ids"]) == MAX_ELEMENTS
    refused = service.call(client, headers, elements(MAX_ELEMENTS + 1))
    assert f"at most {MAX_ELEMENTS} elements" in _refusal(refused)


def test_identity_provider_outage_is_reported_as_unavailable(tmp_path: Path) -> None:
    service = Planning(tmp_path)
    service.platform.key.unavailable = True
    headers = service.platform.researcher(_BOB, "demo")

    with TestClient(service.platform.client.app) as client:
        response = service.call(client, headers, service.arguments(service.bob))
        rest = client.get("/v1/launch-context", headers=headers)

    assert (response.status_code, response.json()) == (502, {"error": UNAVAILABLE})
    assert (rest.status_code, rest.json()) == (502, {"error": UNAVAILABLE})


@pytest.mark.parametrize(
    "changes",
    [
        {"prediction": {"schema_version": "heartwood.workload-prediction.v1"}},
        {"scope": "elements", "element_ids": ["missing"]},
        {"scope": "elements", "element_ids": None},
        {"element_ids": ["batch"]},
    ],
)
def test_invalid_requests_are_refused(
    planning: tuple[Planning, TestClient], changes: dict[str, Any]
) -> None:
    service, client = planning
    headers = service.platform.researcher(_BOB, "demo")

    _refusal(service.call(client, headers, service.arguments(service.bob, **changes)))


def test_rest_interface_is_unchanged_beside_the_mcp_path(
    planning: tuple[Planning, TestClient],
) -> None:
    service, client = planning
    headers = service.platform.researcher(_BOB, "demo")

    assert client.get("/v1/launch-context", headers=headers).json()["project_context_id"] == (
        service.bob
    )
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/v1/unknown").status_code == 404
    assert client.post("/v1/launch-context", headers=headers).status_code == 405


@pytest.fixture
def served(tmp_path: Path) -> Iterator[tuple[Planning, str]]:
    service = Planning(tmp_path)
    server = uvicorn.Server(
        uvicorn.Config(service.platform.client.app, host="127.0.0.1", port=0, log_config=None)
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "the server did not start"
        time.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield service, f"http://127.0.0.1:{port}{MCP_PATH}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_mcp_client_discovers_the_read_only_tool_and_receives_a_platform_estimate(
    served: tuple[Planning, str],
) -> None:
    service, url = served
    token = service.platform.key.token(sub=_BOB, azp="hw-proj-demo")

    async def session() -> tuple[Any, Any]:
        async with Client(StreamableHttpTransport(url, auth=token)) as client:
            tools = await client.list_tools()
            result = await client.call_tool(
                "estimate_workload",
                service.arguments(service.bob, scope="elements", element_ids=["batch"]),
            )
            return tools, result

    tools, result = asyncio.run(session())

    (tool,) = tools
    assert tool.name == "estimate_workload"
    assert tool.annotations is not None
    assert tool.annotations.read_only_hint is True
    quote = PlatformEstimate.model_validate_json(json.dumps(result.structured_content))
    assert quote.scope == "elements"
    assert quote.covered_element_ids == ("batch",)
    assert quote.project_context_id == service.bob
