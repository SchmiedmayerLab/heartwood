# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Platform conformance kit and deterministic service fakes, run in process on loopback."""

from __future__ import annotations

import hashlib
import json
import os
import runpy
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

import pytest
from fastmcp import FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from mcp.shared.exceptions import MCPError as McpProtocolError
from mcp.types import CallToolResult
from openhands.sdk.mcp import MCPClient

from heartwood.adapters.service_conformance import validate_platform_message
from heartwood.gateway.platform_conformance import (
    InferenceEndpoint,
    McpEndpoint,
    check_inference,
    check_mcp_service,
    run_platform_conformance,
)
from heartwood.gateway.platform_fakes import (
    BUDGETS_AUDIENCE,
    DATA_AUDIENCE,
    INFERENCE_API_KEY,
    JOBS_AUDIENCE,
    OTHER_PROJECT_ID,
    PROJECT_ID,
    SYNTHETIC_OBJECTS,
    InferenceFailure,
    RunningFakes,
    running_platform_fakes,
    serve,
)
from heartwood.schemas import PlatformMcpServiceProfile

_SCRIPT = Path(__file__).parents[1] / "scripts" / "check_platform_endpoints.py"
_CPU: dict[str, object] = {"cpu_cores": 2, "ram_gb": 4.0}
_GPU: dict[str, object] = {
    "cpu_cores": 4,
    "ram_gb": 16.0,
    "gpu_type": "synthetic-gpu",
    "gpu_count": 1,
}


@pytest.fixture(autouse=True)
def _loopback_without_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")


@pytest.fixture
def fakes() -> Iterator[RunningFakes]:
    with running_platform_fakes(gpu_projects=frozenset({OTHER_PROJECT_ID})) as running:
        yield running


@contextmanager
def _client(profile: PlatformMcpServiceProfile, token: str) -> Iterator[MCPClient]:
    client = MCPClient(StreamableHttpTransport(str(profile.url), auth=token), timeout=5)  # type: ignore[no-untyped-call]
    try:
        client.call_async_from_sync(client.connect, timeout=5)
        yield client
    finally:
        client.sync_close()


def _call(
    profile: PlatformMcpServiceProfile, token: str, tool: str, /, **arguments: object
) -> dict[str, Any]:
    """Return structured content, or raise for a refused call."""
    with _client(profile, token) as client:
        result = cast(
            CallToolResult,
            client.call_async_from_sync(client.call_tool_mcp, tool, arguments, timeout=5),
        )
    wire = result.model_dump(mode="json", by_alias=True)
    if wire.get("isError"):
        raise PermissionError(wire["content"][0]["text"])
    return cast(dict[str, Any], wire["structuredContent"])


def _jobs(
    fakes: RunningFakes, tool: str, *, project: str = PROJECT_ID, **arguments: object
) -> dict[str, Any]:
    token = fakes.issuer.exchange(audience=JOBS_AUDIENCE, project_id=project)
    observation = _call(fakes.jobs_profile, token, tool, project_id=project, **arguments)
    validate_platform_message("operation-observation.schema.json", observation)
    return observation


def _data(fakes: RunningFakes, tool: str, **arguments: object) -> dict[str, Any]:
    token = fakes.issuer.exchange(audience=DATA_AUDIENCE)
    return _call(fakes.data_profile, token, tool, project_id=PROJECT_ID, **arguments)


def _submit(
    fakes: RunningFakes, operation: str, resources: dict[str, object], *, project: str = PROJECT_ID
) -> dict[str, Any]:
    return _jobs(
        fakes,
        "create_task",
        operation_id=operation,
        name="synthetic-analysis",
        command=["python", "analysis.py"],
        resources=resources,
        project=project,
    )


def test_kit_runs_the_same_checks_against_the_fakes(fakes: RunningFakes) -> None:
    inference, services = fakes.endpoints()

    results = run_platform_conformance(inference, services)

    assert [(r.capability, r.target, r.status, r.item_count) for r in results] == [
        ("inference", "inference", "passed", 1),
        ("platform-tools", "synthetic-data", "passed", 3),
        ("platform-tools", "synthetic-jobs", "passed", 5),
        ("platform-tools", "synthetic-budgets", "passed", 2),
    ]
    assert all(r.diagnostic is None for r in results)
    assert "Negotiated MCP revision listed in the service profile" in results[1].checks


