# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Planning tools for a worker's agent, called through outside review.

The platform is the deterministic budget and estimate fake; the worker is a stand-in
that emits client-tool calls.
"""

from __future__ import annotations

import itertools
import json
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal, cast

import pytest
from openhands.sdk import LocalConversation
from openhands.sdk.conversation import BaseConversation
from openhands.sdk.event import ActionEvent, ObservationEvent
from openhands.sdk.event import Event as OpenHandsEvent
from openhands.sdk.llm import LLMStreamChunk, Message, MessageToolCall, TextContent
from openhands.sdk.settings import OpenHandsAgentSettings
from openhands.sdk.testing import TestLLM
from openhands.sdk.tool import ClientToolSpec, register_client_tools
from openhands.sdk.tool.client_tool import extract_client_tool_specs
from pydantic import AnyHttpUrl

from heartwood.core_adapter import PendingActionGroup, SessionService
from heartwood.core_adapter.external_actions import ExternalActionError, ExternalActionRecord
from heartwood.core_adapter.external_results import external_result_records
from heartwood.core_adapter.worker_attempts import WorkerRuntimeBinding
from heartwood.gateway import (
    OpenHandsSdkBackend,
    PlanningDiscovery,
    ProjectContext,
    SessionGateway,
)
from heartwood.gateway._mcp_servers import McpOutsideAdapter, advance_outside_calls
from heartwood.gateway._model_settings import ModelProfile, ModelSettings
from heartwood.gateway._openhands_persistence import ContentMinimizedLocalFileStore
from heartwood.gateway._openhands_sdk import _agent_context, _security_configuration, _tool_call
from heartwood.gateway._platform_planning import (
    AGENT_RESULT_BYTES,
    ESTIMATE_AGENT_TOOL,
    OVERVIEW_AGENT_TOOL,
    PLANNING_SERVICE_ID,
    PREDICTION_AGENT_TOOL,
    McpPlanningClient,
    PlatformPlanning,
    PlatformPlanningBinding,
)
from heartwood.gateway._remote_workspace import _admit_client_tools, _client_tool_contract
from heartwood.gateway.platform_fakes import (
    OTHER_PROJECT_ID,
    RunningFakes,
    running_platform_fakes,
)
from heartwood.schemas import (
    BudgetObservation,
    PlatformEstimate,
    PlatformOperationObservation,
    PolicyProfile,
    WorkloadPrediction,
)
from heartwood.schemas.planning import PlanningEstimateRequest
from heartwood.session import CommandKind, SessionCommand
from packages.gateway.tests.test_mcp_servers import (
    _CONTROL_PLANE,
    Platform,
    ProposalWorker,
    _decide,
    _records,
)
from packages.gateway.tests.test_platform_planning import (
    _LAUNCH,
    _client,
    _Clock,
    _gateway,
    _prediction,
)

_TOOLS = {OVERVIEW_AGENT_TOOL, PREDICTION_AGENT_TOOL, ESTIMATE_AGENT_TOOL}
# Each read is past the interval at which finished calls are offered for release.
_later = itertools.count(1e9, 60.0).__next__


@pytest.fixture(autouse=True)
def _loopback_without_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def fakes(clock: _Clock) -> Iterator[RunningFakes]:
    with running_platform_fakes() as running:
        running.budgets.clock = clock
        yield running


def _worker_gateway(
    tmp_path: Path, binding: PlatformPlanningBinding | None, platform: Platform | None
) -> SessionGateway:
    root = tmp_path / "project"
    root.mkdir()
    identity = uuid.uuid4()
    gateway = SessionGateway(
        project=ProjectContext(
            root, protected_state=tmp_path / "protected" / str(identity), project_id=identity
        ),
        worker_plan_factory=None if platform is None else platform.prepare,
        platform_planning=binding,
        env={},
    )
    profile = ModelProfile(
        profile_id="synthetic",
        model="openai/synthetic",
        base_url="http://127.0.0.1:9999/v1",
        policy_endpoint="http://127.0.0.1:9999/v1/chat/completions",
        credential_kind="none",
    )
    gateway.config_store.save(
        replace(
            gateway.config_store.load(),
            model_settings=ModelSettings(active_profile=profile.profile_id, profiles=(profile,)),
            policy=PolicyProfile(
                policy_id="synthetic",
                platform_id="generic",
                allowed_model_endpoints=(profile.policy_endpoint,),
            ),
        )
    )
    return gateway


@contextmanager
def _session(
    tmp_path: Path, gateway: SessionGateway
) -> Iterator[tuple[SessionService, ProposalWorker, McpOutsideAdapter]]:
    """Run a session whose admitted worker has the gateway's planning tools."""
    gateway.planning()
    adapter = McpOutsideAdapter(gateway.mcp_servers, (), planning=gateway.platform_planning)
    worker = ProposalWorker()
    session = SessionService.local_default(
        tmp_path / "sessions",
        backend=worker,
        worker_project_id=gateway.project.project_id,
        external_actions=adapter,
    )
    try:
        intent = session.prepare_worker_attempt(
            image_digest="sha256:" + "b" * 64,
            sandbox_policy_sha256="c" * 64,
            input_manifest_sha256="d" * 64,
            control_plane=_CONTROL_PLANE,
        )
        session.bind_worker_runtime(
            attempt_id=intent.attempt_id,
            runtime=WorkerRuntimeBinding(
                sandbox_id="synthetic-worker",
                endpoint=AnyHttpUrl("http://worker.test"),
                image_digest=intent.image_digest,
                sandbox_policy_sha256=intent.sandbox_policy_sha256,
            ),
        )
        yield session, worker, adapter
    finally:
        session.close()
        gateway.stop()


