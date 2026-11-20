# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""The data catalog's MCP tools: operation binding, project scope and kept observations."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fakes import ISSUER, SigningKey, TokenReviews
from heartwood_platform.common.tokens import ResearcherTokens, ServiceTokens
from heartwood_platform.data.access import ObjectAccess
from heartwood_platform.data.app import create_app
from heartwood_platform.data.catalog import Catalog as CatalogModel
from heartwood_platform.data.catalog_tools import (
    MCP_PATH,
    OBSERVATION_TOOL,
    Operations,
    create_catalog_app,
)
from heartwood_platform.data.signing import Credentials
from mcp.types import CallToolResult
from starlette.testclient import TestClient
from test_data_service import _ALICE, _BOB, _catalog

from heartwood.schemas import PlatformOperationObservation

_ACCEPT = {"Accept": "application/json, text/event-stream"}
_NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
_HOST = "heartwood-data.heartwood-platform.svc.cluster.local:8443"


class Catalog:
    def __init__(self, directory: Path, document: dict[str, Any] | None = None) -> None:
        self.key = SigningKey()
        self.contexts = {(_ALICE, "demo"): "session-alice-demo", (_BOB, "demo"): "session-bob"}
        self.unavailable = False
        catalog = _catalog() if document is None else CatalogModel.model_validate(document)
        researchers = ResearcherTokens(ISSUER, "heartwood-data", self.key.key_set)
        tools = create_catalog_app(
            catalog,
            researchers,
            Operations(directory / "data.sqlite3"),
            self.context,
            hostname=_HOST,
            mount_path="/data",
            clock=lambda: _NOW,
        )
        self.app = create_app(
            catalog,
            researchers,
            ServiceTokens(TokenReviews().review, "heartwood-data", frozenset()),
            ObjectAccess(
                "https://store:7070", "us-east-1", lambda: Credentials("a", "b"), lambda: _NOW
            ),
            hostname=_HOST,
            platform_id="reference-platform",
            catalog_tools=tools,
        )

    def context(self, subject: str, project: str) -> str:
        if self.unavailable:
            raise httpx.ConnectError("control service unreachable")
        try:
            return self.contexts[(subject, project)]
        except KeyError:
            raise LookupError(subject, project) from None

    def headers(
        self,
        subject: str = _ALICE,
        project: str = "demo",
        **claims: Any,  # noqa: ANN401
    ) -> dict[str, str]:
        token = self.key.token(
            **{"aud": "heartwood-data", "sub": subject, "azp": f"hw-proj-{project}", **claims}
        )
        return {"Authorization": f"Bearer {token}", **_ACCEPT}


@pytest.fixture
def catalog(tmp_path: Path) -> Iterator[tuple[Catalog, TestClient]]:
    service = Catalog(tmp_path)
    with TestClient(service.app) as client:
        yield service, client


def _rpc(client: TestClient, headers: dict[str, str], method: str, params: dict[str, Any]) -> Any:  # noqa: ANN401
    return client.post(
        MCP_PATH,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        headers=headers,
    )


def _call(
    client: TestClient,
    headers: dict[str, str],
    tool: str,
    **arguments: Any,  # noqa: ANN401
) -> dict[str, Any]:
    response = _rpc(client, headers, "tools/call", {"name": tool, "arguments": arguments})
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert isinstance(result, dict)
    return result


def _observation(result: dict[str, Any]) -> PlatformOperationObservation:
    assert result["isError"] is False, result
    return PlatformOperationObservation.model_validate_json(json.dumps(result["structuredContent"]))


def test_every_tool_is_read_only_and_takes_the_operation_binding(
    catalog: tuple[Catalog, TestClient],
) -> None:
    service, client = catalog
    tools = _rpc(client, service.headers(), "tools/list", {}).json()["result"]["tools"]
    assert {tool["name"] for tool in tools} == {
        "list_datasets",
        "list_objects",
        "resolve_object",
        OBSERVATION_TOOL,
    }
    for tool in tools:
        assert tool["annotations"]["readOnlyHint"] is True
        assert {"project_id", "operation_id"} <= set(tool["inputSchema"]["required"])


