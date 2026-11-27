# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Contract of the TES executor that the job service relies on.

The tests use only an executor's URL, CA and credential, through the job service's
``TesExecutor`` client, and only the TES subset that the job service sends: one executor
per task, single files by presigned URL, resources and tags. On the cluster they run
against the demo project's Funnel server; any executor that serves the subset passes them.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from heartwood_platform.jobs.tes import (
    TaskNotFoundError,
    TesExecutor,
    TesExecutorSpec,
    TesInput,
    TesOutput,
    TesResources,
    TesTask,
)
from jobs import (
    TASK_IMAGE,
    TERMINAL,
    Store,
    executor,
    job_service_password,
    job_service_paused,
    store,
    wait_for,
)
from kube import Cluster

_BUCKET = "hw-project-demo"


@dataclass
class Contract:
    tes: TesExecutor
    store: Store
    cluster: Cluster


@pytest.fixture(scope="module")
def contract(cluster: Cluster) -> Iterator[Contract]:
    with (
        job_service_paused(cluster),
        executor(cluster, "demo") as funnel,
        store(cluster) as objects,
    ):
        yield Contract(
            funnel.client("jobsvc", job_service_password(cluster, "demo")), objects, cluster
        )


def _task(
    contract: Contract,
    script: str,
    *,
    inputs: dict[str, str] | None = None,
    outputs: tuple[str, ...] = (),
) -> tuple[TesTask, dict[str, str]]:
    """A task running ``script``; each input maps a path to an object key it is read from."""
    run = secrets.token_hex(6)
    keys = {name: f"results/contract-{run}/{name}" for name in outputs}
    return (
        TesTask(
            name=f"contract {run}",
            inputs=tuple(
                TesInput(url=contract.store.url("GET", _BUCKET, key), path=path)
                for path, key in (inputs or {}).items()
            ),
            outputs=tuple(
                TesOutput(url=contract.store.url("PUT", _BUCKET, key), path=f"/outputs/{name}")
                for name, key in keys.items()
            ),
            resources=TesResources(cpu_cores=1, ram_gb=0.25, disk_gb=1),
            executors=(TesExecutorSpec(image=TASK_IMAGE, command=("sh", "-c", script)),),
            tags={"contract": run},
        ),
        keys,
    )


def _finish(contract: Contract, task_id: str) -> tuple[str, list[str], int | None]:
    view, states = wait_for(
        lambda: contract.tes.get(task_id, view="FULL"), lambda item: item.state in TERMINAL
    )
    codes = [log.exit_code for attempt in view.logs or () for log in attempt.logs]
    return view.state, states, codes[-1] if codes else None


def test_the_executor_describes_itself(contract: Contract) -> None:
    info = contract.tes.service_info()
    assert info.id
    assert info.name


def test_a_task_reads_its_input_and_writes_its_output(contract: Contract) -> None:
    contract.store.put(_BUCKET, "contract/input.csv", b"a\nb\nc\n")
    task, keys = _task(
        contract,
        "wc -l < /inputs/input.csv > /outputs/count.txt",
        inputs={"/inputs/input.csv": "contract/input.csv"},
        outputs=("count.txt",),
    )
    state, states, code = _finish(contract, contract.tes.create(task))
    assert state == "COMPLETE", states
    assert code == 0
    assert contract.store.get(_BUCKET, keys["count.txt"]) == b"3\n"


def test_a_failing_command_is_an_executor_error_with_its_exit_code(contract: Contract) -> None:
    task, _ = _task(contract, "exit 3")
    state, states, code = _finish(contract, contract.tes.create(task))
    assert state == "EXECUTOR_ERROR", states
    assert code == 3


def test_a_missing_input_is_a_system_error(contract: Contract) -> None:
    task, _ = _task(contract, "true", inputs={"/inputs/missing.csv": "contract/missing.csv"})
    state, states, _ = _finish(contract, contract.tes.create(task))
    assert state == "SYSTEM_ERROR", states


def test_a_declared_output_that_is_not_written_is_a_system_error(contract: Contract) -> None:
    task, _ = _task(contract, "true", outputs=("never.txt",))
    state, states, _ = _finish(contract, contract.tes.create(task))
    assert state == "SYSTEM_ERROR", states


def test_cancelling_a_running_task_removes_its_pods(contract: Contract) -> None:
    task, _ = _task(contract, "sleep 600")
    task_id = contract.tes.create(task)
    wait_for(lambda: contract.tes.get(task_id), lambda item: item.state == "RUNNING")
    contract.tes.cancel(task_id)
    state, states, _ = _finish(contract, task_id)
    assert state == "CANCELED", states

    def pods() -> str:
        result = contract.cluster.kubectl(
            "get",
            "--namespace=hw-jobs-demo",
            "pods",
            f"--selector=heartwood.platform/task={task_id}",
            "--output=name",
        )
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    for _ in range(60):
        if not pods():
            break
        import time

        time.sleep(2)
    assert pods() == ""


def test_cancelling_a_finished_task_leaves_its_state(contract: Contract) -> None:
    task, _ = _task(contract, "exit 0")
    task_id = contract.tes.create(task)
    assert _finish(contract, task_id)[0] == "COMPLETE"
    contract.tes.cancel(task_id)
    assert contract.tes.get(task_id).state == "COMPLETE"


def test_an_unknown_task_does_not_exist(contract: Contract) -> None:
    with pytest.raises(TaskNotFoundError):
        contract.tes.get("00000000-0000-1000-8000-000000000000")
    with pytest.raises(TaskNotFoundError):
        contract.tes.cancel("00000000-0000-1000-8000-000000000000")


def test_tasks_are_listed_by_tag_and_state(contract: Contract) -> None:
    task, _ = _task(contract, "exit 0")
    task_id = contract.tes.create(task)
    _finish(contract, task_id)
    from heartwood_platform.jobs.tes import TaskFilter

    listed = contract.tes.list(
        filters=TaskFilter(tag_key="contract", tag_value=task.tags["contract"])
    )
    assert [view.id for view in listed.tasks] == [task_id]
    running = contract.tes.list(filters=TaskFilter(state="RUNNING"))
    assert task_id not in {view.id for view in running.tasks}
