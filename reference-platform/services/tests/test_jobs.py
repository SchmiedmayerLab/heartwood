# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""The job service over MCP, against a deterministic TES executor."""

from __future__ import annotations

import itertools
import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fakes import SigningKey
from heartwood_platform.common.tokens import ResearcherTokens
from heartwood_platform.jobs.app import MCP_PATH, create_jobs_app
from heartwood_platform.jobs.objects import DRS_ID, ObjectNotFoundError
from heartwood_platform.jobs.platform import (
    BudgetDeniedError,
    JobUsage,
    NoSessionError,
    NotAMemberError,
)
from heartwood_platform.jobs.records import Records
from heartwood_platform.jobs.service import (
    OPERATION_TAG,
    JobService,
    ProjectPolicy,
    ServiceSettings,
)
from heartwood_platform.jobs.tes import (
    TERMINAL,
    TaskFilter,
    TaskNotFoundError,
    TesExecutor,
    TesExecutorLog,
    TesServiceInfo,
    TesTask,
    TesTaskList,
    TesTaskLog,
    TesTaskView,
    View,
)
from starlette.testclient import TestClient

from heartwood.schemas import (
    PlatformOperationCapabilities,
    PlatformOperationObservation,
    PlatformOperationProposal,
)

ISSUER = "https://keycloak.example.test/realms/heartwood"
AUDIENCE = "heartwood-jobs"
ALICE = "0b9f5d1e-7c64-4f0e-9d53-6f6f3f0f1a01"
BOB = "0b9f5d1e-7c64-4f0e-9d53-6f6f3f0f1a02"
CAROL = "0b9f5d1e-7c64-4f0e-9d53-6f6f3f0f1a03"
IMAGE = "registry.example.test/task@sha256:" + "a" * 64
_ACCEPT = {"Accept": "application/json, text/event-stream"}
_START = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
_DEADLINE = 600


@dataclass
class _Task:
    task: TesTask
    state: str = "QUEUED"
    logs: tuple[TesTaskLog, ...] = ()
    created: datetime = _START


@dataclass
class FakeExecutor:
    """A TES executor in memory, with paging, lost replies and refusals."""

    tasks: dict[str, _Task] = field(default_factory=dict)
    cancelled: list[str] = field(default_factory=list)
    # "lost": the task is created and the reply times out; "refused": HTTP 400;
    # "unreachable": the connection fails before anything is created.
    failure: str | None = None
    page_size: int = 2
    clock: Callable[[], datetime] = lambda: _START
    _ids: Iterator[int] = field(default_factory=itertools.count)

    def service_info(self) -> TesServiceInfo:
        return TesServiceInfo(id="fake", name="fake executor")

    def create(self, task: TesTask) -> str:
        if self.failure == "unreachable":
            raise httpx.ConnectError("unreachable")
        if self.failure == "refused":
            request = httpx.Request("POST", "https://executor/v1/tasks")
            raise httpx.HTTPStatusError(
                "refused", request=request, response=httpx.Response(400, request=request)
            )
        identifier = f"task-{next(self._ids):04d}"
        self.tasks[identifier] = _Task(task, created=self.clock())
        if self.failure == "lost":
            raise httpx.ReadTimeout("lost reply")
        return identifier

    def get(self, task_id: str, view: View = "BASIC") -> TesTaskView:
        found = self.tasks.get(task_id)
        if found is None:
            raise TaskNotFoundError
        return TesTaskView(
            id=task_id,
            state=found.state,  # type: ignore[arg-type]
            tags=found.task.tags,
            creation_time=found.created.isoformat(),
            logs=found.logs if view == "FULL" else None,
        )

    def list(
        self, page_token: str | None = None, view: View = "BASIC", filters: TaskFilter | None = None
    ) -> TesTaskList:
        chosen = filters or TaskFilter()
        identifiers = [
            identifier
            for identifier, item in sorted(self.tasks.items())
            if (chosen.state is None or item.state == chosen.state)
            and (chosen.tag_key is None or item.task.tags.get(chosen.tag_key) == chosen.tag_value)
        ]
        # Like Funnel, a page token is the last identifier of the previous page.
        remaining = [item for item in identifiers if page_token is None or item > page_token]
        page = remaining[: self.page_size]
        return TesTaskList(
            tasks=tuple(self.get(item, view) for item in page),
            next_page_token=page[-1] if len(remaining) > self.page_size else None,
        )

    def cancel(self, task_id: str) -> None:
        found = self.tasks.get(task_id)
        if found is None:
            raise TaskNotFoundError
        self.cancelled.append(task_id)
        if found.state not in TERMINAL:
            found.state = "CANCELED"

    def advance(self, task_id: str, state: str, *logs: TesTaskLog) -> None:
        self.tasks[task_id].state = state
        self.tasks[task_id].logs = logs