def test_the_listing_names_the_projects_datasets_and_their_objects(
    catalog: tuple[Catalog, TestClient],
) -> None:
    service, client = catalog
    operation = str(uuid4())
    observed = _observation(
        _call(client, service.headers(), "list_datasets", project_id="demo", operation_id=operation)
    )
    assert observed.status == "completed"
    assert observed.project_context_id == "session-alice-demo"
    assert observed.operation_id == operation
    assert observed.service_id == "heartwood-data"
    result: Any = observed.result
    (dataset,) = result["datasets"]
    assert (dataset["id"], dataset["object_count"]) == ("synthetic-cohort", 2)
    # Another project's dataset is not listed.
    assert "other-panel" not in json.dumps(result)
    objects = _observation(
        _call(
            client,
            service.headers(),
            "list_objects",
            dataset_id="synthetic-cohort",
            project_id="demo",
            operation_id=str(uuid4()),
        )
    )
    listed: Any = objects.result
    assert {item["path"] for item in listed["objects"]} == {
        "/data/synthetic-cohort/participants.csv",
        "/data/synthetic-cohort/README.md",
    }
    assert listed["next_offset"] is None
    denied = _observation(
        _call(
            client,
            service.headers(),
            "list_objects",
            dataset_id="other-panel",
            project_id="demo",
            operation_id=str(uuid4()),
        )
    )
    assert denied.status == "denied"


@pytest.mark.parametrize("character", ["D", '"', "\u8840", "\U0001f9ec"])
def test_answers_of_a_large_catalog_stay_below_the_result_limit(
    tmp_path: Path, character: str
) -> None:
    document = _catalog().model_dump(mode="json")
    dataset = document["projects"][0]["datasets"][0]
    dataset["objects"] = [
        {**dataset["objects"][0], "name": f"part-{index:04d}-of-a-long-synthetic-name.csv"}
        for index in range(200)
    ]
    document["projects"][0]["datasets"] = [
        {
            **dataset,
            "id": f"synthetic-cohort-{index:02d}",
            "name": character * 200,
            "description": character * 2000,
        }
        for index in range(30)
    ]
    service = Catalog(tmp_path, document=document)
    with TestClient(service.app) as client:
        listing = _rpc(
            client,
            service.headers(),
            "tools/call",
            {
                "name": "list_datasets",
                "arguments": {"project_id": "demo", "operation_id": str(uuid4())},
            },
        )
        page = _rpc(
            client,
            service.headers(),
            "tools/call",
            {
                "name": "list_objects",
                "arguments": {
                    "dataset_id": "synthetic-cohort-00",
                    "project_id": "demo",
                    "operation_id": str(uuid4()),
                    "offset": 190,
                },
            },
        )
    for response in (listing, page):
        # The gateway counts the whole tool result against its limit, at most 32 KiB.
        result = CallToolResult.model_validate(response.json()["result"])
        assert len(result.model_dump_json(by_alias=True, exclude_none=True).encode()) < 24 * 1024
    listed = listing.json()["result"]["structuredContent"]["result"]
    assert 1 <= len(listed["datasets"]) <= 10
    assert listed["next_offset"] == len(listed["datasets"])
    assert len(listed["datasets"][0]["description"]) == 500
    objects = page.json()["result"]["structuredContent"]["result"]
    assert (len(objects["objects"]), objects["next_offset"]) == (10, None)


def test_an_operation_identifier_must_be_a_uuid(catalog: tuple[Catalog, TestClient]) -> None:
    service, client = catalog
    refused = _call(
        client, service.headers(), "get_operation", project_id="demo", operation_id="x" * 5000
    )
    assert refused["isError"] is True