def test_kit_reports_a_tool_list_that_violates_the_profile(fakes: RunningFakes) -> None:
    server = FastMCP("Unannotated")

    def lookup(key: str) -> dict[str, str]:
        """Look up a synthetic value."""
        return {"key": key}

    server.tool(lookup)
    with serve(server.http_app(path="/mcp", json_response=True, stateless_http=True)) as origin:
        profile = fakes.data_profile.model_copy(update={"url": f"{origin}/mcp"})
        result = check_mcp_service(McpEndpoint(profile))

    assert result.status == "failed"
    assert result.checks == ("Negotiated MCP revision listed in the service profile",)


@pytest.mark.parametrize(
    ("failure", "diagnostic"),
    [
        ("authentication", "HW-AGENT-008"),
        ("quota", "HW-AGENT-009"),
        ("rate-limit", "HW-AGENT-010"),
        ("model-denied", "HW-AGENT-011"),
        ("unavailable", "HW-AGENT-012"),
    ],
)
def test_kit_maps_inference_failures_to_contract_diagnostics(
    fakes: RunningFakes, failure: InferenceFailure, diagnostic: str
) -> None:
    fakes.inference.failure = failure

    result = check_inference(InferenceEndpoint(fakes.inference_url, INFERENCE_API_KEY))

    assert (result.status, result.diagnostic) == ("unavailable", diagnostic)


def test_kit_reports_unknown_models_and_unreachable_routes(fakes: RunningFakes) -> None:
    denied = check_inference(
        InferenceEndpoint(fakes.inference_url, INFERENCE_API_KEY, model="withdrawn-model")
    )
    with serve(FastMCP("Stopped").http_app(path="/mcp")) as origin:
        unreachable_url = f"{origin}/v1"
    unreachable = check_inference(InferenceEndpoint(unreachable_url, INFERENCE_API_KEY))

    assert denied.diagnostic == "HW-AGENT-011"
    assert unreachable.diagnostic == "HW-AGENT-012"


def test_inference_down_leaves_platform_tools_listing(fakes: RunningFakes) -> None:
    fakes.inference.available = False
    inference, services = fakes.endpoints()

    results = run_platform_conformance(inference, services)

    assert [(r.status, r.diagnostic) for r in results] == [
        ("unavailable", "HW-AGENT-012"),
        ("passed", None),
        ("passed", None),
        ("passed", None),
    ]


def test_platform_tools_down_leave_inference_working(fakes: RunningFakes) -> None:
    fakes.data.available = False
    fakes.jobs.available = False
    fakes.budgets.available = False
    inference, services = fakes.endpoints()

    results = run_platform_conformance(inference, services)

    assert [r.status for r in results] == ["passed", "unavailable", "unavailable", "unavailable"]


@pytest.mark.parametrize("service", ["data", "jobs", "budgets"])
@pytest.mark.parametrize(
    "token",
    [
        {"lifetime_seconds": -3600},
        {"audience": "https://other.synthetic.invalid"},
        {"project_id": OTHER_PROJECT_ID},
    ],
    ids=["expired", "wrong-audience", "wrong-project"],
)
def test_services_refuse_expired_wrong_audience_and_wrong_project_tokens(
    fakes: RunningFakes, service: str, token: dict[str, Any]
) -> None:
    profile, audience, tool, arguments = (
        (fakes.data_profile, DATA_AUDIENCE, "list_objects", {})
        if service == "data"
        else (fakes.budgets_profile, BUDGETS_AUDIENCE, "get_budgets", {})
        if service == "budgets"
        else (
            fakes.jobs_profile,
            JOBS_AUDIENCE,
            "create_task",
            {
                "operation_id": "op-refused",
                "name": "synthetic-analysis",
                "command": ["true"],
                "resources": _CPU,
            },
        )
    )
    issued = fakes.issuer.exchange(**{"audience": audience, **token})
    # The verifier answers 401 before a tool runs; the tool refuses another project's token.
    refusal = PermissionError if "project_id" in token else McpProtocolError
    message = "project authorization denied" if "project_id" in token else None

    with pytest.raises(refusal, match=message):
        _call(profile, issued, tool, project_id=PROJECT_ID, **arguments)
    assert fakes.jobs.task_count == 0


