# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""The reference platform end to end, with two synthetic researchers.

Both researchers sign in through the Hub. The first starts the servers of projects demo
and other, the second the server of project demo. In the first researcher's demo session,
a worker on the OpenShell Kubernetes driver follows a script of the synthetic model: it
lists, resolves and reads the project's dataset through the data catalog and the read-only
data volume, runs a batch job and cancels a second one through the job service. Each call
is reviewed in the gateway before it reaches a platform service, and each result before it
reaches the worker. The session's inference reservation is settled when its server is
deleted, and its audit export verifies. The second researcher is refused the first
researcher's server, data and jobs, and neither reaches another project's data or jobs.
"""

from __future__ import annotations

import ast
import base64
import hashlib
import hmac
import json
import re
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from hub import Browser, Server, error_codes, events_of, gateway_pods, pending_group
from identity import (
    FIRST,
    PLATFORM,
    SECOND,
    SESSIONS,
    exchange,
    probe,
    request,
    secret,
    sign_in,
)
from jobs import TASK_IMAGE, store
from kube import Cluster

_ROUTE = "platform-route"
_MODEL = "synthetic-coder"
_SCRIPT = "synthetic:script"
_DATA = "https://heartwood-data.heartwood-platform.svc.cluster.local:8443"
_JOBS = "https://heartwood-jobs.heartwood-platform.svc.cluster.local:8443"
# The cost of one request to the synthetic model at the chart's prices.
_REQUEST_COST = 0.00125
_COHORT = "synthetic-cohort.participants.csv"
_PANEL = "synthetic-panel.panel.csv"
_COHORT_FILE = (
    Path(__file__).resolve().parents[2]
    / "chart/heartwood-platform/files/datasets/synthetic-cohort/participants.csv"
)
_COPY = "participants-head.txt"
# The command copies the first lines of the dataset and records that /data is read-only.
_READ = (
    f"{{ head -n 3 /data/synthetic-cohort/participants.csv;"
    f" touch /data/synthetic-cohort/written 2>&1 || true; }} > /workspace/{_COPY}"
)
_WAITING = ["finish", {"message": "Waiting for the reviewed result."}]


@dataclass
class Platform:
    """The servers of both researchers."""

    demo: Server
    other: Server
    second: Server
    # The outside operations that a review returned, so each call is reviewed once.
    reviewed: set[str] = field(default_factory=set)


def _start(server: Server) -> None:
    server.browser.sign_in_to_hub()
    server.start()


@pytest.fixture(scope="module")
def platform(cluster: Cluster, probe_image: str) -> Iterator[Platform]:
    with (
        probe(cluster, "hw-e2e-first", probe_image) as first_pod,
        probe(cluster, "hw-e2e-second", probe_image) as second_pod,
    ):
        first = Browser(cluster, first_pod, "researcher-one")
        second = Browser(cluster, second_pod, "researcher-two")
        found = Platform(
            Server(first, FIRST, "demo"),
            Server(first, FIRST, "other"),
            Server(second, SECOND, "demo"),
        )
        try:
            for server in (found.demo, found.other, found.second):
                _start(server)
            # The first researcher's demo session uses the model route and both services.
            found.demo.connect(_ROUTE, _MODEL)
            _enable(
                found.demo, "catalog", _DATA, ("list_datasets", "list_objects", "resolve_object")
            )
            _enable(found.demo, "jobs", _JOBS, ("create_task", "cancel_task"))
            # Enabled tools reach the conversations created afterwards.
            found.demo.create_conversation()
            yield found
        finally:
            for server in (found.demo, found.other, found.second):
                server.remove()


def _researcher_actor(cluster: Cluster, subject: str) -> str:
    """The actor that the gateways record for a subject, from the deployment's actor key."""
    result = cluster.kubectl(
        "get", f"--namespace={PLATFORM}", "configmap", "heartwood-hub", "--output=json"
    )
    assert result.returncode == 0, result.stderr
    issuer = json.loads(json.loads(result.stdout)["data"]["settings.json"])["issuer"]
    key = secret(cluster, PLATFORM, "heartwood-actor-key", "actor-key").removesuffix("\n")
    digest = hmac.new(key.encode(), f"{issuer}\0{subject}".encode(), hashlib.sha256)
    return f"oidc-{digest.hexdigest()}"