def test_an_object_resolves_to_its_drs_object_and_data_path(
    catalog: tuple[Catalog, TestClient],
) -> None:
    service, client = catalog
    observed = _observation(
        _call(
            client,
            service.headers(),
            "resolve_object",
            object_id="synthetic-cohort.README.md",
            project_id="demo",
            operation_id=str(uuid4()),
        )
    )
    result: Any = observed.result
    assert result is not None
    found = result["object"]
    assert found["self_uri"] == f"drs://{_HOST}/synthetic-cohort.README.md"
    assert found["path"] == "/data/synthetic-cohort/README.md"


def test_an_object_of_another_project_is_denied(catalog: tuple[Catalog, TestClient]) -> None:
    service, client = catalog
    observed = _observation(
        _call(
            client,
            service.headers(),
            "resolve_object",
            object_id="other-panel.panel.csv",
            project_id="demo",
            operation_id=str(uuid4()),
        )
    )
    assert observed.status == "denied"
    assert observed.result is None
    assert "result" not in observed.model_dump(mode="json")


def test_a_call_naming_another_project_or_without_a_session_is_refused(
    catalog: tuple[Catalog, TestClient],
) -> None:
    service, client = catalog
    # Alice is a member of both projects; her token for demo cannot name project other.
    refused = _call(
        client, service.headers(), "list_datasets", project_id="other", operation_id=str(uuid4())
    )
    assert refused["isError"] is True
    assert "project of the token" in refused["content"][0]["text"]
    # Her token for other names other, but she has no session there.
    no_session = _call(
        client,
        service.headers(project="other"),
        "list_datasets",
        project_id="other",
        operation_id=str(uuid4()),
    )
    assert no_session["isError"] is True
    assert "no session" in no_session["content"][0]["text"]
    service.unavailable = True
    unavailable = _call(
        client, service.headers(), "list_datasets", project_id="demo", operation_id=str(uuid4())
    )
    assert unavailable["isError"] is True


@pytest.mark.parametrize(
    "claims",
    [{"aud": "heartwood-control"}, {"azp": "heartwood-cli"}, {"sub": _BOB, "azp": "hw-proj-other"}],
)
def test_tokens_of_other_audiences_clients_or_non_members_are_refused(
    catalog: tuple[Catalog, TestClient], claims: dict[str, Any]
) -> None:
    service, client = catalog
    response = _rpc(client, service.headers(**claims), "tools/list", {})
    assert response.status_code == 401
    assert _rpc(client, _ACCEPT, "tools/list", {}).status_code == 401


def test_a_repeated_operation_returns_the_kept_observation_and_another_request_is_refused(
    catalog: tuple[Catalog, TestClient],
) -> None:
    service, client = catalog
    operation = str(uuid4())
    arguments = {"project_id": "demo", "operation_id": operation}
    first = _call(client, service.headers(), "list_datasets", **arguments)
    again = _call(client, service.headers(), "list_datasets", **arguments)
    assert again["structuredContent"] == first["structuredContent"]
    other = _call(
        client,
        service.headers(),
        "resolve_object",
        object_id="synthetic-cohort.README.md",
        **arguments,
    )
    assert other["isError"] is True
    observed = _call(client, service.headers(), OBSERVATION_TOOL, **arguments)
    assert observed["structuredContent"] == first["structuredContent"]
    # Another researcher of the project does not see Alice's operation.
    bob = _observation(_call(client, service.headers(_BOB), OBSERVATION_TOOL, **arguments))
    assert bob.status == "failed"


def test_an_unseen_operation_is_reported_failed_and_refused_afterwards(
    catalog: tuple[Catalog, TestClient],
) -> None:
    service, client = catalog
    arguments = {"project_id": "demo", "operation_id": str(uuid4())}
    observed = _observation(_call(client, service.headers(), OBSERVATION_TOOL, **arguments))
    assert observed.status == "failed"
    assert observed.result is None
    late = _call(client, service.headers(), "list_datasets", **arguments)
    assert late["isError"] is True