def test_data_service_resolves_only_selected_objects_of_the_project(fakes: RunningFakes) -> None:
    listed = _data(fakes, "list_objects")
    cohort = _data(fakes, "get_object", object_id="obj-cohort")
    access = _data(fakes, "get_access_url", object_id="obj-cohort", access_id="https-obj-cohort")

    assert [item["id"] for item in listed["objects"]] == ["obj-cohort", "obj-expired"]
    assert cohort["self_uri"] == "drs://data.synthetic.invalid/obj-cohort"
    assert cohort["checksums"] == [
        {"type": "sha-256", "checksum": hashlib.sha256(SYNTHETIC_OBJECTS[0].content).hexdigest()}
    ]
    assert access["url"].startswith("https://storage.synthetic.invalid/")
    for object_id, message in (
        ("obj-unselected", "object is not selected for this project"),
        ("obj-other", "object not found"),
    ):
        with pytest.raises(PermissionError, match=message):
            _data(fakes, "get_object", object_id=object_id)
    with pytest.raises(PermissionError, match="access grant expired"):
        _data(fakes, "get_access_url", object_id="obj-expired", access_id="https-obj-expired")


def test_data_service_unavailability_is_reported_and_recovers(fakes: RunningFakes) -> None:
    fakes.data.available = False
    with pytest.raises(McpProtocolError):
        _data(fakes, "list_objects")

    fakes.data.available = True
    assert _data(fakes, "list_objects")["objects"]


def test_job_service_submits_reports_and_cancels_by_operation(fakes: RunningFakes) -> None:
    submitted = _submit(fakes, "op-batch", _CPU)
    fakes.jobs.advance(PROJECT_ID, "op-batch", "RUNNING")
    running = _jobs(fakes, "get_task", operation_id="op-batch")
    cancel = _jobs(fakes, "cancel_task", operation_id="op-cancel", target_operation_id="op-batch")
    repeated = _jobs(fakes, "cancel_task", operation_id="op-cancel", target_operation_id="op-batch")
    cancelled = _jobs(fakes, "get_task", operation_id="op-batch")

    assert (submitted["status"], submitted["backend_operation_ref"]) == ("queued", "task-0001")
    assert submitted["project_context_id"] == "launch-synthetic-1"
    assert running["status"] == "running"
    # The cancellation is its own operation; the target reports what happened to it.
    assert cancel == repeated == _jobs(fakes, "get_task", operation_id="op-cancel")
    assert (cancel["status"], cancel["backend_operation_ref"]) == ("completed", "task-0001")
    assert cancel["limitations"] == ["Cancellation of op-batch observed TES state CANCELED"]
    assert cancelled["status"] == "cancelled"
    assert fakes.jobs.task_count == 1
    with pytest.raises(PermissionError, match="operation identifier conflict"):
        _jobs(fakes, "cancel_task", operation_id="op-cancel", target_operation_id="op-other")
    with pytest.raises(PermissionError, match="operation not found"):
        _jobs(fakes, "cancel_task", operation_id="op-cancel-2", target_operation_id="op-none")


def test_status_of_an_unreceived_operation_is_failed_and_refused_afterwards(
    fakes: RunningFakes,
) -> None:
    unseen = _jobs(fakes, "get_task", operation_id="op-unseen")

    assert unseen["status"] == "failed"
    assert unseen["limitations"] == ["The service never received this operation."]
    with pytest.raises(PermissionError, match="reported as not received"):
        _submit(fakes, "op-unseen", _CPU)
    with pytest.raises(PermissionError, match="reported as not received"):
        _jobs(fakes, "cancel_task", operation_id="op-unseen", target_operation_id="op-unseen")
    with pytest.raises(PermissionError, match="operation not found"):
        _jobs(fakes, "cancel_task", operation_id="op-cancel", target_operation_id="op-unseen")
    assert fakes.jobs.task_count == 0