def test_each_researcher_starts_one_server_per_project_with_its_launch_context(
    platform: Platform, cluster: Cluster
) -> None:
    servers = (platform.demo, platform.other, platform.second)
    assert len({server.session for server in servers}) == 3
    for server in servers:
        pod = server.pod()
        labels = pod["metadata"]["labels"]
        assert labels["hub.jupyter.org/username"] == server.user
        assert labels["hub.jupyter.org/servername"] == server.project
        # The gateway reads the platform launch context of its session.
        context = json.loads(
            secret(cluster, SESSIONS, f"hw-gw-{server.session}", "launch-context.json")
        )
        assert (context["project_id"], context["project_context_id"]) == (
            server.project,
            server.session,
        )
        status, planning = server.api("POST", "/project/planning/refresh")
        assert status == 200, planning
        assert planning["services"]["budgets"] == "available", planning
        budgets = json.dumps(planning)
        assert server.session in budgets
        assert server.project in budgets


def _enable(server: Server, server_id: str, url: str, tools: tuple[str, ...]) -> None:
    """Add a platform service as a research service and enable the given tools."""
    status, answer = server.api(
        "POST",
        "/settings/mcp-servers",
        {"server_id": server_id, "label": server_id, "url": f"{url}/mcp"},
    )
    assert status == 200, answer
    status, tested = server.api("POST", f"/settings/mcp-servers/{server_id}/test", {})
    assert status == 200, tested
    digests = {tool["name"]: tool["sha256"] for tool in tested["tools"]}
    chosen = [{"name": name, "sha256": digests[name]} for name in tools]
    status, answer = server.api(
        "PUT", f"/settings/mcp-servers/{server_id}/tools", {"tools": chosen}
    )
    assert status == 200, answer
    status, answer = server.api(
        "PUT", f"/settings/mcp-servers/{server_id}/enabled", {"enabled": True}
    )
    assert status == 200, answer


def _records(events: list[dict[str, Any]], kind: str) -> dict[str, dict[str, Any]]:
    """The latest record of each outside operation or result release, by operation."""
    latest: dict[str, dict[str, Any]] = {}
    for event in events_of(events, kind):
        record = event["payload"]["record"]
        latest[record["proposal"]["operation_id"]] = record
    return latest


