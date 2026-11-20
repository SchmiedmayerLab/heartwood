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
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from hub import Browser, gateway_pods, wait_for_gateway
from identity import CONTROL, FIRST, PLATFORM, SECOND, SESSIONS, probe, request, secret
from kube import Cluster

_ROUTE = "platform-route"
_MODEL = "synthetic-coder"
# The synthetic model's command, which the researcher approves.
_COMMAND = "printf synthetic-route > /workspace/synthetic-route.txt"


@dataclass
class Server:
    """One named server of the first researcher, opened through the Hub's proxy."""

    browser: Browser
    project: str
    session: str = ""
    capability: str = ""
    conversation: str = ""

    @property
    def base(self) -> str:
        return f"/user/{FIRST}/{self.project}"

    def start(self) -> None:
        status, body = self.browser.spawn(FIRST, self.project)
        assert status == 302, body[:300]
        wait_for_gateway(self.browser, self.base)
        (pod,) = [
            pod
            for pod in gateway_pods(self.browser.cluster)
            if pod["metadata"]["labels"].get("hub.jupyter.org/servername") == self.project
        ]
        self.session = pod["metadata"]["labels"]["heartwood.platform/session"]
        self.capability = self.browser.launch(self.base)

    def stop(self) -> None:
        self.browser.hub_api("DELETE", f"/users/{FIRST}/servers/{self.project}")
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            status, user = self.browser.hub_api("GET", f"/users/{FIRST}")
            stopped = status == 200 and self.project not in user.get("servers", {})
            if stopped and not gateway_pods(self.browser.cluster, self.session):
                return
            time.sleep(3)
        raise AssertionError("the Hub did not stop the server")

    def api(
        self, method: str, path: str, body: dict[str, Any] | None = None, max_time: int = 60
    ) -> tuple[int, Any]:
        return self.browser.api(self.base, method, path, self.capability, body, max_time)

    def connect(self) -> dict[str, Any]:
        status, catalog = self.api(
            "POST", "/settings/models/catalog", {"connection_id": _ROUTE, "refresh": True}
        )
        assert status == 200, catalog
        status, settings = self.api(
            "POST", "/settings/models/connect", {"connection_id": _ROUTE, "model_id": _MODEL}
        )
        assert status == 200, settings
        status, created = self.api("POST", "/sessions", {"title": "Model route"})
        assert status == 201, created
        self.conversation = created["session_id"]
        assert isinstance(catalog, dict)
        return catalog

    def command(self, kind: str, payload: dict[str, Any]) -> None:
        body = {
            "schema_version": "heartwood.session-command.v1",
            "command_id": f"{self.conversation}-{kind}-{uuid4().hex}",
            "session_id": self.conversation,
            "kind": kind,
            "created_at": datetime.now(UTC).isoformat(),
            "payload": payload,
        }
        # A command can start the session's worker before it answers.
        status, answer = self.api(
            "POST", f"/sessions/{self.conversation}/commands", body, max_time=600
        )
        assert status == 200, answer

    def events(self) -> list[dict[str, Any]]:
        status, answer = self.api("GET", f"/sessions/{self.conversation}/events")
        assert status == 200, answer
        events: list[dict[str, Any]] = answer["events"]
        return events

    def wait(self, found: Callable[[list[dict[str, Any]]], Any], timeout: float = 420) -> Any:  # noqa: ANN401
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            events = self.events()
            result = found(events)
            if result:
                return result
            time.sleep(3)
        raise AssertionError(json.dumps(self.events()[-8:], indent=1)[-4000:])