@dataclass
class FakeObjects:
    """The objects of each project, and the expiry of each URL handed out."""

    objects: dict[str, set[str]] = field(
        default_factory=lambda: {"demo": {"cohort.data.csv"}, "other": {"panel.data.csv"}}
    )
    expiries: list[tuple[str, int]] = field(default_factory=list)

    def input_url(self, project: str, object_id: str, expires: int) -> str:
        if DRS_ID.fullmatch(object_id) is None or object_id not in self.objects[project]:
            raise ObjectNotFoundError
        self.expiries.append(("input", expires))
        return f"https://store.example.test/{project}/{object_id}?signature=input"

    def output_url(self, project: str, operation_id: str, name: str, expires: int) -> str:
        self.expiries.append(("output", expires))
        return f"https://store.example.test/{project}/results/{operation_id}/{name}?signature=out"

    def output_location(self, project: str, operation_id: str) -> str:
        return f"s3://hw-project-{project}/results/{operation_id}/"


@dataclass
class FakeBudgets:
    """One compute allocation per project; usage is priced at one unit per core-second."""

    # Whether the control service fails after it reserved.
    unavailable: bool = False
    available: dict[str, int] = field(default_factory=lambda: {"demo": 100_000, "other": 0})
    held: dict[str, tuple[str, int]] = field(default_factory=dict)
    settled: dict[str, JobUsage] = field(default_factory=dict)

    def reserve(self, project: str, holder: str, usage: JobUsage) -> str:
        if holder in self.held:
            return holder
        amount = usage.cpu_cores * usage.seconds
        if amount > self.available[project]:
            raise BudgetDeniedError("Budget demo-compute has no availability.")
        self.available[project] -= amount
        self.held[holder] = (project, amount)
        if self.unavailable:
            raise httpx.ReadTimeout("lost reply")
        return holder

    def settle(self, holder: str, usage: JobUsage) -> None:
        if holder not in self.held:
            return
        project, amount = self.held.pop(holder)
        self.available[project] += amount - usage.cpu_cores * usage.seconds
        self.settled[holder] = usage


class FakeContexts:
    """Alice is a member of demo and other, Bob and Carol of demo; Bob has no session."""

    def context(self, subject: str, project: str) -> str:
        members = {"demo": {ALICE, BOB, CAROL}, "other": {ALICE}}
        if subject not in members.get(project, set()):
            raise NotAMemberError
        if subject == BOB:
            raise NoSessionError
        return f"session-{project}"


class Jobs:
    """The job service with two projects; ``other`` holds a GPU entitlement and no budget."""

    def __init__(self, directory: Path, *, budgets: bool = True) -> None:
        self.now = _START
        self.key = SigningKey()
        self.executors = {"demo": FakeExecutor(), "other": FakeExecutor()}
        self.objects = FakeObjects()
        self.budgets = FakeBudgets() if budgets else None
        policy = ProjectPolicy(
            gpu=False, max_cpu_cores=4, max_ram_gb=8, max_disk_gb=10, max_runtime_seconds=7200
        )
        for executor in self.executors.values():
            executor.clock = lambda: self.now
        executors: dict[str, TesExecutor] = dict(self.executors)
        self.service = JobService(
            Records(directory / "jobs.sqlite3"),
            executors,
            {"demo": policy, "other": ProjectPolicy(**{**policy.__dict__, "gpu": True})},
            self.objects,
            self.budgets,
            ServiceSettings(
                images=frozenset({IMAGE}),
                start_deadline_seconds=_DEADLINE,
                cpu_profile="synthetic-batch-small",
                gpu_profile="synthetic-batch-gpu",
                drs_host="heartwood-data.example.test",
            ),
            clock=lambda: self.now,
        )
        self.app = create_jobs_app(
            self.service, ResearcherTokens(ISSUER, AUDIENCE, self.key.key_set), FakeContexts()
        )

    def headers(self, subject: str = ALICE, project: str = "demo") -> dict[str, str]:
        token = self.key.token(iss=ISSUER, aud=AUDIENCE, sub=subject, azp=f"hw-proj-{project}")
        return {"Authorization": f"Bearer {token}", **_ACCEPT}

    def call(
        self,
        client: TestClient,
        tool: str,
        arguments: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        response = client.post(
            MCP_PATH,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool, "arguments": arguments},
            },
            headers=headers or self.headers(),
        )
        assert response.status_code == 200, response.text
        result = response.json()["result"]
        assert isinstance(result, dict)
        return result

    def observe(
        self,
        client: TestClient,
        tool: str,
        arguments: dict[str, Any],
        **kwargs: Any,  # noqa: ANN401
    ) -> PlatformOperationObservation:
        result = self.call(client, tool, arguments, **kwargs)
        assert result["isError"] is False, result
        return PlatformOperationObservation.model_validate_json(
            json.dumps(result["structuredContent"])
        )

    def error(self, client: TestClient, tool: str, arguments: dict[str, Any], **kwargs: Any) -> str:  # noqa: ANN401
        result = self.call(client, tool, arguments, **kwargs)
        assert result["isError"] is True, result
        return str(result["content"][0]["text"])