def _operations(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return _records(events, "external_action.recorded")


def _status(record: dict[str, Any]) -> str | None:
    observation = record.get("observation")
    return None if observation is None else str(observation["operation"]["status"])


def _terminal(record: dict[str, Any]) -> bool:
    return record["phase"] in {"denied", "expired", "withdrawn"} or _status(record) in {
        "completed",
        "failed",
        "denied",
        "cancelled",
    }


def _of_tool(record: dict[str, Any], tool: str) -> bool:
    """Whether an outside operation calls an MCP tool of that name, as the gateway names it."""
    name = record["proposal"]["request"]["tool_name"]
    return re.fullmatch(rf"[a-z0-9_]+__{re.escape(tool)}_[0-9a-f]{{8}}", name) is not None


def _running(events: list[dict[str, Any]]) -> bool:
    """Whether the worker's last reported state is a running turn."""
    states = [event["payload"]["status"] for event in events_of(events, "agent.lifecycle.updated")]
    return bool(states) and states[-1] == "running"


def _review(
    server: Server,
    reviewed: set[str],
    tool: str,
    done: Callable[[dict[str, Any]], bool],
    timeout: float = 420,
    *,
    released: bool = True,
) -> dict[str, Any]:
    """Approve the worker's next call of a tool, its outside operation and its result release.

    Every decision waits for the researcher, who approves what the gateway shows as
    pending. Returns the operation's record, with the kinds of the approved actions,
    once ``done`` holds for it and its result release is decided, or once ``done`` holds
    for an operation that releases nothing. Without ``released``, ``done`` alone decides,
    as for an operation that still runs.
    """
    deadline = time.monotonic() + timeout
    approved: list[str] = []
    while time.monotonic() < deadline:
        events, projection = server.snapshot()
        # The earliest call of the tool that no earlier review returned.
        found = [
            (key, record)
            for key, record in _operations(events).items()
            if key not in reviewed and _of_tool(record, tool)
        ]
        if found:
            key, record = found[0]
            release = _records(events, "external_result.recorded").get(key)
            decided = release is not None and release["phase"] != "proposed"
            if done(record) and (not released or decided or record["phase"] == "denied"):
                reviewed.add(key)
                return {**record, "approved": approved}
        # The worker's own call first; the gateway's reviews of the outside operation and
        # of its result once the worker's turn has ended, as a release requires.
        call = pending_group(events)
        pending = projection.get("pendingApproval")
        if call is not None:
            server.command("approve", {"target_type": "action-set", "target_id": call})
            approved.append("worker-call")
            continue
        if pending and not _running(events):
            server.command(
                "approve", {"target_type": "action-set", "target_id": pending["groupId"]}
            )
            approved.extend(str(action["details"]["kind"]) for action in pending["actions"])
            continue
        time.sleep(3)
    raise AssertionError(_story(server.events()))


def _story(events: list[dict[str, Any]]) -> str:
    """The conversation as a list of event kinds, with phases and codes."""
    lines = []
    for event in events:
        payload = event["payload"]
        record = payload.get("record") or {}
        detail = (
            payload.get("code")
            or payload.get("phase")
            or payload.get("status")
            or record.get("phase")
            or ""
        )
        lines.append(f"{event['sequence']} {event['kind']} {detail} {payload.get('reason', '')}")
    return "\n".join(lines)


def _chat(server: Server, steps: list[list[Any]]) -> int:
    """Send a prompt with a script of the synthetic model; return the events before it."""
    before = len(server.events())
    server.command(
        "chat", {"prompt": f"Continue the synthetic task. {_SCRIPT} {json.dumps(steps)}"}
    )
    return before


def test_the_worker_reads_the_dataset_through_the_catalog_and_its_data_volume(
    platform: Platform, cluster: Cluster
) -> None:
    demo = platform.demo
    _chat(
        demo,
        [
            ["list_datasets", {}],
            _WAITING,
            ["list_objects", {"dataset_id": "synthetic-cohort"}],
            _WAITING,
            ["resolve_object", {"object_id": _COHORT}],
            _WAITING,
            # An object of the researcher's other project, which the catalog denies here.
            ["resolve_object", {"object_id": _PANEL}],
            _WAITING,
            ["terminal", {"command": _READ}],
            ["finish", {"message": "The synthetic catalog task completed."}],
        ],
    )
    completed = lambda record: _terminal(record)  # noqa: E731
    listed = _review(demo, platform.reviewed, "list_datasets", completed)
    objects = _review(demo, platform.reviewed, "list_objects", completed)
    resolved = _review(demo, platform.reviewed, "resolve_object", completed)
    refused = _review(demo, platform.reviewed, "resolve_object", completed)
    for record in (listed, objects, resolved, refused):
        assert record["proposal"]["request"]["service_id"] == "heartwood-data"
        assert record["observation"]["operation"]["project_context_id"] == demo.session
    datasets = listed["observation"]["operation"]["result"]["datasets"]
    assert [item["id"] for item in datasets] == ["synthetic-cohort"]
    paths = {item["path"] for item in objects["observation"]["operation"]["result"]["objects"]}
    assert "/data/synthetic-cohort/participants.csv" in paths
    found = resolved["observation"]["operation"]["result"]["object"]
    assert found["path"] == "/data/synthetic-cohort/participants.csv"
    assert refused["observation"]["operation"]["status"] == "denied"
    assert refused["observation"]["operation"].get("result") is None

    # The worker runs on the Kubernetes driver in the session namespace and reads the
    # copy of the dataset on its read-only data volume.
    group = demo.wait(pending_group)
    requested = [
        event
        for event in events_of(demo.events(), "confirmation.requested")
        if event["payload"]["request"]["group_id"] == group
    ]
    assert _READ in json.dumps(requested)
    namespace = f"hw-s-{demo.session}"
    pods = cluster.kubectl(
        "get",
        f"--namespace={namespace}",
        "pods",
        "--selector=heartwood.role=worker",
        "--output=json",
    )
    assert pods.returncode == 0, pods.stderr
    (worker,) = json.loads(pods.stdout)["items"]
    demo.command("approve", {"target_type": "action-set", "target_id": group})
    calls = set(re.findall(r'"tool_call_id": "([^"]+)"', json.dumps(requested)))
    (execution,) = demo.wait(
        lambda events: [
            event
            for event in events_of(events, "tool.execution.recorded")
            if event["payload"]["tool_call_id"] in calls
        ]
    )
    assert execution["payload"]["exit_code"] == 0
    # The copy that the approved command wrote in the worker's workspace.
    copied = cluster.kubectl(
        "exec",
        f"--namespace={namespace}",
        worker["metadata"]["name"],
        "-c",
        "agent",
        "--",
        "cat",
        f"/workspace/{_COPY}",
    )
    assert copied.returncode == 0, copied.stderr
    *lines, written = copied.stdout.splitlines()
    assert lines == _COHORT_FILE.read_text(encoding="utf-8").splitlines()[:3]
    assert "Read-only file system" in written
    demo.wait(
        lambda events: [
            event
            for event in events_of(events, "agent_message.emitted")
            if "synthetic catalog task completed" in json.dumps(event["payload"])
        ]
    )
    assert not error_codes(demo.events())


def _count_task() -> dict[str, Any]:
    return {
        "name": "count participants",
        "image": TASK_IMAGE,
        "command": ["sh", "-c", "wc -l < /inputs/participants.csv > /outputs/count.txt"],
        "resources": {"cpu_cores": 1, "ram_gb": 0.25, "disk_gb": 1, "max_runtime_seconds": 300},
        "inputs": [{"object_id": _COHORT, "path": "/inputs/participants.csv"}],
        "outputs": [{"name": "count.txt", "path": "/outputs/count.txt"}],
    }


@dataclass
class Jobs:
    """The operations of the first researcher's batch job and of the cancelled job."""

    completed: dict[str, Any]
    cancelled: dict[str, Any]
    cancellation: dict[str, Any]


@pytest.fixture(scope="module")
def jobs(platform: Platform) -> Jobs:
    demo = platform.demo
    _chat(demo, [["create_task", _count_task()], ["finish", {"message": "Submitted."}]])
    completed = _review(
        demo,
        platform.reviewed,
        "create_task",
        _terminal,
        timeout=600,
    )
    assert _status(completed) == "completed", _story(demo.events())
    long = {
        "name": "wait",
        "image": TASK_IMAGE,
        "command": ["sleep", "600"],
        "resources": {"cpu_cores": 1, "ram_gb": 0.25, "max_runtime_seconds": 900},
    }
    _chat(demo, [["create_task", long], ["finish", {"message": "Submitted."}]])
    running = _review(
        demo,
        platform.reviewed,
        "create_task",
        lambda record: _status(record) == "running",
        timeout=600,
        released=False,
    )
    target = running["proposal"]["operation_id"]
    _chat(
        demo,
        [["cancel_task", {"target_operation_id": target}], ["finish", {"message": "Cancelled."}]],
    )
    cancellation = _review(demo, platform.reviewed, "cancel_task", _terminal, timeout=600)
    cancelled = demo.wait(
        lambda events: next(
            (record for record in [_operations(events)[target]] if _terminal(record)), None
        ),
        timeout=300,
    )
    return Jobs(completed, cancelled, cancellation)


def test_a_batch_job_runs_through_the_reviewed_operation_path(
    platform: Platform, jobs: Jobs, cluster: Cluster
) -> None:
    record = jobs.completed
    request = record["proposal"]["request"]
    assert request["service_id"] == "heartwood-jobs"
    review = request["operation"]
    assert review["kind"] == "submit"
    proposal = review["proposal"]
    assert _COHORT in json.dumps(proposal["data_scope"])
    assert f"of budget {platform.demo.project}-compute" in json.dumps(proposal)
    # The researcher approved the call, the submission and the release of the result.
    assert record["approved"] == ["worker-call", "outside-operation", "outside-result"]
    operation = record["observation"]["operation"]
    assert operation["status"] == "completed"
    assert operation["project_context_id"] == platform.demo.session
    expected = len(_COHORT_FILE.read_text(encoding="utf-8").splitlines())
    with store(cluster) as objects:
        result = objects.get("hw-project-demo", f"results/{operation['operation_id']}/count.txt")
    assert result == f"{expected}\n".encode()


def test_a_second_job_is_cancelled_through_its_own_reviewed_operation(jobs: Jobs) -> None:
    assert jobs.cancellation["proposal"]["request"]["operation"]["kind"] == "cancel"
    assert jobs.cancellation["observation"]["operation"]["status"] == "completed"
    assert jobs.cancelled["observation"]["operation"]["status"] == "cancelled"


def _control_rows(cluster: Cluster, holder: str) -> list[tuple[str, str, str]]:
    query = (
        "import sqlite3,sys;c=sqlite3.connect('/var/lib/heartwood-control/control.sqlite3');"
        'print(c.execute("SELECT state, amount, incurred FROM reservation WHERE holder = ?",'
        "(sys.argv[1],)).fetchall())"
    )
    result = cluster.kubectl(
        "exec",
        f"--namespace={PLATFORM}",
        "statefulset/heartwood-control",
        "--",
        "/opt/heartwood-platform/.venv/bin/python",
        "-I",
        "-c",
        query,
        holder,
    )
    assert result.returncode == 0, result.stderr
    rows: list[tuple[str, str, str]] = ast.literal_eval(result.stdout.strip())
    return rows


def _tokens(cluster: Cluster, server: Server, username: str, service: str) -> str:
    """Sign a researcher in at the project client of a session; exchange for one service."""
    client_id = f"hw-proj-{server.project}"
    client_secret = secret(cluster, SESSIONS, f"hw-gw-{server.session}", "keycloak-client-secret")
    redirect = (
        f"https://proxy-public.heartwood-platform.svc.cluster.local{server.base}/oauth_callback"
    )
    signed_in = sign_in(
        cluster,
        server.browser.pod,
        username=username,
        redirect_uri=redirect,
        client_id=client_id,
        client_secret=client_secret,
    )
    status, exchanged = exchange(
        cluster,
        server.browser.pod,
        client_id=client_id,
        client_secret=client_secret,
        subject_token=signed_in["access_token"],
        service=service,
        audience=f"heartwood-{service}",
    )
    assert status == 200, exchanged.get("error_description")
    token = exchanged["access_token"]
    assert isinstance(token, str)
    return token


def _tool(
    cluster: Cluster,
    pod: str,
    url: str,
    token: str,
    tool: str,
    **arguments: Any,  # noqa: ANN401
) -> dict[str, Any]:
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }
    status, answer = request(cluster, pod, "POST", f"{url}/mcp", token, body)
    assert status == 200, answer
    result = answer["result"]
    assert isinstance(result, dict)
    return result


