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
small. Project other exercises a partial model answer and a worker whose model access
is withdrawn between worker launches.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

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
    if record := os.environ.get("PUBLISHED_IMAGE_RECORD"):
        # The approval pauses the worker while all six production containers are running.
        subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve().parents[2] / "tools/qualification_images.py"),
                "observe",
                "--context",
                cluster.context,
                "--stage",
                os.environ["QUALIFICATION_STAGE"],
                "--run",
                os.environ["GITHUB_RUN_ID"],
                "--attempt",
                os.environ["GITHUB_RUN_ATTEMPT"],
                "--session",
                small.session,
                "--record",
                record,
                "--evidence",
                str(Path(record).with_name("observed-images.json")),
                "--commit",
                os.environ["GITHUB_SHA"],
            ],
            check=True,
        )
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

    small.wait(
        lambda events: any(
            event["payload"]["status"] == "error"
            for event in events_of(events[before:], "agent.lifecycle.updated")
        )
    )
    codes = error_codes(_settled_events(small)[before:])

    # The exhausted inference key produces only the quota diagnostic for this turn.
    assert "HW-AGENT-009" in codes, codes
    assert not {"HW-AGENT-010", "HW-AGENT-011"} & set(codes), codes


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


class _CaseServer(Server):
    """Own only the named server created for this case, including a partial start."""

    owner: str

    def registration(self) -> dict[str, Any] | None:
        status, user = self.browser.hub_api("GET", f"/users/{self.user}?include_stopped_servers=1")
        assert status == 200, status
        registered: dict[str, Any] | None = user["servers"].get(self.project)
        return registered

    def registered_session(self) -> str:
        # A spawn may fail before there is a gateway pod. Provisioning records the
        # subject/project session before creating its namespace.
        query = """
import json, sqlite3, sys
from pathlib import Path
db = json.loads(Path('/etc/heartwood-control/configuration.json').read_text())['database']
with sqlite3.connect(f'file:{db}?mode=ro', uri=True) as conn:
    row = conn.execute('SELECT id FROM session WHERE subject=? AND project_id=?',
                       sys.argv[1:]).fetchone()
    print(row[0] if row else 'absent')
"""
        result = self.browser.cluster.kubectl(
            "exec",
            "-i",
            f"--namespace={PLATFORM}",
            "statefulset/heartwood-control",
            "--",
            "/opt/heartwood-platform/.venv/bin/python",
            "-I",
            "-",
            self.subject,
            self.project,
            text_input=query,
        )
        identity = result.stdout.strip()
        if not result.returncode and identity == "absent":
            return ""
        if result.returncode or not re.fullmatch(r"[0-9a-f]{16}", identity):
            raise AssertionError("Could not identify the hosted test session")
        return identity

    def start(self) -> None:
        status, _ = self.browser.hub_api(
            "POST",
            f"/users/{self.user}/servers/{self.project}",
            {"profile": self.project, "test_owner": self.owner},
        )
        assert status in {201, 202}, status
        self.wait_started()


@contextmanager
def _owned_server(server: _CaseServer) -> Iterator[Server]:
    if server.registration() is not None:
        raise AssertionError("The named test server is already registered")
    if server.registered_session():
        raise AssertionError("The test project already has a session")
    try:
        server.start()
        yield server
    finally:
        deadline = time.monotonic() + 300
        while True:
            registered = server.registration()
            if (
                not registered
                or (registered.get("user_options") or {}).get("test_owner") != server.owner
            ):
                break
            if not registered.get("pending"):
                if not server.session:
                    server.session = server.registered_session()
                server.remove()
                break
            if time.monotonic() >= deadline:
                raise AssertionError("Owned server is still pending cleanup")
            time.sleep(3)


@pytest.fixture
def other(browser: Browser) -> Iterator[Server]:
    if not browser.cluster.context.startswith("kind-"):
        raise AssertionError("Hosted failure cases require the synthetic kind deployment")
    server = _CaseServer(browser, FIRST, "other")
    server.owner = uuid4().hex
    with _owned_server(server):
        yield server