def _of(events: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [event for event in events if event["kind"] == kind]


def _pending(events: list[dict[str, Any]]) -> str | None:
    resolved = {event["payload"].get("group_id") for event in _of(events, "confirmation.resolved")}
    for event in _of(events, "confirmation.requested"):
        group = event["payload"].get("request", {}).get("group_id")
        if group and group not in resolved:
            return str(group)
    return None


def _errors(events: list[dict[str, Any]]) -> list[str]:
    return [str(event["payload"].get("code")) for event in _of(events, "error.recorded")]


@pytest.fixture(scope="module")
def browser(cluster: Cluster, probe_image: str) -> Iterator[Browser]:
    with probe(cluster, "hw-worker-browser", probe_image) as pod:
        found = Browser(cluster, pod, "researcher-one")
        found.sign_in_to_hub()
        yield found


@pytest.fixture(scope="module")
def small(browser: Browser) -> Iterator[Server]:
    server = Server(browser, "small")
    server.start()
    try:
        yield server
    finally:
        browser.hub_api("DELETE", f"/users/{FIRST}/servers/small", {"remove": True})


def test_the_gateway_discovers_the_route_models_with_its_discovery_key(small: Server) -> None:
    catalog = small.connect()

    models = {item["model_id"]: item for item in catalog["models"]}
    assert _MODEL in models
    declared = models[_MODEL]
    assert declared["availability"] == "available"
    assert (declared["context_window"], declared["supports_tools"]) == (5120, True)
    status, settings = small.api("GET", "/settings/models")
    assert status == 200, settings
    assert settings["active_profile"] == _ROUTE


def test_an_approved_tool_call_runs_in_the_worker_through_the_model_route(
    small: Server, cluster: Cluster
) -> None:
    small.command("chat", {"prompt": "Record the synthetic value."})

    group = small.wait(_pending)
    requested = [
        event
        for event in _of(small.events(), "confirmation.requested")
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

    executions = small.wait(lambda events: _of(events, "tool.execution.recorded"))
    assert _COMMAND in json.dumps(executions)
    finished = small.wait(
        lambda events: [
            event
            for event in _of(events, "agent_message.emitted")
            if "synthetic model route completed" in json.dumps(event["payload"])
        ]
    )
    assert finished
    assert not _errors(small.events())


def test_budget_exhaustion_is_answered_with_the_diagnostic(small: Server) -> None:
    before = len(small.events())
    small.command("chat", {"prompt": "Record the synthetic value again."})

    codes = small.wait(lambda events: _errors(events[before:]))

    assert "HW-AGENT-009" in codes, codes


def test_settlement_records_the_spending_above_the_key_budget(
    small: Server, cluster: Cluster
) -> None:
    session = small.session
    small.browser.hub_api("DELETE", f"/users/{FIRST}/servers/small", {"remove": True})
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
    # Two requests of 0.00125 each; the key's budget was the reservation minus 0.003584.
    assert float(incurred) == pytest.approx(0.0025)
    assert float(incurred) > float(amount) - 0.003584


def test_the_second_researcher_cannot_start_a_server_of_project_small(
    cluster: Cluster, probe_image: str
) -> None:
    with probe(cluster, "hw-worker-second", probe_image) as pod:
        second = Browser(cluster, pod, "researcher-two")
        second.sign_in_to_hub()
        status, _, page = second.get(f"/hub/spawn/{SECOND}/small")
        assert 'value="small"' not in page
        # The Hub accepts the request and the spawn hook refuses the start.
        second.hub_api("POST", f"/users/{SECOND}/servers/small")
        deadline = time.monotonic() + 120
        started: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            status, user = second.hub_api("GET", f"/users/{SECOND}")
            assert status == 200, user
            started = user["servers"].get("small")
            if started is None or not started["pending"]:
                break
            time.sleep(3)
        assert started is None or not started["ready"], started
        assert not [
            pod
            for pod in gateway_pods(cluster)
            if pod["metadata"]["labels"].get("hub.jupyter.org/username") == SECOND
        ]


@pytest.fixture(scope="module")
def demo(browser: Browser) -> Iterator[Server]:
    server = Server(browser, "demo")
    server.start()
    try:
        yield server
    finally:
        browser.hub_api("DELETE", f"/users/{FIRST}/servers/demo", {"remove": True})


def _turn(server: Server, prompt: str) -> None:
    before = len(server.events())
    server.command("chat", {"prompt": prompt})
    group = server.wait(lambda events: _pending(events[before:]))
    server.command("approve", {"target_type": "action-set", "target_id": group})
    server.wait(lambda events: _of(events[before:], "tool.execution.recorded"))


def test_a_rotated_key_fails_the_attached_worker_and_reaches_the_next_one(
    demo: Server, cluster: Cluster
) -> None:
    demo.connect()
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
        f"{CONTROL}/v1/researchers/{FIRST}/projects/demo/model-key",
        token,
    )
    assert status == 200, answer
    assert answer == {"session": demo.session}
    assert secret(cluster, SESSIONS, account, "model-discovery-key") != discovery

    before = len(demo.events())
    demo.command("chat", {"prompt": "Record the synthetic value after the rotation."})
    codes = demo.wait(lambda events: _errors(events[before:]))
    assert codes, codes
    assert not _of(demo.events()[before:], "tool.execution.recorded")

    demo.stop()
    demo.start()
    demo.connect()
    _turn(demo, "Record the synthetic value with the rotated key.")