def test_the_second_researcher_is_refused_the_first_researchers_server_data_and_jobs(
    platform: Platform, jobs: Jobs, cluster: Cluster
) -> None:
    second = platform.second
    browser = second.browser
    # The first researcher's servers, through the Hub's API and at the gateway.
    for project in ("demo", "other"):
        status, _ = browser.hub_api("POST", f"/users/{platform.demo.user}/servers/{project}")
        assert status in {403, 404}
    status, _, body = browser.sign_in_to_gateway(platform.demo.base)
    assert status == 403
    assert "HW-INGRESS-004" in body
    status, answer = browser.api(platform.demo.base, "GET", "/sessions", None)
    assert status == 401, answer

    pod = browser.pod
    data = _tokens(cluster, second, "researcher-two", "data")
    # The catalog of the second researcher's session lists only project demo's dataset,
    # and neither the catalog nor the DRS endpoints resolve the dataset of project other.
    listed = _tool(
        cluster,
        pod,
        _DATA,
        data,
        "list_datasets",
        project_id="demo",
        operation_id=str(uuid.uuid4()),
    )
    assert listed["isError"] is False, listed
    datasets = listed["structuredContent"]["result"]["datasets"]
    assert [item["id"] for item in datasets] == ["synthetic-cohort"]
    denied = _tool(
        cluster,
        pod,
        _DATA,
        data,
        "resolve_object",
        project_id="demo",
        operation_id=str(uuid.uuid4()),
        object_id=_PANEL,
    )
    assert denied["structuredContent"]["status"] == "denied", denied
    status, _ = request(cluster, pod, "GET", f"{_DATA}/ga4gh/drs/v1/objects/{_PANEL}", data)
    assert status == 404
    other = _tool(
        cluster,
        pod,
        _DATA,
        data,
        "list_datasets",
        project_id="other",
        operation_id=str(uuid.uuid4()),
    )
    assert other["isError"] is True
    assert "not the project of the token" in other["content"][0]["text"]
    # Signed in at the client of project other, of which only the first researcher is a
    # member, the second researcher is refused the project's objects.
    outsider = _tokens(cluster, platform.other, "researcher-two", "data")
    status, _ = request(cluster, pod, "GET", f"{_DATA}/ga4gh/drs/v1/objects/{_PANEL}", outsider)
    assert status == 403

    # The first researcher's jobs do not exist for the second researcher, and cannot be
    # cancelled by them.
    token = _tokens(cluster, second, "researcher-two", "jobs")
    target = jobs.completed["proposal"]["operation_id"]
    observed = _tool(cluster, pod, _JOBS, token, "get_task", project_id="demo", operation_id=target)
    assert observed["isError"] is True, observed
    assert "operation not found" in observed["content"][0]["text"]
    cancel = _tool(
        cluster,
        pod,
        _JOBS,
        token,
        "cancel_task",
        project_id="demo",
        operation_id=str(uuid.uuid4()),
        target_operation_id=jobs.cancelled["proposal"]["operation_id"],
    )
    assert cancel["isError"] is True, cancel
    assert "operation not found" in cancel["content"][0]["text"]