def task(operation_id: str = "op-1", project: str = "demo", **changes: Any) -> dict[str, Any]:  # noqa: ANN401
    return {
        "project_id": project,
        "operation_id": operation_id,
        "name": "count rows",
        "image": IMAGE,
        "command": ["sh", "-c", "wc -l < /inputs/cohort.csv > /outputs/count.txt"],
        "resources": {"cpu_cores": 2, "ram_gb": 1, "max_runtime_seconds": 600},
        "inputs": [{"object_id": "cohort.data.csv", "path": "/inputs/cohort.csv"}],
        "outputs": [{"name": "count.txt", "path": "/outputs/count.txt"}],
        **changes,
    }


@pytest.fixture
def jobs(tmp_path: Path) -> Iterator[tuple[Jobs, TestClient]]:
    service = Jobs(tmp_path)
    with TestClient(service.app) as client:
        yield service, client


def test_tools_declare_the_four_annotations(jobs: tuple[Jobs, TestClient]) -> None:
    service, client = jobs
    response = client.post(
        MCP_PATH,
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers=service.headers(),
    )
    tools = {tool["name"]: tool["annotations"] for tool in response.json()["result"]["tools"]}
    hints = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")
    assert {name: tuple(value[hint] for hint in hints) for name, value in tools.items()} == {
        "describe_operations": (True, False, True, False),
        "propose_task": (True, False, True, False),
        "get_task": (True, False, True, False),
        "create_task": (False, False, True, False),
        "cancel_task": (False, True, True, False),
    }


def test_discovery_reports_operations_and_the_gpu_entitlement(
    jobs: tuple[Jobs, TestClient],
) -> None:
    service, client = jobs
    for project, status in (("demo", "not-permitted"), ("other", "available")):
        result = service.call(
            client,
            "describe_operations",
            {"project_id": project},
            headers=service.headers(project=project),
        )
        declared = PlatformOperationCapabilities.model_validate_json(
            json.dumps(result["structuredContent"])
        )
        assert [item.operation for item in declared.operations] == [
            "submit",
            "status",
            "cancel",
            "proposal",
        ]
        assert declared.optional_capabilities[0].status == status


def test_a_proposal_shows_limits_data_scope_and_destination_and_submits_nothing(
    jobs: tuple[Jobs, TestClient],
) -> None:
    service, client = jobs
    arguments = {
        key: value for key, value in task().items() if key not in {"project_id", "operation_id"}
    }
    result = service.call(
        client,
        "propose_task",
        {"project_id": "demo", "tool": "create_task", "arguments": arguments},
    )
    proposal = PlatformOperationProposal.model_validate_json(
        json.dumps(result["structuredContent"])
    )
    assert proposal.runtime_profile_ref == "synthetic-batch-small"
    assert {item.metric: item.maximum for item in proposal.resource_limits} == {
        "cpu": "2",
        "memory": "1",
        "disk": "1",
        "duration": "600",
    }
    assert (
        proposal.data_scope == "Project objects drs://heartwood-data.example.test/cohort.data.csv"
    )
    assert "s3://hw-project-demo/results/" in proposal.destination
    assert service.executors["demo"].tasks == {}
    # A GPU request outside the entitlement is described with its denial.
    arguments["resources"] = {"gpu_count": 1, "gpu_type": "nvidia-l4"}
    result = service.call(
        client,
        "propose_task",
        {"project_id": "demo", "tool": "create_task", "arguments": arguments},
    )
    proposal = PlatformOperationProposal.model_validate_json(
        json.dumps(result["structuredContent"])
    )
    assert proposal.runtime_profile_ref == "synthetic-batch-gpu"
    assert any("no GPU entitlement" in item for item in proposal.limitations)


