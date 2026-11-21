# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""A hosted session's worker on the model route.

The first researcher signs in through the Hub and starts the server of project small,
whose inference budget ends after two requests to the synthetic model. The gateway
discovers the route's models with its discovery key, and a worker runs an approved tool
call through the model route. The next turn exceeds the key's budget and ends with the
diagnostic, and deleting the server settles what the session incurred. In project demo,
an operator rotates the session's key: the attached worker fails, and the worker of the
next start uses the new key. The second researcher cannot start a server of project
small.
"""

from __future__ import annotations

import ast
import json
import re
import time
from collections.abc import Iterator
from typing import Any

import pytest
from hub import Browser, Server, error_codes, events_of, gateway_pods, pending_group
from identity import (
    CONTROL,
    FIRST,
    PLATFORM,
    SECOND,
    SESSIONS,
    probe,
    request,
    researcher,
    secret,
)
from kube import Cluster

_ROUTE = "platform-route"
_MODEL = "synthetic-coder"
# The synthetic model's command, which the researcher approves.
_COMMAND = "printf synthetic-route > /workspace/synthetic-route.txt"


@pytest.fixture(scope="module")
def browser(cluster: Cluster, probe_image: str) -> Iterator[Browser]:
    with probe(cluster, "hw-worker-browser", probe_image) as pod:
        found = Browser(cluster, pod, "researcher-one")
        found.sign_in_to_hub()
        yield found


@pytest.fixture(scope="module")
def small(browser: Browser) -> Iterator[Server]:
    server = Server(browser, FIRST, "small")
    server.start()
    try:
        yield server
    finally:
        server.remove()


def test_the_gateway_discovers_the_route_models_with_its_discovery_key(small: Server) -> None:
    catalog = small.connect(_ROUTE, _MODEL)

    models = {item["model_id"]: item for item in catalog["models"]}
    assert _MODEL in models
    declared = models[_MODEL]
    assert declared["availability"] == "available"
    assert (declared["context_window"], declared["supports_tools"]) == (17408, True)
    status, settings = small.api("GET", "/settings/models")
    assert status == 200, settings
    assert settings["active_profile"] == _ROUTE


def test_an_approved_tool_call_runs_in_the_worker_through_the_model_route(
    small: Server, cluster: Cluster
) -> None:
    small.command("chat", {"prompt": "Record the synthetic value."})

    group = small.wait(pending_group)
    requested = [
        event
        for event in events_of(small.events(), "confirmation.requested")
        if event["payload"]["request"]["group_id"] == group
    ]
    assert _COMMAND in json.dumps(requested)
    # The worker runs in the session namespace on the Kubernetes driver.
    pods = cluster.kubectl(
        "get",
        f"--namespace=hw-s-{small.session}",
        "pods",
        "--selector=heartwood.role=worker",
        "--output=json",
    )
    assert json.loads(pods.stdout)["items"], pods.stderr
    small.command("approve", {"target_type": "action-set", "target_id": group})

    # The execution names the approved call; it records no command or output.
    calls = set(re.findall(r'"tool_call_id": "([^"]+)"', json.dumps(requested)))
    (execution,) = small.wait(lambda events: events_of(events, "tool.execution.recorded"))
    assert execution["payload"]["tool_call_id"] in calls
    assert (execution["payload"]["tool_name"], execution["payload"]["exit_code"]) == ("terminal", 0)
    finished = small.wait(
        lambda events: [
            event
            for event in events_of(events, "agent_message.emitted")
            if "synthetic model route completed" in json.dumps(event["payload"])
        ]
    )
    assert finished
    assert not error_codes(small.events())


def test_budget_exhaustion_is_answered_with_the_diagnostic(small: Server) -> None:
    before = len(small.events())
    small.command("chat", {"prompt": "Record the synthetic value again."})

    codes = small.wait(lambda events: error_codes(events[before:]))

    # LiteLLM answers an exhausted key budget with HTTP 422 `budget_exceeded`, which its
    # client raises as `BadRequestError`. The OpenHands SDK of the runtime lock maps that
    # to `LLMBadRequestError` and classifies it as a configuration failure, so the
    # gateway reports HW-AGENT-011, model configuration is invalid, for this budget.
    assert "HW-AGENT-011" in codes, codes


def test_settlement_records_the_spending_above_the_key_budget(
    small: Server, cluster: Cluster
) -> None:
    session = small.session
    small.remove()
    query = (
        "import sqlite3,sys;c=sqlite3.connect('/var/lib/heartwood-control/control.sqlite3');"
        'print(c.execute("SELECT state, amount, incurred FROM reservation WHERE holder=?",'
        "(sys.argv[1],)).fetchall())"
    )
    deadline = time.monotonic() + 300
    rows = "[]"
    while time.monotonic() < deadline:
        result = cluster.kubectl(
            "exec",
            f"--namespace={PLATFORM}",
            "statefulset/heartwood-control",
            "--",
            "/opt/heartwood-platform/.venv/bin/python",
            "-I",
            "-c",
            query,
            f"session:{session}",
        )
        rows = result.stdout.strip()
        if "settled" in rows:
            break
        time.sleep(5)
    ((state, amount, incurred),) = ast.literal_eval(rows)
    assert state == "settled"
    # Two requests of 0.00125 each; the key's budget was the reservation minus one
    # request's maximum cost, 16384 input tokens at 0.50 and 1024 output tokens at 1.50
    # per million.
    assert float(incurred) == pytest.approx(0.0025)
    assert float(incurred) > float(amount) - 0.009728


def test_the_second_researcher_cannot_start_a_server_of_project_small(
    cluster: Cluster, probe_image: str
) -> None:
    with probe(cluster, "hw-worker-second", probe_image) as pod:
        second = Browser(cluster, pod, "researcher-two")
        second.sign_in_to_hub()
        status, _, page = second.get(f"/hub/spawn/{researcher(SECOND)}/small")
        assert 'value="small"' not in page
        # The Hub accepts the request and the spawn hook refuses the start.
        status, answer = second.hub_api("POST", f"/users/{researcher(SECOND)}/servers/small")
        assert status == 202, answer
        deadline = time.monotonic() + 120
        started: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            status, user = second.hub_api("GET", f"/users/{researcher(SECOND)}")
            assert status == 200, user
            started = user["servers"].get("small")
            if started is None or not started["pending"]:
                break
            time.sleep(3)
        assert started is None or not started["ready"], started
        assert not [
            pod
            for pod in gateway_pods(cluster)
            if pod["metadata"]["labels"].get("hub.jupyter.org/username") == researcher(SECOND)
        ]


@pytest.fixture(scope="module")
def demo(browser: Browser) -> Iterator[Server]:
    server = Server(browser, FIRST, "demo")
    server.start()
    try:
        yield server
    finally:
        server.remove()


def _turn(server: Server, prompt: str) -> None:
    before = len(server.events())
    server.command("chat", {"prompt": prompt})
    group = server.wait(lambda events: pending_group(events[before:]))
    server.command("approve", {"target_type": "action-set", "target_id": group})
    server.wait(lambda events: events_of(events[before:], "tool.execution.recorded"))


def test_a_rotated_key_fails_the_attached_worker_and_reaches_the_next_one(
    demo: Server, cluster: Cluster
) -> None:
    demo.connect(_ROUTE, _MODEL)
    _turn(demo, "Record the synthetic value.")
    account = f"hw-gw-{demo.session}"
    discovery = secret(cluster, SESSIONS, account, "model-discovery-key")

    token = cluster.kubectl(
        "create",
        "token",
        "heartwood-operator",
        f"--namespace={PLATFORM}",
        "--audience=heartwood-control",
    ).stdout.strip()
    status, answer = request(
        cluster,
        demo.browser.pod,
        "POST",
        f"{CONTROL}/v1/researchers/{researcher(FIRST)}/projects/demo/model-key",
        token,
    )
    assert status == 200, answer
    assert answer == {"session": demo.session}
    assert secret(cluster, SESSIONS, account, "model-discovery-key") != discovery

    before = len(demo.events())
    demo.command("chat", {"prompt": "Record the synthetic value after the rotation."})
    codes = demo.wait(lambda events: error_codes(events[before:]))
    assert codes, codes
    assert not events_of(demo.events()[before:], "tool.execution.recorded")

    demo.stop()
    demo.start()
    demo.connect(_ROUTE, _MODEL)
    _turn(demo, "Record the synthetic value with the rotated key.")