def _failed_turn(events: list[dict[str, Any]], conversation: str) -> tuple[str, ...]:
    """Evaluate only a terminal snapshot from this conversation."""
    if any(event["session_id"] != conversation for event in events):
        raise AssertionError("Unexpected conversation in the event stream")
    lifecycle = events_of(events, "agent.lifecycle.updated")
    codes = tuple(error_codes(events))
    if not lifecycle or lifecycle[-1]["payload"]["status"] != "error" or not codes:
        return ()
    if any(
        events_of(events, kind)
        for kind in ("tool_call.proposed", "confirmation.requested", "tool.execution.recorded")
    ):
        raise AssertionError("A failed turn requested or executed a tool")
    if any(
        "synthetic model route completed" in json.dumps(event["payload"])
        for event in events_of(events, "agent_message.emitted")
    ):
        raise AssertionError("A failed turn reported successful completion")
    if not any(
        event["payload"]["phase"] == "active"
        for event in events_of(events, "worker_attempt.recorded")
    ):
        raise AssertionError("Missing active worker in the failed conversation")
    return codes


def _revoked_turn(events: list[dict[str, Any]], conversation: str) -> bool:
    codes = _failed_turn(events, conversation)
    if codes and ("HW-AGENT-011" not in codes or "HW-AGENT-012" in codes):
        raise AssertionError("Revoked model produced the wrong terminal diagnostic")
    return bool(codes)


def _settled_events(server: Server) -> list[dict[str, Any]]:
    """Read the conversation once its gateway has recorded the final callbacks."""
    deadline = time.monotonic() + 420
    while time.monotonic() < deadline:
        status, answer = server.api("GET", f"/sessions/{server.conversation}/events?settled=true")
        if status == 200:
            events: list[dict[str, Any]] = answer["events"]
            return events
        if status != 409 or answer != {"error": "session execution is still settling"}:
            raise AssertionError(f"Could not observe the settled conversation ({status})")
        time.sleep(3)
    raise AssertionError("Timed out waiting for the conversation to settle")


def _settled_failure(server: Server) -> list[dict[str, Any]]:
    server.wait(lambda events: bool(_failed_turn(events, server.conversation)))
    events = _settled_events(server)
    if not _failed_turn(events, server.conversation):
        raise AssertionError("Missing failure after execution settled")
    return events


def _require_model_denial(server: Server, events: list[dict[str, Any]]) -> None:
    if not _revoked_turn(events, server.conversation):
        raise AssertionError("Missing settled revocation")
    attempts = [
        e["payload"]["intent"]
        for e in events_of(events, "worker_attempt.recorded")
        if e["payload"]["phase"] == "active"
    ]
    if len(attempts) != 1 or attempts[0]["session_id"] != server.conversation:
        raise AssertionError("Expected one active attempt for the denied conversation")
    attempt = str(UUID(attempts[0]["attempt_id"]))
    native = str(UUID(attempts[0]["conversation_id"]))
    sources = []
    for event in events_of(events, "error.recorded"):
        payload = event["payload"]
        match = re.fullmatch(r"openhands:([a-fA-F0-9-]+):error", payload.get("source_event_id", ""))
        if payload.get("code") == "HW-AGENT-011" and match:
            sources.append(match[1])
    if not sources:
        raise AssertionError("Missing native source of the model diagnostic")
    cluster = server.browser.cluster
    namespace = f"--namespace=hw-s-{server.session}"
    pods = cluster.kubectl(
        "get",
        namespace,
        "pods",
        f"--selector=heartwood.role=worker,heartwood.attempt={attempt}",
        "--output=name",
    )
    names = pods.stdout.split()
    if pods.returncode or len(names) != 1:
        raise AssertionError("Could not identify the denied conversation's worker")
    result = cluster.kubectl(
        "exec",
        "-i",
        namespace,
        names[0],
        "-c",
        "agent",
        "--",
        "/opt/heartwood/.venv/bin/python",
        "-I",
        "-",
        native,
        _MODEL,
        *sources,
        text_input=Path(__file__).with_name("model_denial.py").read_text(),
    )
    if result.returncode or result.stdout != "model-access-denial-verified\n":
        raise AssertionError("Missing model-access denial from the diagnostic's native event")