def test_a_repeated_operation_identifier_returns_the_existing_task(
    jobs: tuple[Jobs, TestClient],
) -> None:
    service, client = jobs
    first = service.observe(client, "create_task", task())
    second = service.observe(client, "create_task", task())

    assert first.status == second.status == "queued"
    assert first.backend_operation_ref == second.backend_operation_ref == "task-0000"
    assert first.project_context_id == "session-demo"
    assert len(service.executors["demo"].tasks) == 1
    created = service.executors["demo"].tasks["task-0000"].task
    assert created.tags == {OPERATION_TAG: "op-1"}
    assert [item.path for item in created.inputs] == ["/inputs/cohort.csv"]
    # Different content under the same identifier is a conflict and starts nothing.
    changed = task(command=["true"])
    assert "conflict" in service.error(client, "create_task", changed)
    assert len(service.executors["demo"].tasks) == 1


def test_urls_expire_shortly_after_the_task_needs_them(jobs: tuple[Jobs, TestClient]) -> None:
    service, client = jobs
    service.observe(client, "create_task", task())
    assert service.objects.expiries == [("input", _DEADLINE + 300), ("output", _DEADLINE + 900)]


@pytest.mark.parametrize(
    ("headers", "project", "message"),
    [
        ({"subject": ALICE, "project": "other"}, "demo", "not the project of the token"),
        ({"subject": BOB, "project": "other"}, "other", "not a member"),
        ({"subject": BOB, "project": "demo"}, "demo", "no session"),
    ],
)
def test_calls_for_another_project_or_without_membership_are_refused(
    jobs: tuple[Jobs, TestClient], headers: dict[str, str], project: str, message: str
) -> None:
    service, client = jobs
    for tool, arguments in (
        ("create_task", task(project=project)),
        ("get_task", {"project_id": project, "operation_id": "op-1"}),
        ("describe_operations", {"project_id": project}),
    ):
        assert message in service.error(client, tool, arguments, headers=service.headers(**headers))
    assert all(not executor.tasks for executor in service.executors.values())


@pytest.mark.parametrize(
    "claims",
    [{"aud": "heartwood-control"}, {"azp": "heartwood-cli"}, {"exp": 1}, {"iss": "https://x"}],
)
def test_tokens_for_another_service_or_without_a_project_are_refused(
    jobs: tuple[Jobs, TestClient], claims: dict[str, Any]
) -> None:
    service, client = jobs
    token = service.key.token(**{"iss": ISSUER, "aud": AUDIENCE, "sub": ALICE, **claims})
    response = client.post(
        MCP_PATH,
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Authorization": f"Bearer {token}", **_ACCEPT},
    )
    assert response.status_code == 401


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"resources": {"gpu_count": 1}}, "no GPU entitlement"),
        ({"image": "registry.example.test/other@sha256:" + "b" * 64}, "image is not one"),
        ({"resources": {"cpu_cores": 8}}, "limit for CPU cores"),
        ({"resources": {"max_runtime_seconds": 86400}}, "limit for run time"),
        ({"inputs": [{"object_id": "panel.data.csv", "path": "/inputs/p.csv"}]}, "no object"),
    ],
)
def test_requests_outside_entitlements_limits_or_project_data_are_denied(
    jobs: tuple[Jobs, TestClient], changes: dict[str, Any], reason: str
) -> None:
    service, client = jobs
    observed = service.observe(client, "create_task", task(**changes))

    assert observed.status == "denied"
    assert any(reason in item for item in observed.limitations)
    assert service.executors["demo"].tasks == {}
    assert service.budgets is not None
    assert service.budgets.held == {}
    # The refusal is the operation's outcome: repeating it reports the same denial.
    assert service.observe(client, "create_task", task(**changes)).status == "denied"


def test_a_budget_without_availability_denies_the_task(jobs: tuple[Jobs, TestClient]) -> None:
    service, client = jobs
    observed = service.observe(
        client,
        "create_task",
        task(project="other", inputs=[{"object_id": "panel.data.csv", "path": "/inputs/p.csv"}]),
        headers=service.headers(project="other"),
    )
    assert observed.status == "denied"
    assert observed.limitations == ("Budget demo-compute has no availability.",)
    assert service.executors["other"].tasks == {}


@pytest.mark.parametrize(
    "tags", [{"_WORKER_SA": "funnel-server"}, {"heartwood-operation": "other"}, {"bad key": "x"}]
)
def test_reserved_and_invalid_task_tags_are_rejected(
    jobs: tuple[Jobs, TestClient], tags: dict[str, str]
) -> None:
    service, client = jobs
    assert "invalid task arguments" in service.error(client, "create_task", task(tags=tags))
    assert service.executors["demo"].tasks == {}