def test_job_service_describes_operations_and_proposes_without_submitting(
    fakes: RunningFakes,
) -> None:
    token = fakes.issuer.exchange(audience=JOBS_AUDIENCE)
    described = _call(fakes.jobs_profile, token, "describe_operations", project_id=PROJECT_ID)
    proposal = _call(
        fakes.jobs_profile,
        token,
        "propose_task",
        project_id=PROJECT_ID,
        tool="create_task",
        arguments={
            "name": "synthetic-gpu",
            "command": ["python", "train.py"],
            "resources": _GPU,
            "inputs": ["obj-cohort"],
        },
    )

    validate_platform_message("operation-capabilities.schema.json", described)
    validate_platform_message("operation-proposal.schema.json", proposal)
    assert {item["tool"]: item["required_permission"] for item in described["operations"]} == {
        "create_task": "jobs:submit",
        "get_task": "jobs:read",
        "cancel_task": "jobs:cancel",
        "propose_task": "jobs:read",
    }
    # Only GPU resources are declared, and this project may not use them.
    assert [
        (item["capability"], item["status"]) for item in described["optional_capabilities"]
    ] == [("gpu-resources", "not-permitted")]
    assert proposal["runtime_profile_ref"] == "synthetic-batch-gpu"
    assert {
        item["metric"]: (item["unit"], item["maximum"]) for item in proposal["resource_limits"]
    } == {
        "cpu": ("core", "4"),
        "memory": ("GB", "16"),
        "duration": ("s", "3600"),
        "gpu": ("synthetic-gpu", "1"),
    }
    assert proposal["data_scope"] == "Project objects drs://data.synthetic.invalid/obj-cohort"
    assert "no GPU entitlement" in proposal["limitations"][-1]
    assert fakes.jobs.task_count == 0
    with pytest.raises(PermissionError, match="no proposal for this tool"):
        _call(
            fakes.jobs_profile,
            token,
            "propose_task",
            project_id=PROJECT_ID,
            tool="get_task",
            arguments={},
        )


def test_exhausted_budgets_and_revoked_access_refuse_work(fakes: RunningFakes) -> None:
    fakes.jobs.exhausted_projects.add(PROJECT_ID)
    denied = _submit(fakes, "op-budget", _CPU)
    fakes.jobs.revoked_projects.add(PROJECT_ID)

    assert (denied["status"], denied["limitations"]) == (
        "denied",
        ["Budget budget-compute has no availability"],
    )
    with pytest.raises(PermissionError, match="authorization revoked"):
        _jobs(fakes, "get_task", operation_id="op-budget")
    assert fakes.jobs.task_count == 0


def test_lost_submission_reply_reconciles_to_one_task(fakes: RunningFakes) -> None:
    fakes.jobs.drop_next_submission_reply()
    with pytest.raises(McpProtocolError):
        _submit(fakes, "op-lost", _CPU)

    reconciled = _jobs(fakes, "get_task", operation_id="op-lost")
    repeated = _submit(fakes, "op-lost", _CPU)

    assert reconciled == repeated
    assert reconciled["backend_operation_ref"] == "task-0001"
    assert fakes.jobs.task_count == 1
    with pytest.raises(PermissionError, match="operation identifier conflict"):
        _submit(fakes, "op-lost", _GPU)


def test_gpu_request_without_entitlement_is_denied(fakes: RunningFakes) -> None:
    denied = _submit(fakes, "op-gpu", _GPU)
    entitled = _submit(fakes, "op-gpu", _GPU, project=OTHER_PROJECT_ID)

    assert denied["status"] == "denied"
    assert _jobs(fakes, "get_task", operation_id="op-gpu")["status"] == "denied"
    assert entitled["status"] == "queued"
    assert fakes.jobs.task_count == 1
    with pytest.raises(ValueError, match="denied operation has no task"):
        fakes.jobs.advance(PROJECT_ID, "op-gpu", "RUNNING")