def _require_interruption(log: str, nonce: str) -> None:
    marker = "interrupt-" + hashlib.sha256(nonce.encode("ascii")).hexdigest()
    if not any(line.endswith(f"_InterruptedAnswerError: {marker}") for line in log.splitlines()):
        raise AssertionError("Missing backend interruption for the submitted request")


def test_an_interrupted_hosted_answer_cannot_request_or_execute_a_tool(
    other: Server, cluster: Cluster
) -> None:
    other.connect(_ROUTE, _MODEL)
    nonce = uuid4().hex
    other.command("chat", {"prompt": f"Record the synthetic value. synthetic:interrupt:{nonce}"})
    _settled_failure(other)
    result = cluster.kubectl("logs", f"--namespace={PLATFORM}", "deployment/synthetic-model")
    if result.returncode:
        raise AssertionError("Could not read the synthetic backend observation")
    _require_interruption(result.stdout, nonce)


def _successful_control(server: Server, prompt: str) -> None:
    _turn(server, prompt)
    server.wait(
        lambda events: [
            event
            for event in events_of(events, "agent_message.emitted")
            if "synthetic model route completed" in json.dumps(event["payload"])
        ]
    )


def test_a_revoked_model_is_refused_in_the_next_hosted_worker(
    other: Server, cluster: Cluster
) -> None:
    other.connect(_ROUTE, _MODEL)
    _successful_control(other, "Record the synthetic value before revocation.")
    session = other.session
    other.stop()
    other.start()
    assert other.session == session
    # The Hub start refreshes the session key. Withdraw its model access before the
    # next conversation launches a worker. The discovery key and saved model still
    # select the original route, so the refusal must occur on inference.
    assert cluster.context.startswith("kind-")
    restrict = """
import json, sqlite3, ssl, sys
from pathlib import Path
import httpx
from heartwood_platform.control.litellm import LiteLLM

config = json.loads(Path('/etc/heartwood-control/configuration.json').read_text())
settings = config['inference']
assert {model['name'] for model in settings['models']} == {'synthetic-coder', 'local-model'}
with sqlite3.connect(f"file:{config['database']}?mode=ro", uri=True) as db:
    (token,) = db.execute('SELECT token FROM session_key WHERE session=? AND settled IS NULL',
                          (sys.argv[1],)).fetchone()
trust = ssl.create_default_context(cafile=config['sessions']['platform_ca_file'])
master = Path(settings['master_key_file']).read_text().strip()
with httpx.Client(base_url=settings['litellm_url'], verify=trust,
                  headers={'Authorization': 'Bearer ' + master}) as client:
    proxy = LiteLLM(client, lambda: master)
    proxy.update_key(token, models=['local-model'])
    info = client.post('/v2/key/info', json={'keys': [token]})
    info.raise_for_status()
    if [row['models'] for row in info.json()['info']] != [['local-model']]:
        sys.exit(1)
print("model-policy-verified")
"""
    result = cluster.kubectl(
        "exec",
        "-i",
        f"--namespace={PLATFORM}",
        "statefulset/heartwood-control",
        "--",
        "/opt/heartwood-platform/.venv/bin/python",
        "-I",
        "-",
        session,
        text_input=restrict,
    )
    if result.returncode or result.stdout != "model-policy-verified\n":
        raise AssertionError("Could not restrict the test session model key")
    other.connect(_ROUTE, _MODEL)
    other.command("chat", {"prompt": "Record the synthetic value after revocation."})
    _require_model_denial(other, _settled_failure(other))
    # Relaunch refreshes the same key's model list. The same model must work again.
    other.stop()
    other.start()
    assert other.session == session
    other.connect(_ROUTE, _MODEL)
    _successful_control(other, "Record the synthetic value after restoring model access.")