@pytest.mark.parametrize(
    "changes",
    [
        {"inputs": [{"object_id": "cohort.data.csv", "path": "/inputs/../etc/passwd"}]},
        {"outputs": [{"name": "count.txt", "path": "/tmp/count.txt"}]},
        {"outputs": [{"name": "../count.txt", "path": "/outputs/count.txt"}]},
        {"inputs": [{"object_id": "s3://bucket/key", "path": "/inputs/x"}]},
        {"command": []},
    ],
)
def test_malformed_requests_are_refused(
    jobs: tuple[Jobs, TestClient], changes: dict[str, Any]
) -> None:
    service, client = jobs
    service.error(client, "create_task", task(**changes))
    assert service.executors["demo"].tasks == {}


def test_a_completed_task_settles_its_reservation_with_its_run_time(
    jobs: tuple[Jobs, TestClient],
) -> None:
    service, client = jobs
    service.observe(client, "create_task", task())
    budgets = service.budgets
    assert budgets is not None
    assert budgets.held["job/demo/op-1"] == ("demo", 2 * 600)
    executor = service.executors["demo"]
    executor.advance("task-0000", "RUNNING")
    started = service.now = _START + timedelta(seconds=10)
    assert (
        service.observe(client, "get_task", {"project_id": "demo", "operation_id": "op-1"}).status
        == "running"
    )
    service.now = started + timedelta(seconds=30)
    log = TesTaskLog(
        start_time=started.isoformat(),
        end_time=(started + timedelta(seconds=25)).isoformat(),
        logs=(TesExecutorLog(exit_code=0),),
    )
    executor.advance("task-0000", "COMPLETE", log)
    observed = service.observe(client, "get_task", {"project_id": "demo", "operation_id": "op-1"})

    assert observed.status == "completed"
    assert budgets.held == {}
    assert budgets.settled["job/demo/op-1"].seconds == 25
    assert budgets.available["demo"] == 100_000 - 2 * 25


def test_an_identifier_reported_before_it_arrived_is_failed_for_good(
    jobs: tuple[Jobs, TestClient],
) -> None:
    service, client = jobs
    observed = service.observe(client, "get_task", {"project_id": "demo", "operation_id": "late"})
    assert observed.status == "failed"
    assert "not received" in service.error(client, "create_task", task("late"))
    assert service.executors["demo"].tasks == {}


def test_a_task_that_does_not_start_in_time_is_cancelled_and_failed(
    jobs: tuple[Jobs, TestClient],
) -> None:
    service, client = jobs
    service.observe(client, "create_task", task())
    service.now = _START + timedelta(seconds=_DEADLINE + 1)
    observed = service.observe(client, "get_task", {"project_id": "demo", "operation_id": "op-1"})

    assert observed.status == "failed"
    assert observed.limitations[0] == f"The task did not start within {_DEADLINE} seconds."
    assert service.executors["demo"].cancelled == ["task-0000"]
    assert service.budgets is not None
    assert service.budgets.settled["job/demo/op-1"].seconds == 0


def test_a_task_past_its_run_time_limit_is_cancelled_and_failed(
    jobs: tuple[Jobs, TestClient],
) -> None:
    service, client = jobs
    service.observe(client, "create_task", task())
    service.executors["demo"].advance("task-0000", "RUNNING")
    service.observe(client, "get_task", {"project_id": "demo", "operation_id": "op-1"})
    service.now = _START + timedelta(seconds=601)
    observed = service.observe(client, "get_task", {"project_id": "demo", "operation_id": "op-1"})

    assert observed.status == "failed"
    assert "run time limit of 600 seconds" in observed.limitations[0]
    assert service.executors["demo"].cancelled == ["task-0000"]


def test_a_lost_creation_reply_is_found_again_by_the_operation_tag(
    jobs: tuple[Jobs, TestClient],
) -> None:
    service, client = jobs
    executor = service.executors["demo"]
    executor.failure = "lost"
    executor.tasks = {
        f"task-x{index}": _Task(
            TesTask.model_validate(
                {
                    "name": "other",
                    "resources": {"cpu_cores": 1, "ram_gb": 1},
                    "executors": [{"image": IMAGE, "command": ["true"]}],
                    "tags": {OPERATION_TAG: f"before-{index}"},
                }
            ),
            "COMPLETE",
        )
        for index in range(3)
    }
    assert service.observe(client, "create_task", task()).status == "submitted"
    executor.failure = None

    # A repetition finds the task on a later page instead of creating a second one.
    observed = service.observe(client, "create_task", task())
    assert observed.status == "queued"
    assert observed.backend_operation_ref == "task-0000"
    assert len(executor.tasks) == 4