def test_task_that_fails_after_it_started_is_observed_as_failed(fakes: RunningFakes) -> None:
    _submit(fakes, "op-fails", _CPU)
    fakes.jobs.advance(PROJECT_ID, "op-fails", "RUNNING")
    fakes.jobs.advance(PROJECT_ID, "op-fails", "EXECUTOR_ERROR")

    failed = _jobs(fakes, "get_task", operation_id="op-fails")
    _jobs(fakes, "cancel_task", operation_id="op-cancel", target_operation_id="op-fails")

    assert failed["status"] == _jobs(fakes, "get_task", operation_id="op-fails")["status"]
    assert failed["status"] == "failed"
    assert failed["limitations"] == ["TES state EXECUTOR_ERROR"]


def test_tool_annotations_mark_submission_and_cancellation_as_state_changing(
    fakes: RunningFakes,
) -> None:
    with _client(fakes.jobs_profile, fakes.issuer.exchange(audience=JOBS_AUDIENCE)) as client:
        listed = client.call_async_from_sync(client.list_tools_mcp, timeout=5)
    hints = {
        tool["name"]: tool["annotations"]
        for tool in json.loads(listed.model_dump_json(by_alias=True))["tools"]
    }

    assert {name: hint["readOnlyHint"] for name, hint in hints.items()} == {
        "create_task": False,
        "get_task": True,
        "cancel_task": False,
        "describe_operations": True,
        "propose_task": True,
    }
    assert hints["cancel_task"]["destructiveHint"] is True


def _script(*arguments: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *arguments],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, **env},
        timeout=120,
    )


def _main(monkeypatch: pytest.MonkeyPatch, *arguments: str) -> int:
    monkeypatch.setattr(sys, "argv", [str(_SCRIPT), *arguments])
    try:
        runpy.run_path(str(_SCRIPT), run_name="__main__")
    except SystemExit as exit_status:
        return int(exit_status.code or 0)
    return 0


def test_script_checks_platform_endpoints_from_profiles(
    tmp_path: Path, fakes: RunningFakes
) -> None:
    profile = tmp_path / "jobs.json"
    profile.write_text(fakes.jobs_profile.model_dump_json(), encoding="utf-8")
    token = fakes.issuer.exchange(audience=JOBS_AUDIENCE)
    arguments = (
        "--inference-url",
        fakes.inference_url,
        "--inference-key-env",
        "SYNTHETIC_INFERENCE_KEY",
        "--mcp-profile",
        str(profile),
        "--mcp-token-env",
        "SYNTHETIC_JOBS_TOKEN",
    )
    env = {"SYNTHETIC_INFERENCE_KEY": INFERENCE_API_KEY, "SYNTHETIC_JOBS_TOKEN": token}

    passed = _script(*arguments, env=env)
    fakes.inference.available = False
    refused = _script(*arguments, env=env)

    assert passed.returncode == 0, passed.stderr
    assert [item["status"] for item in json.loads(passed.stdout)["results"]] == [
        "passed",
        "passed",
    ]
    assert refused.returncode == 1
    assert [
        (item["status"], item["diagnostic"]) for item in json.loads(refused.stdout)["results"]
    ] == [("unavailable", "HW-AGENT-012"), ("passed", None)]
    for output in (passed.stdout, passed.stderr, refused.stdout, refused.stderr):
        assert token not in output
        assert INFERENCE_API_KEY not in output