def test_another_projects_dataset_and_jobs_are_refused(
    platform: Platform, jobs: Jobs, cluster: Cluster
) -> None:
    demo, pod = platform.demo, platform.demo.browser.pod
    # The demo session's tokens name only project demo.
    data = _tokens(cluster, demo, "researcher-one", "data")
    status, _ = request(cluster, pod, "GET", f"{_DATA}/ga4gh/drs/v1/objects/{_PANEL}", data)
    assert status == 404
    token = _tokens(cluster, demo, "researcher-one", "jobs")
    refused = _tool(
        cluster,
        pod,
        _JOBS,
        token,
        "create_task",
        project_id="other",
        operation_id=str(uuid.uuid4()),
        **_count_task(),
    )
    assert refused["isError"] is True
    assert "not the project of the token" in refused["content"][0]["text"]
    # A job of project demo does not exist in the session of project other.
    other = _tokens(cluster, platform.other, "researcher-one", "jobs")
    observed = _tool(
        cluster,
        pod,
        _JOBS,
        other,
        "get_task",
        project_id="other",
        operation_id=jobs.completed["proposal"]["operation_id"],
    )
    assert observed["structuredContent"]["status"] == "failed", observed
    assert observed["structuredContent"]["limitations"] == [
        "The service never received this operation."
    ]