def test_a_submission_that_never_reached_the_executor_fails_after_the_grace_period(
    jobs: tuple[Jobs, TestClient],
) -> None:
    service, client = jobs
    service.executors["demo"].failure = "unreachable"
    assert service.observe(client, "create_task", task()).status == "submitted"
    service.executors["demo"].failure = None
    service.now = _START + timedelta(seconds=121)
    observed = service.observe(client, "get_task", {"project_id": "demo", "operation_id": "op-1"})

    assert observed.status == "failed"
    assert observed.limitations[0] == "The executor did not accept the task."
    assert service.executors["demo"].tasks == {}
    assert service.budgets is not None
    assert service.budgets.held == {}


def test_reconciliation_cancels_tasks_that_no_operation_recorded(
    jobs: tuple[Jobs, TestClient],
) -> None:
    service, client = jobs
    service.observe(client, "create_task", task())
    executor = service.executors["demo"]
    # A task created around the job service, as a compromised worker could.
    stray = executor.create(
        TesTask.model_validate(
            {
                "name": "stray",
                "resources": {"cpu_cores": 1, "ram_gb": 1},
                "executors": [{"image": IMAGE, "command": ["true"]}],
            }
        )
    )
    forged = executor.create(
        TesTask.model_validate(
            {
                "name": "forged",
                "resources": {"cpu_cores": 1, "ram_gb": 1},
                "executors": [{"image": IMAGE, "command": ["true"]}],
                "tags": {OPERATION_TAG: "op-1"},
            }
        )
    )

    assert service.service.reconcile("demo") == [stray, forged]
    assert executor.tasks["task-0000"].state == "QUEUED"
    assert service.service.reconcile("demo") == []


@pytest.mark.parametrize(
    ("state", "logs", "expected"),
    [
        (
            "EXECUTOR_ERROR",
            ("OOMKilled",),
            137,
        ),
        ("EXECUTOR_ERROR", (), 137),
        ("EXECUTOR_ERROR", (), 3),
        ("SYSTEM_ERROR", ("download failed url='https://store/x?X-Amz-Signature=secret'",), None),
        ("SYSTEM_ERROR", ("execution owner Pod abc is missing",), None),
    ],
)
def test_failures_are_explained_without_passing_on_executor_logs(
    jobs: tuple[Jobs, TestClient], state: str, logs: tuple[str, ...], expected: int | None
) -> None:
    service, client = jobs
    service.observe(client, "create_task", task())
    attempt = TesTaskLog(system_logs=logs, logs=(TesExecutorLog(exit_code=expected),))
    service.executors["demo"].advance("task-0000", state, attempt)
    observed = service.observe(client, "get_task", {"project_id": "demo", "operation_id": "op-1"})

    assert observed.status == "failed"
    text = " ".join(observed.limitations)
    assert "secret" not in text
    assert "abc" not in text
    if "OOMKilled" in logs:
        assert "ran out of memory" in text
    elif expected == 137:
        assert "deleted or evicted" in text
    elif expected == 3:
        assert "exited with code 3" in text
    elif "download" in logs[0]:
        assert "input could not be downloaded" in text
    else:
        assert "worker of the task was lost" in text


def test_observed_states_only_move_forward(jobs: tuple[Jobs, TestClient]) -> None:
    service, client = jobs
    service.observe(client, "create_task", task())
    executor = service.executors["demo"]
    executor.advance("task-0000", "RUNNING")
    service.observe(client, "get_task", {"project_id": "demo", "operation_id": "op-1"})
    executor.advance("task-0000", "QUEUED")
    observed = service.observe(client, "get_task", {"project_id": "demo", "operation_id": "op-1"})
    assert observed.status == "running"


def test_cancellation_is_its_own_operation_and_reports_what_it_observed(
    jobs: tuple[Jobs, TestClient],
) -> None:
    service, client = jobs
    service.observe(client, "create_task", task())
    service.executors["demo"].advance("task-0000", "RUNNING")
    arguments = {"project_id": "demo", "operation_id": "cancel-1", "target_operation_id": "op-1"}

    observed = service.observe(client, "cancel_task", arguments)

    assert observed.status == "completed"
    assert observed.backend_operation_ref == "task-0000"
    assert observed.limitations == ("Cancellation of op-1 observed TES state CANCELED.",)
    target = service.observe(client, "get_task", {"project_id": "demo", "operation_id": "op-1"})
    assert target.status == "cancelled"
    # Repeating it observes the same outcome; the status tool reports it too.
    assert service.observe(client, "cancel_task", arguments) == observed
    status = service.observe(client, "get_task", {"project_id": "demo", "operation_id": "cancel-1"})
    assert status.status == "completed"
    assert service.executors["demo"].cancelled == ["task-0000"]
    # Another target under the same identifier is a conflict.
    assert "conflict" in service.error(
        client, "cancel_task", {**arguments, "target_operation_id": "op-2"}
    )