def test_script_runs_the_same_checks_against_the_fakes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    status = _main(monkeypatch, "--fakes")

    assert status == 0
    assert [item["target"] for item in json.loads(capsys.readouterr().out)["results"]] == [
        "inference",
        "synthetic-data",
        "synthetic-jobs",
        "synthetic-budgets",
    ]


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ((), "give either --fakes or endpoint options"),
        (("--model", "synthetic-coder"), "give --inference-url with inference options"),
        (("--fakes", "--model", "synthetic-coder"), "give either --fakes or endpoint options"),
        (
            ("--inference-url", "http://127.0.0.1:9/v1", "--inference-key-env", "UNSET_KEY"),
            "variable UNSET_KEY is not set",
        ),
        (
            (
                "--inference-url",
                "http://inference.synthetic.invalid/v1",
                "--inference-key-env",
                "KEY",
            ),
            "credentials are sent only over HTTPS or to a loopback address",
        ),
        (
            ("--mcp-profile", "missing.json", "--mcp-token-env", "KEY", "--mcp-token-env", "KEY"),
            "once for each",
        ),
    ],
)
def test_script_refuses_incomplete_or_unprotected_options(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    arguments: tuple[str, ...],
    message: str,
) -> None:
    monkeypatch.delenv("UNSET_KEY", raising=False)
    monkeypatch.setenv("KEY", "synthetic-key")

    assert _main(monkeypatch, *arguments) == 2
    assert message in capsys.readouterr().err


_PLAN = json.loads(
    (
        Path(__file__).parents[3]
        / "fixtures/synthetic/platform-contract/research-plan.r1.prediction.json"
    ).read_text("utf-8")
)


def _budgets(fakes: RunningFakes, tool: str, **arguments: object) -> dict[str, Any]:
    token = fakes.issuer.exchange(audience=BUDGETS_AUDIENCE)
    return _call(fakes.budgets_profile, token, tool, project_id=PROJECT_ID, **arguments)


def test_budget_service_reports_allocations_from_its_own_records(fakes: RunningFakes) -> None:
    before = _budgets(fakes, "get_budgets")["budgets"]
    fakes.budgets.record_spending("budget-compute", "12.5")
    after = _budgets(fakes, "get_budgets")["budgets"]

    for observation in (*before, *after):
        validate_platform_message("budget-observation.schema.json", observation)
    assert [(item["budget_id"], item["available"]) for item in before] == [
        ("budget-compute", "40"),
        ("budget-inference", "13"),
    ]
    assert {item["project_context_id"] for item in before} == {"launch-synthetic-1"}
    assert after[0]["available"] == "27.5"
    assert after[0]["platform_record_ref"] != before[0]["platform_record_ref"]


def test_budget_service_prices_plans_and_elements_from_its_price_sheet(
    fakes: RunningFakes,
) -> None:
    plan = _budgets(fakes, "estimate_workload", scope="plan", prediction=_PLAN)
    batch = _budgets(
        fakes, "estimate_workload", scope="elements", prediction=_PLAN, element_ids=["batch"]
    )

    for estimate in (plan, batch):
        validate_platform_message("platform-estimate.schema.json", estimate)
    assert (plan["total"]["expected"], plan["total"]["coverage"]) == ("24.7", "complete")
    assert {
        item["element_ids"][0]: item["cost"]["expected"] for item in plan["element_estimates"]
    } == {"exploration": "3.6", "batch": "19.8", "inference": "1.3"}
    assert batch["covered_element_ids"] == ["batch"]
    assert batch["total"]["expected"] == "19.8"
    assert plan["estimate_id"] != batch["estimate_id"]


def test_budget_service_refuses_other_bindings_and_reports_unpriced_work(
    fakes: RunningFakes,
) -> None:
    other = {
        **_PLAN,
        "project_context_id": "launch-synthetic-2",
        "evidence": [
            {**item, "project_context_id": "launch-synthetic-2"} for item in _PLAN["evidence"]
        ],
    }
    unpriced = json.loads(json.dumps(_PLAN))
    unpriced["elements"][0]["runtime_profile_ref"] = "synthetic-unlisted"

    with pytest.raises(PermissionError, match="another project context"):
        _budgets(fakes, "estimate_workload", scope="plan", prediction=other)
    with pytest.raises(PermissionError, match="invalid estimate scope"):
        _budgets(fakes, "estimate_workload", scope="elements", prediction=_PLAN)
    estimate = _budgets(fakes, "estimate_workload", scope="plan", prediction=unpriced)

    assert estimate["total"]["coverage"] == "partial"
    assert estimate["total"]["expected"] == "21.1"
    assert estimate["total"]["limitations"] == [
        "Runtime profile synthetic-unlisted has no price for this project."
    ]