def test_the_audit_export_verifies_and_names_the_keyed_actor(
    platform: Platform, jobs: Jobs, cluster: Cluster
) -> None:
    del jobs
    demo = platform.demo
    demo.command("audit.export", {})
    status, export = demo.api("GET", f"/sessions/{demo.conversation}/audit-export")
    assert status == 200, export
    content = export["content"]
    assert isinstance(content, str)
    # The application's verifier in the gateway pod checks the chain of the export.
    verify = (
        "import hashlib,sys;from heartwood.audit import verify_audit_jsonl;"
        "data=sys.stdin.buffer.read();events,v=verify_audit_jsonl(data.decode());"
        "print(hashlib.sha256(data).hexdigest(),v.event_count,v.terminal_event_hash)"
    )
    result = cluster.kubectl(
        "exec",
        "--stdin",
        f"--namespace={SESSIONS}",
        demo.pod()["metadata"]["name"],
        "-c",
        "notebook",
        "--",
        "/opt/heartwood/.venv/bin/python",
        "-I",
        "-c",
        verify,
        text_input=content,
    )
    assert result.returncode == 0, result.stderr
    digest, count, terminal = result.stdout.split()
    assert digest == hashlib.sha256(content.encode()).hexdigest()
    records = [json.loads(line) for line in content.splitlines()]
    assert int(count) == len(records)
    assert terminal == records[-1]["event_hash"]
    # Commands record the researcher's keyed actor, never the subject or the user name.
    actor = _researcher_actor(cluster, FIRST)
    assert actor in content
    for raw in (FIRST, "researcher-one"):
        assert raw not in content
        assert base64.b64encode(raw.encode()).decode() not in content


def test_deleting_the_server_settles_the_sessions_inference_reservation(
    platform: Platform, cluster: Cluster
) -> None:
    demo = platform.demo
    ((state, amount, _),) = _control_rows(cluster, f"session:{demo.session}")
    assert state == "held"
    assert float(amount) > 0
    demo.remove()
    deadline = time.monotonic() + 300
    rows = _control_rows(cluster, f"session:{demo.session}")
    while rows[0][0] != "settled" and time.monotonic() < deadline:
        time.sleep(5)
        rows = _control_rows(cluster, f"session:{demo.session}")
    ((state, _, incurred),) = rows
    assert state == "settled"
    # Every request to the synthetic model costs the same, and the session made some.
    requests = float(incurred) / _REQUEST_COST
    assert requests >= 10
    assert requests == pytest.approx(round(requests))
    assert not gateway_pods(cluster, demo.session)