def test_cancelling_finished_or_unknown_work(jobs: tuple[Jobs, TestClient]) -> None:
    service, client = jobs
    service.observe(client, "create_task", task())
    service.executors["demo"].advance("task-0000", "COMPLETE")
    observed = service.observe(
        client,
        "cancel_task",
        {"project_id": "demo", "operation_id": "c", "target_operation_id": "op-1"},
    )
    assert observed.limitations == ("Cancellation of op-1 observed TES state COMPLETE.",)
    assert service.executors["demo"].cancelled == []
    for target in ("missing", "c"):
        arguments = {
            "project_id": "demo",
            "operation_id": f"c-{target}",
            "target_operation_id": target,
        }
        assert "operation not found" in service.error(client, "cancel_task", arguments)


def test_another_projects_operation_cannot_be_observed_or_cancelled(
    jobs: tuple[Jobs, TestClient],
) -> None:
    service, client = jobs
    service.observe(client, "create_task", task())
    other = service.headers(project="other")
    arguments = {"project_id": "other", "operation_id": "c", "target_operation_id": "op-1"}
    assert "operation not found" in service.error(client, "cancel_task", arguments, headers=other)
    observed = service.observe(
        client, "get_task", {"project_id": "other", "operation_id": "op-1"}, headers=other
    )
    assert observed.status == "failed"
    assert service.executors["demo"].cancelled == []


def test_without_budgets_the_service_says_it_reserves_nothing(tmp_path: Path) -> None:
    service = Jobs(tmp_path, budgets=False)
    with TestClient(service.app) as client:
        result = service.call(client, "describe_operations", {"project_id": "demo"})
        assert (
            "This deployment reserves no budget for jobs."
            in result["structuredContent"]["limitations"]
        )
        assert service.observe(client, "create_task", task()).status == "queued"


def test_another_members_operations_are_neither_shown_nor_cancelled(
    jobs: tuple[Jobs, TestClient],
) -> None:
    service, client = jobs
    service.observe(client, "create_task", task())
    carol = service.headers(subject=CAROL)
    status = {"project_id": "demo", "operation_id": "op-1"}
    assert "operation not found" in service.error(client, "get_task", status, headers=carol)
    cancel = {"project_id": "demo", "operation_id": "c", "target_operation_id": "op-1"}
    assert "operation not found" in service.error(client, "cancel_task", cancel, headers=carol)
    assert "conflict" in service.error(client, "create_task", task(), headers=carol)
    assert service.executors["demo"].cancelled == []
    assert len(service.executors["demo"].tasks) == 1


def test_a_gpu_type_is_denied(jobs: tuple[Jobs, TestClient]) -> None:
    service, client = jobs
    arguments = task(
        project="other",
        resources={"gpu_count": 1, "gpu_type": "nvidia-l4"},
        inputs=[],
    )
    observed = service.observe(
        client, "create_task", arguments, headers=service.headers(project="other")
    )
    assert observed.status == "denied"
    assert observed.limitations == ("The platform does not select GPU types.",)


def test_a_lost_reservation_reply_is_settled_by_its_holder(
    jobs: tuple[Jobs, TestClient],
) -> None:
    service, client = jobs
    budgets = service.budgets
    assert budgets is not None
    budgets.unavailable = True
    observed = service.observe(client, "create_task", task())

    assert observed.status == "failed"
    assert observed.limitations[0] == "The budget could not be reserved."
    assert budgets.held == {}
    assert budgets.available["demo"] == 100_000
    assert service.executors["demo"].tasks == {}