def _propose(
    session: SessionService,
    worker: ProposalWorker,
    adapter: McpOutsideAdapter,
    tool: str,
    arguments: dict[str, Any],
) -> ExternalActionRecord:
    worker.call(tool, arguments)
    advance_outside_calls(session, adapter)
    return _records(session)[-1]


def _run(
    session: SessionService,
    worker: ProposalWorker,
    adapter: McpOutsideAdapter,
    tool: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Approve one agent call and return what its result release would show the model."""
    _decide(session, _propose(session, worker, adapter, tool, arguments), CommandKind.APPROVE)
    advance_outside_calls(session, adapter, clock=_later)
    release = list(external_result_records(session.replay_events()).values())[-1]
    assert release.phase == "proposed"
    content: dict[str, Any] = json.loads(release.proposal.content_json)
    return content


def _record(prediction: WorkloadPrediction) -> dict[str, Any]:
    return {"prediction": prediction.model_dump(mode="json")}


def _estimate(prediction: WorkloadPrediction) -> dict[str, Any]:
    return {"prediction": prediction.prediction.model_dump(mode="json"), "scope": "plan"}


# Binding


def test_a_deployment_without_planning_gives_the_agent_no_planning_tools(tmp_path: Path) -> None:
    platform = Platform()
    gateway = _worker_gateway(tmp_path, None, platform)
    try:
        session = gateway._service("session-plain")
        request = gateway._worker_sessions[id(session)]._request
        assert request.client_tools == ()
        assert id(session) not in gateway._mcp_adapters
    finally:
        gateway.stop()


def test_worker_sessions_receive_the_planning_tools(
    tmp_path: Path, fakes: RunningFakes, clock: _Clock
) -> None:
    platform = Platform()
    gateway = _worker_gateway(
        tmp_path, PlatformPlanningBinding(_LAUNCH, _client(fakes), clock), platform
    )
    try:
        session = gateway._service("session-planning")
        request = gateway._worker_sessions[id(session)]._request
        assert {item.name for item in request.client_tools} == _TOOLS
        assert gateway._mcp_adapters[id(session)].tool_names == _TOOLS
        assert all(item.annotations is None for item in request.client_tools)
    finally:
        gateway.stop()


def test_sessions_without_a_worker_receive_no_planning_tools(
    tmp_path: Path, fakes: RunningFakes, clock: _Clock
) -> None:
    gateway = _worker_gateway(
        tmp_path, PlatformPlanningBinding(_LAUNCH, _client(fakes), clock), None
    )
    try:
        session = gateway._service("session-local")
        assert id(session) not in gateway._mcp_adapters
        assert id(session) not in gateway._worker_sessions
    finally:
        gateway.stop()


# Calls


class _BudgetsOnly:
    """A planning service that offers budgets and no estimates."""

    def __init__(self, client: McpPlanningClient) -> None:
        self.client = client
        self.estimates = 0

    def discover(self) -> PlanningDiscovery:
        return PlanningDiscovery(budgets=self.client.discover().budgets, estimates=False)

    def budgets(self) -> tuple[BudgetObservation, ...]:
        return self.client.budgets()

    def estimate(
        self,
        prediction: WorkloadPrediction,
        *,
        scope: Literal["plan", "elements"],
        element_ids: Sequence[str],
    ) -> PlatformEstimate:
        self.estimates += 1
        return self.client.estimate(prediction, scope=scope, element_ids=element_ids)


def test_the_overview_reports_a_service_that_offers_only_budgets(
    tmp_path: Path, fakes: RunningFakes, clock: _Clock
) -> None:
    client = _BudgetsOnly(_client(fakes))
    gateway = _gateway(tmp_path, PlatformPlanningBinding(_LAUNCH, client, clock))
    with _session(tmp_path, gateway) as (session, worker, adapter):
        overview = _run(session, worker, adapter, OVERVIEW_AGENT_TOOL, {})
        _run(session, worker, adapter, PREDICTION_AGENT_TOOL, _record(_prediction(1)))
        estimated = _run(session, worker, adapter, ESTIMATE_AGENT_TOOL, _estimate(_prediction(1)))

    assert overview["service_id"] == PLANNING_SERVICE_ID
    assert overview["status"] == "completed"
    view = overview["result"]
    assert view["project_context_id"] == _LAUNCH.project_context_id
    assert (view["services"]["budgets"], view["services"]["estimates"]) == (
        "available",
        "not-offered",
    )
    assert view["budgets"]
    assert all(item["observed_at"] for item in view["budgets"])
    assert client.estimates == 0
    assert estimated["status"] == "completed"
    assert estimated["result"]["services"]["estimates"] == "not-offered"
    assert estimated["result"]["plans"][0]["revisions"][0]["estimates"] == []
    # The launch context's billing and authority references stay out of the agent's view.
    shown = json.dumps(overview)
    for value in (
        _LAUNCH.billing_attribution_ref,
        _LAUNCH.authority_ref,
        _LAUNCH.policy_ref,
        _LAUNCH.project_id,
        _LAUNCH.platform_id,
    ):
        assert value is None or f'"{value}"' not in shown


def test_agent_records_appear_in_the_researcher_projection(
    tmp_path: Path, fakes: RunningFakes, clock: _Clock
) -> None:
    gateway = _gateway(tmp_path, PlatformPlanningBinding(_LAUNCH, _client(fakes), clock))
    with _session(tmp_path, gateway) as (session, worker, adapter):
        _run(session, worker, adapter, OVERVIEW_AGENT_TOOL, {})
        _run(session, worker, adapter, PREDICTION_AGENT_TOOL, _record(_prediction(1)))
        estimated = _run(session, worker, adapter, ESTIMATE_AGENT_TOOL, _estimate(_prediction(1)))
        projection = gateway.planning()

    (plan,) = projection.plans
    (revision,) = plan.revisions
    assert revision.overview.prediction == _prediction(1)
    (estimate,) = revision.overview.estimates
    (agent_revision,) = estimated["result"]["plans"][0]["revisions"]
    assert [item["estimate_id"] for item in agent_revision["estimates"]] == [estimate.estimate_id]
    assert agent_revision["estimates"][0]["state"] == "current"
    assert estimated["result"]["services"]["estimates"] == "available"


def test_planning_outcomes_reach_the_agent_as_every_interface_shows_them(
    tmp_path: Path, fakes: RunningFakes, clock: _Clock
) -> None:
    gateway = _gateway(tmp_path, PlatformPlanningBinding(_LAUNCH, _client(fakes), clock))
    fakes.budgets.available = False
    with _session(tmp_path, gateway) as (session, worker, adapter):
        overview = _run(session, worker, adapter, OVERVIEW_AGENT_TOOL, {})
        projection = gateway.planning()

    codes = [item["code"] for item in overview["result"]["outcomes"]]
    assert codes == ["HW-PLAN-001"]
    assert codes == [item.code for item in projection.outcomes]
    assert overview["result"]["services"]["budgets"] == "unavailable"


@pytest.mark.parametrize(
    ("tool", "arguments", "reason"),
    [
        (
            PREDICTION_AGENT_TOOL,
            _record(
                WorkloadPrediction.model_validate_json(
                    _prediction(1)
                    .model_dump_json()
                    .replace(_LAUNCH.project_context_id, "launch-synthetic-2")
                )
            ),
            "The prediction belongs to another platform project context",
        ),
        (
            PREDICTION_AGENT_TOOL,
            {"prediction": {"schema_version": "heartwood.workload-prediction.v1"}},
            "The arguments do not match the planning tool",
        ),
        (
            ESTIMATE_AGENT_TOOL,
            {"prediction": {"record_id": "cohort-prediction", "revision": 0}},
            "The arguments do not match the planning tool",
        ),
        (
            OVERVIEW_AGENT_TOOL,
            {"project_id": "other-project"},
            "The arguments do not match the planning tool",
        ),
    ],
)
def test_a_refused_call_reaches_the_agent_through_review_and_records_nothing(
    tmp_path: Path,
    fakes: RunningFakes,
    clock: _Clock,
    tool: str,
    arguments: dict[str, Any],
    reason: str,
) -> None:
    gateway = _gateway(tmp_path, PlatformPlanningBinding(_LAUNCH, _client(fakes), clock))
    with _session(tmp_path, gateway) as (session, worker, adapter):
        record = _propose(session, worker, adapter, tool, arguments)
        # The researcher sees the refusal and its reason before anything reaches the model.
        assert record.phase == "proposed"
        assert record.proposal.request.summary == f"Refuse {tool}: {reason}"
        assert json.loads(record.proposal.request.request_json) == {"refusal": reason}
        _decide(session, record, CommandKind.APPROVE)
        advance_outside_calls(session, adapter, clock=_later)
        (release,) = external_result_records(session.replay_events()).values()
        refused = json.loads(release.proposal.content_json)
        projection = gateway.planning()

    assert refused["status"] == "failed"
    assert refused["limitations"] == [reason]
    assert projection.plans == ()
    services = projection.services
    # Nothing contacted the platform: discovery never ran.
    assert services is not None
    assert services.budgets == "unchecked"


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        (OVERVIEW_AGENT_TOOL, {}),
        (PREDICTION_AGENT_TOOL, _record(_prediction(1))),
        (ESTIMATE_AGENT_TOOL, _estimate(_prediction(1))),
    ],
)
def test_the_sdk_fields_of_a_call_stay_out_of_the_reviewed_request(
    tmp_path: Path, fakes: RunningFakes, clock: _Clock, tool: str, arguments: dict[str, Any]
) -> None:
    gateway = _gateway(tmp_path, PlatformPlanningBinding(_LAUNCH, _client(fakes), clock))
    with _session(tmp_path, gateway) as (_session_service, _worker, adapter):
        plain = adapter.prepare(tool, arguments)
        called = adapter.prepare(
            tool, {**arguments, "summary": "Plan the run", "security_risk": "LOW"}
        )

    assert not plain.summary.startswith("Refuse")
    assert called == plain


def test_a_refused_record_is_reported_to_the_agent(
    tmp_path: Path, fakes: RunningFakes, clock: _Clock
) -> None:
    gateway = _gateway(tmp_path, PlatformPlanningBinding(_LAUNCH, _client(fakes), clock))
    with _session(tmp_path, gateway) as (session, worker, adapter):
        refused = _run(session, worker, adapter, ESTIMATE_AGENT_TOOL, _estimate(_prediction(1)))

    assert refused["status"] == "failed"
    assert refused["limitations"] == ["The prediction revision is not recorded"]
    assert "result" not in refused


def test_planning_calls_wait_for_review_and_a_denied_call_records_nothing(
    tmp_path: Path, fakes: RunningFakes, clock: _Clock
) -> None:
    gateway = _gateway(tmp_path, PlatformPlanningBinding(_LAUNCH, _client(fakes), clock))
    with _session(tmp_path, gateway) as (session, worker, adapter):
        record = _propose(session, worker, adapter, PREDICTION_AGENT_TOOL, _record(_prediction(1)))
        assert record.phase == "proposed"
        assert record.proposal.request.service_id == PLANNING_SERVICE_ID
        assert record.proposal.request.project_context_id == _LAUNCH.project_context_id
        assert json.loads(record.proposal.request.request_json) == _record(_prediction(1))
        assert gateway.planning().plans == ()
        _decide(session, record, CommandKind.DENY)
        advance_outside_calls(session, adapter, clock=_later)
        assert _records(session)[-1].phase == "denied"
        assert gateway.planning().plans == ()

        overview = _propose(session, worker, adapter, OVERVIEW_AGENT_TOOL, {"summary": "Plan"})
        assert overview.proposal.request.request_json == "{}"
        _decide(session, overview, CommandKind.APPROVE)
        # The result reaches the model only through its own release review.
        assert external_result_records(session.replay_events()) == {}
        advance_outside_calls(session, adapter, clock=_later)
        (release,) = external_result_records(session.replay_events()).values()
        assert release.phase == "proposed"


@pytest.mark.parametrize("recorded", [True, False])
def test_a_call_with_an_unknown_outcome_is_reconciled_without_running_again(
    tmp_path: Path,
    fakes: RunningFakes,
    clock: _Clock,
    monkeypatch: pytest.MonkeyPatch,
    recorded: bool,
) -> None:
    gateway = _gateway(tmp_path, PlatformPlanningBinding(_LAUNCH, _client(fakes), clock))
    planning = gateway.platform_planning
    assert planning is not None
    record_prediction = planning.record_prediction
    calls: list[WorkloadPrediction] = []

    def lost_reply(prediction: WorkloadPrediction) -> None:
        calls.append(prediction)
        if recorded:
            record_prediction(prediction)
        raise RuntimeError("the reply was lost")

    monkeypatch.setattr(planning, "record_prediction", lost_reply)
    with _session(tmp_path, gateway) as (session, worker, adapter):
        record = _propose(session, worker, adapter, PREDICTION_AGENT_TOOL, _record(_prediction(1)))
        _decide(session, record, CommandKind.APPROVE)
        assert _records(session)[-1].phase == "unknown"
        advance_outside_calls(session, adapter, clock=_later)
        settled = _records(session)[-1]
        plans = gateway.planning().plans

    assert len(calls) == 1
    assert settled.observation is not None
    observed = settled.observation.operation
    if recorded:
        assert observed.status == "completed"
        assert len(_view(observed)["plans"]) == 1
        assert len(plans) == 1
    else:
        assert observed.status == "failed"
        assert observed.limitations == ("The prediction revision was not recorded",)
        assert plans == ()


def test_a_lost_estimate_says_whether_the_journal_holds_one(
    tmp_path: Path, fakes: RunningFakes, clock: _Clock
) -> None:
    gateway = _gateway(tmp_path, PlatformPlanningBinding(_LAUNCH, _client(fakes), clock))
    planning = gateway.platform_planning
    assert planning is not None
    request = json.dumps(_estimate(_prediction(1)))
    try:
        gateway.record_plan_prediction(_prediction(1))
        before = planning.agent_outcome(ESTIMATE_AGENT_TOOL, request, operation_id="lost-1")
        gateway.refresh_planning()
        gateway.request_plan_estimate(
            PlanningEstimateRequest(prediction=_prediction(1).prediction, scope="plan")
        )
        after = planning.agent_outcome(ESTIMATE_AGENT_TOOL, request, operation_id="lost-1")
    finally:
        gateway.stop()

    assert before.status == after.status == "completed"
    assert "holds no estimate for this revision and scope" in before.limitations[0]
    assert "holds an estimate for this revision and scope" in after.limitations[0]


def test_a_planning_call_needs_its_launch_context_and_an_unexpired_review(
    tmp_path: Path, fakes: RunningFakes, clock: _Clock
) -> None:
    gateway = _gateway(tmp_path, PlatformPlanningBinding(_LAUNCH, _client(fakes), clock))
    with _session(tmp_path, gateway) as (session, worker, adapter):
        proposal = _propose(session, worker, adapter, OVERVIEW_AGENT_TOOL, {}).proposal
        request = proposal.request
        adapter.check_policy(proposal, "researcher", proposal.created_at)
        for changed in (
            request.model_copy(update={"project_context_id": None}),
            request.model_copy(update={"project_context_id": "launch-synthetic-2"}),
        ):
            with pytest.raises(ExternalActionError):
                adapter.check_policy(
                    proposal.model_copy(update={"request": changed}),
                    "researcher",
                    proposal.created_at,
                )
        with pytest.raises(ExternalActionError, match="expired"):
            adapter.check_policy(proposal, "researcher", proposal.expires_at)
        # Another launch context changes the policy digest, so an open review is stale.
        other = McpOutsideAdapter(
            gateway.mcp_servers,
            (),
            planning=PlatformPlanning(
                PlatformPlanningBinding(
                    _LAUNCH.model_copy(update={"project_context_id": "launch-synthetic-2"}),
                    _client(fakes),
                    clock,
                ),
                journal_path=tmp_path / "other-planning.jsonl",
                contains_file=lambda _path: True,
                run_ids=set,
            ),
        )
        assert other.policy_sha256 != adapter.policy_sha256


def test_a_refused_service_is_reported_to_the_agent(
    tmp_path: Path, fakes: RunningFakes, clock: _Clock
) -> None:
    # A client that names another platform project still holds this project's token.
    gateway = _gateway(
        tmp_path,
        PlatformPlanningBinding(_LAUNCH, _client(fakes, project_id=OTHER_PROJECT_ID), clock),
    )
    with _session(tmp_path, gateway) as (session, worker, adapter):
        overview = _run(session, worker, adapter, OVERVIEW_AGENT_TOOL, {})

    assert overview["result"]["services"]["budgets"] == "refused"
    assert [item["code"] for item in overview["result"]["outcomes"]] == ["HW-PLAN-002"]


def test_a_large_overview_keeps_service_status_and_budgets(
    tmp_path: Path, fakes: RunningFakes, clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = _gateway(tmp_path, PlatformPlanningBinding(_LAUNCH, _client(fakes), clock))
    planning = gateway.platform_planning
    assert planning is not None
    gateway.refresh_planning()
    first, second = _prediction(1), _prediction(2)
    gateway.record_plan_prediction(first)
    gateway.record_plan_prediction(second)
    full = planning.agent_observation(OVERVIEW_AGENT_TOOL, operation_id="overview-1")
    assert full.limitations == ()

    def overview(limit: int) -> PlatformOperationObservation:
        monkeypatch.setattr("heartwood.gateway._platform_planning.AGENT_RESULT_BYTES", limit)
        return planning.agent_observation(OVERVIEW_AGENT_TOOL, operation_id="overview-1")

    latest = overview(_size(full) - 1)
    shortest = overview(_size(latest) - 1)
    refused = overview(_size(shortest) - 1)
    gateway.stop()

    assert latest.status == "completed"
    (plan,) = _view(latest)["plans"]
    assert [item["prediction"]["revision"] for item in plan["revisions"]] == [2]
    assert "latest revision" in latest.limitations[0]
    assert shortest.status == "completed"
    assert _view(shortest)["plans"] == []
    assert _view(shortest)["services"] == _view(full)["services"]
    assert _view(shortest)["budgets"] == _view(full)["budgets"]
    assert refused.status == "failed"


def test_a_planning_result_over_its_limit_is_not_released(
    tmp_path: Path, fakes: RunningFakes, clock: _Clock
) -> None:
    gateway = _gateway(tmp_path, PlatformPlanningBinding(_LAUNCH, _client(fakes), clock))
    with _session(tmp_path, gateway) as (session, worker, adapter):
        record = _propose(session, worker, adapter, OVERVIEW_AGENT_TOOL, {})
        _decide(session, record, CommandKind.APPROVE)
        observed = _records(session)[-1]
        content = adapter.prepare_result(observed)
        adapter.check_result_policy(
            observed, content, "{}", "researcher", record.proposal.created_at
        )
        with pytest.raises(ExternalActionError, match="size limit"):
            adapter.check_result_policy(
                observed,
                "x" * (AGENT_RESULT_BYTES + 1),
                "{}",
                "researcher",
                record.proposal.created_at,
            )


def _view(observation: PlatformOperationObservation) -> dict[str, Any]:
    assert observation.result is not None
    return cast(dict[str, Any], observation.result)


def _size(observation: PlatformOperationObservation) -> int:
    content = json.dumps(observation.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return len(content.encode())


# OpenHands conformance


def test_an_openhands_agent_calls_a_planning_tool_as_a_reviewed_operation(
    tmp_path: Path, fakes: RunningFakes, clock: _Clock
) -> None:
    gateway = _gateway(tmp_path, PlatformPlanningBinding(_LAUNCH, _client(fakes), clock))
    with _session(tmp_path, gateway) as (session, worker, adapter):
        specs = list(adapter.client_tools)
        worker_tools = register_client_tools(specs)
        # The specifications the worker conversation carries pass the gateway's admission.
        _admit_client_tools(
            [item.model_dump(mode="json") for item in extract_client_tool_specs(worker_tools)],
            _client_tool_contract(specs),
        )
        observed: list[OpenHandsEvent] = []
        conversation: BaseConversation = LocalConversation(
            agent=OpenHandsAgentSettings(
                llm=TestLLM.from_messages(
                    [
                        Message(
                            role="assistant",
                            content=[TextContent(text="")],
                            tool_calls=[
                                MessageToolCall(
                                    id="call-overview",
                                    name=OVERVIEW_AGENT_TOOL,
                                    arguments=json.dumps(
                                        {"summary": "Check planning", "security_risk": "LOW"}
                                    ),
                                    origin="completion",
                                )
                            ],
                        ),
                        Message(role="assistant", content=[TextContent(text="Asked.")]),
                    ]
                ),
                tools=worker_tools,
                agent_context=_agent_context([]),
            ).create_agent(),
            workspace=tmp_path,
            persistence_dir=tmp_path / "openhands",
            callbacks=[observed.append],
            visualizer=None,
        )
        try:
            conversation.send_message("Check what the platform offers for planning.")
            conversation.run()
        finally:
            conversation.close()
        # The SDK only acknowledges the call; the gateway runs it after review.
        (action,) = [event for event in observed if isinstance(event, ActionEvent)]
        (observation,) = [event for event in observed if isinstance(event, ObservationEvent)]
        assert observation.observation.text == "Tool call dispatched to client."
        assert _budgets(gateway) == "unchecked"

        # OpenHands keeps its own fields in the call; the reviewed request leaves them out.
        call = _tool_call(action)
        assert call.arguments == {"summary": "Check planning", "security_risk": "LOW"}
        record = _propose(session, worker, adapter, call.tool_name, dict(call.arguments))
        assert record.phase == "proposed"
        assert record.proposal.request.tool_name == OVERVIEW_AGENT_TOOL
        assert record.proposal.request.request_json == "{}"
        _decide(session, record, CommandKind.APPROVE)
        assert _budgets(gateway) == "available"


def test_the_openhands_backend_proposes_planning_calls_the_adapter_reviews(
    tmp_path: Path, fakes: RunningFakes, clock: _Clock
) -> None:
    """Heartwood's OpenHands backend turns model calls into the arguments the adapter binds."""
    gateway = _gateway(tmp_path, PlatformPlanningBinding(_LAUNCH, _client(fakes), clock))
    sdk_fields = {"summary": "Plan the cohort run", "security_risk": "LOW"}
    calls = {
        OVERVIEW_AGENT_TOOL: {**sdk_fields},
        PREDICTION_AGENT_TOOL: {**_record(_prediction(1)), **sdk_fields},
    }
    with _session(tmp_path, gateway) as (session, worker, adapter):
        backend_session = _backend_session(
            tmp_path / "backend",
            list(adapter.client_tools),
            [
                Message(
                    role="assistant",
                    content=[TextContent(text="")],
                    tool_calls=[
                        MessageToolCall(
                            id=f"call-{name}",
                            name=name,
                            arguments=json.dumps(arguments),
                            origin="completion",
                        )
                        for name, arguments in calls.items()
                    ],
                ),
                Message(role="assistant", content=[TextContent(text="Asked.")]),
            ],
        )
        try:
            backend_session.handle(
                SessionCommand(
                    command_id="turn-1",
                    session_id="backend-session",
                    kind=CommandKind.CHAT,
                    actor_id="researcher",
                    created_at="2026-07-01T00:00:00Z",
                    payload={"prompt": "Check planning and record the prediction."},
                )
            )
            group = _pending_group(backend_session)
        finally:
            backend_session.close()
        proposed = {action.tool_name: dict(action.arguments) for action in group.actions}
        assert proposed == calls

        for name in calls:
            record = _propose(session, worker, adapter, name, proposed[name])
            assert record.phase == "proposed"
            assert not {"summary", "security_risk"} & set(
                json.loads(record.proposal.request.request_json)
            )
            _decide(session, record, CommandKind.APPROVE)
            assert _records(session)[-1].observation is not None
        projection = gateway.planning()

    assert projection.plans[0].revisions[0].overview.prediction == _prediction(1)
    assert projection.services is not None
    assert projection.services.budgets == "available"


def _backend_session(
    root: Path, specs: list[ClientToolSpec], script: list[Message | Exception]
) -> SessionService:
    """Run Heartwood's OpenHands backend with the planning tools and a scripted model."""
    workspace = root / "workspace"
    workspace.mkdir(parents=True)
    persistence = root / "openhands"

    def factory(
        callback: Callable[[OpenHandsEvent], None],
        token_callback: Callable[[LLMStreamChunk], None],
    ) -> BaseConversation:
        conversation_id = uuid.uuid4()
        conversation: BaseConversation = LocalConversation(
            agent=OpenHandsAgentSettings(
                llm=TestLLM.from_messages(script),
                tools=register_client_tools(specs),
                agent_context=_agent_context([]),
                enable_classify_and_switch_llm_tool=False,
                tool_concurrency_limit=1,
            ).create_agent(),
            workspace=workspace,
            persistence_dir=persistence,
            conversation_id=conversation_id,
            profile_store_dir=persistence / "profiles",
            file_store=ContentMinimizedLocalFileStore(
                LocalConversation.get_persistence_dir(persistence, conversation_id)
            ),
            callbacks=[callback],
            token_callbacks=[token_callback],
            visualizer=None,
            delete_on_close=False,
        )
        # The same analyzers and confirm-every-action policy as Heartwood's backend.
        analyzer, policy = _security_configuration()
        conversation.set_security_analyzer(analyzer)
        conversation.set_confirmation_policy(policy)
        return conversation

    backend = OpenHandsSdkBackend(
        profile=ModelProfile(
            profile_id="heartwood",
            model="openai/local-model",
            base_url="http://127.0.0.1:8765/v1",
            policy_endpoint="http://127.0.0.1:8765/v1/chat/completions",
            credential_kind="none",
        ),
        workspace=workspace,
        persistence_dir=persistence,
        conversation_key="planning-tools",
        env={},
        conversation_factory=factory,
    )
    return SessionService.local_default(
        root / "sessions",
        session_id="backend-session",
        backend=backend,
        env={},
        clock=lambda: "2026-07-01T00:00:00Z",
    )


def _pending_group(service: SessionService) -> PendingActionGroup:
    backend = cast(OpenHandsSdkBackend, service.backend)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        group = backend.pending_action_group(session_id="backend-session")
        if group is not None:
            return group
        time.sleep(0.02)
    pytest.fail("OpenHands did not request confirmation")


def _budgets(gateway: SessionGateway) -> str:
    services = gateway.planning().services
    assert services is not None
    return services.budgets