def test_a_server_error_on_creation_leaves_the_task_to_be_found(
    jobs: tuple[Jobs, TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    service, client = jobs
    executor = service.executors["demo"]
    original = executor.create

    def create(created: TesTask) -> str:
        original(created)
        request = httpx.Request("POST", "https://executor/v1/tasks")
        raise httpx.HTTPStatusError(
            "bad gateway", request=request, response=httpx.Response(502, request=request)
        )

    monkeypatch.setattr(executor, "create", create)
    assert service.observe(client, "create_task", task()).status == "submitted"
    observed = service.observe(client, "get_task", {"project_id": "demo", "operation_id": "op-1"})
    assert observed.status == "queued"
    assert observed.backend_operation_ref == "task-0000"


def test_a_refused_creation_fails_and_releases_the_reservation(
    jobs: tuple[Jobs, TestClient],
) -> None:
    service, client = jobs
    service.executors["demo"].failure = "refused"
    observed = service.observe(client, "create_task", task())
    assert observed.status == "failed"
    assert service.budgets is not None
    assert service.budgets.held == {}


def test_a_task_planted_with_a_later_operations_tag_is_not_adopted(
    jobs: tuple[Jobs, TestClient],
) -> None:
    service, client = jobs
    executor = service.executors["demo"]
    planted = executor.create(_sleeping({OPERATION_TAG: "op-1"}))
    service.now = _START + timedelta(hours=1)
    executor.failure = "unreachable"
    assert service.observe(client, "create_task", task()).status == "submitted"
    executor.failure = None

    assert service.service.reconcile("demo") == [planted]
    observed = service.observe(client, "get_task", {"project_id": "demo", "operation_id": "op-1"})
    assert observed.status == "submitted"


def test_an_unfinished_enforced_cancellation_is_sent_again(
    jobs: tuple[Jobs, TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    service, client = jobs
    service.observe(client, "create_task", task())
    executor = service.executors["demo"]
    calls: list[str] = []
    monkeypatch.setattr(executor, "cancel", calls.append)
    service.now = _START + timedelta(seconds=_DEADLINE + 1)
    status = {"project_id": "demo", "operation_id": "op-1"}
    assert service.observe(client, "get_task", status).status == "failed"
    service.observe(client, "get_task", status)
    assert calls == ["task-0000", "task-0000"]


def test_a_task_that_completes_before_its_deadline_cancellation_is_completed(
    jobs: tuple[Jobs, TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    service, client = jobs
    service.observe(client, "create_task", task())
    executor = service.executors["demo"]
    executor.advance("task-0000", "RUNNING")
    service.observe(client, "get_task", {"project_id": "demo", "operation_id": "op-1"})
    monkeypatch.setattr(executor, "cancel", lambda _: executor.advance("task-0000", "COMPLETE"))
    service.now = _START + timedelta(seconds=601)
    observed = service.observe(client, "get_task", {"project_id": "demo", "operation_id": "op-1"})
    assert observed.status == "completed"


def test_cancelling_a_task_that_the_executor_lost(jobs: tuple[Jobs, TestClient]) -> None:
    service, client = jobs
    service.observe(client, "create_task", task())
    service.executors["demo"].advance("task-0000", "RUNNING")
    service.observe(client, "get_task", {"project_id": "demo", "operation_id": "op-1"})
    del service.executors["demo"].tasks["task-0000"]
    cancel = {"project_id": "demo", "operation_id": "c", "target_operation_id": "op-1"}
    observed = service.observe(client, "cancel_task", cancel)
    assert observed.limitations == ("Cancellation of op-1 observed TES state SYSTEM_ERROR.",)
    target = service.observe(client, "get_task", {"project_id": "demo", "operation_id": "op-1"})
    assert target.status == "failed"
    assert "no longer has the task" in target.limitations[0]


def test_a_proposal_too_long_to_review_is_refused(jobs: tuple[Jobs, TestClient]) -> None:
    service, client = jobs
    arguments = {"name": "long", "image": IMAGE, "command": ["x" * 4096] * 3}
    message = service.error(
        client,
        "propose_task",
        {"project_id": "demo", "tool": "create_task", "arguments": arguments},
    )
    assert "too long" in message


def test_reconciliation_goes_on_after_a_failure(
    jobs: tuple[Jobs, TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _ = jobs
    executor = service.executors["demo"]
    first, second = executor.create(_sleeping({})), executor.create(_sleeping({}))
    original = executor.cancel

    def cancel(task_id: str) -> None:
        if task_id == first:
            raise TaskNotFoundError
        original(task_id)

    monkeypatch.setattr(executor, "cancel", cancel)
    assert service.service.reconcile("demo") == [second]


def _sleeping(tags: dict[str, str]) -> TesTask:
    return TesTask.model_validate(
        {
            "name": "stray",
            "resources": {"cpu_cores": 1, "ram_gb": 1},
            "executors": [{"image": IMAGE, "command": ["sleep", "600"]}],
            "tags": tags,
        }
    )
