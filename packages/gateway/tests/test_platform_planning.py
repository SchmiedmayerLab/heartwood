# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Research planning against the deterministic platform budget and estimate fake."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

import pytest
from fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel

from heartwood.cli._interactive import InteractiveSession
from heartwood.gateway import (
    McpPlanningClient,
    PlanningDiscovery,
    PlanningRecordError,
    PlanningServiceError,
    ProjectContext,
    SessionGateway,
)
from heartwood.gateway._platform_planning import PlatformPlanningBinding
from heartwood.gateway._rest import RestGateway, RestRequest
from heartwood.gateway.platform_conformance import McpEndpoint
from heartwood.gateway.platform_fakes import (
    BUDGETS_AUDIENCE,
    OTHER_PROJECT_ID,
    PROJECT_ID,
    SYNTHETIC_LAUNCH_CONTEXTS,
    RunningFakes,
    running_platform_fakes,
    serve,
)
from heartwood.notebook import NotebookSession, build_planning_spec
from heartwood.schemas import (
    BudgetObservation,
    PlatformAuthorization,
    PlatformEstimate,
    PlatformMcpServiceProfile,
    PlatformOperationObservation,
    ResearcherDecision,
    WorkloadPrediction,
)
from heartwood.schemas.planning import PlanningEstimateRequest, PlanningProjection

_EXAMPLES = Path(__file__).parents[3] / "fixtures/synthetic/platform-contract"
_LAUNCH = SYNTHETIC_LAUNCH_CONTEXTS[0]
_START = datetime(2026, 7, 1, tzinfo=UTC)
_STATES = json.loads((_EXAMPLES / "failure-states.json").read_text("utf-8"))["states"]


def _prediction(revision: int) -> WorkloadPrediction:
    return WorkloadPrediction.model_validate_json(
        (_EXAMPLES / f"research-plan.r{revision}.prediction.json").read_text("utf-8")
    )


@dataclass
class _Clock:
    now: datetime = _START

    def __call__(self) -> datetime:
        return self.now


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


def _client(fakes: RunningFakes, *, project_id: str = PROJECT_ID) -> McpPlanningClient:
    token = fakes.issuer.exchange(audience=BUDGETS_AUDIENCE)
    return McpPlanningClient(McpEndpoint(fakes.budgets_profile, token), project_id=project_id)


def _gateway(tmp_path: Path, binding: PlatformPlanningBinding | None) -> SessionGateway:
    root = tmp_path / "project"
    root.mkdir(parents=True, exist_ok=True)
    identity = uuid4()
    project = ProjectContext(
        root, protected_state=tmp_path / "protected" / str(identity), project_id=identity
    )
    return SessionGateway(project=project, env={}, platform_planning=binding)


@pytest.fixture
def gateway(tmp_path: Path, fakes: RunningFakes, clock: _Clock) -> Iterator[SessionGateway]:
    instance = _gateway(tmp_path, PlatformPlanningBinding(_LAUNCH, _client(fakes), clock))
    try:
        yield instance
    finally:
        instance.stop()


def _estimate(gateway: SessionGateway, revision: int, *elements: str) -> PlanningProjection:
    return gateway.request_plan_estimate(
        PlanningEstimateRequest(
            prediction=_prediction(revision).prediction,
            scope="elements" if elements else "plan",
            element_ids=elements,
        )
    )


def _codes(projection: PlanningProjection) -> list[str]:
    return [item.code for item in projection.outcomes]


def test_discovery_reads_the_budgets_of_the_launch_context(gateway: SessionGateway) -> None:
    before = gateway.planning()
    assert before.offered is True
    assert before.services is not None
    assert before.services.budgets == "unchecked"

    projection = gateway.refresh_planning()

    assert projection.services is not None
    assert (projection.services.budgets, projection.services.estimates) == (
        "available",
        "available",
    )
    assert projection.launch == _LAUNCH
    assert {item.budget_id: item.available for item in projection.budgets} == {
        "budget-compute": "40",
        "budget-inference": "13",
    }
    assert {item.project_context_id for item in projection.budgets} == {"launch-synthetic-1"}
    assert projection.outcomes == ()


def test_complete_plan_and_element_estimates_stay_linked_without_double_counting(
    gateway: SessionGateway,
) -> None:
    gateway.record_plan_prediction(_prediction(1))
    _estimate(gateway, 1)
    projection = _estimate(gateway, 1, "batch")

    (plan,) = projection.plans
    (revision,) = plan.revisions
    whole, batch = revision.overview.estimates
    assert (whole.scope, whole.total.coverage, whole.total.expected) == ("plan", "complete", "24.7")
    assert (whole.total.lower, whole.total.upper) == ("18.54", "37.05")
    assert set(whole.covered_element_ids) == {"exploration", "batch", "inference"}
    # Breakdowns are part of the total, so they add up to it rather than to more.
    assert all(item.included_in_total for item in whole.element_estimates)
    assert sum(float(item.cost.expected or 0) for item in whole.element_estimates) == 24.7
    assert (batch.scope, batch.covered_element_ids, batch.total.expected) == (
        "elements",
        ("batch",),
        "19.8",
    )
    assert whole.focus_evidence[0].dataset == "SKU Price"
    # Only complete-plan estimates are compared; the element quote is not added to them.
    assert [row.estimate_id for row in plan.comparison] == [whole.estimate_id]
    assert [item.state for item in revision.estimates] == ["current", "current"]
    assert projection.outcomes == ()


def test_unsupported_work_stays_unknown_and_is_never_counted_as_zero(
    gateway: SessionGateway, fakes: RunningFakes
) -> None:
    fakes.budgets.estimate_kinds = frozenset({"exploration", "batch"})
    gateway.record_plan_prediction(_prediction(1))

    projection = _estimate(gateway, 1)

    (estimate,) = projection.plans[0].revisions[0].overview.estimates
    assert (estimate.total.coverage, estimate.total.expected) == ("partial", "23.4")
    assert estimate.total.limitations == ("The platform does not estimate inference work.",)
    inference = next(
        item for item in estimate.element_estimates if item.element_ids == ("inference",)
    )
    assert inference.cost.coverage == "unknown"
    assert inference.cost.expected is None
    assert _codes(projection) == ["HW-PLAN-005"]


def test_a_revised_plan_compares_runtime_configurations(gateway: SessionGateway) -> None:
    gateway.record_plan_prediction(_prediction(1))
    _estimate(gateway, 1)
    gateway.record_plan_prediction(_prediction(2))

    projection = _estimate(gateway, 2)

    comparison = projection.plans[0].comparison
    assert [row.prediction.revision for row in comparison] == [1, 2]
    assert [row.total.expected for row in comparison] == ["24.7", "22.5"]
    assert [row.runtime_profile_refs for row in comparison] == [
        ("synthetic-batch-small", "synthetic-coder", "synthetic-exploration-small"),
        ("synthetic-batch-gpu", "synthetic-coder", "synthetic-exploration-small"),
    ]
    assert all(row.comparable and row.state == "current" for row in comparison)


def test_expired_estimates_and_changed_budgets_ask_for_a_new_comparison(
    gateway: SessionGateway, fakes: RunningFakes, clock: _Clock
) -> None:
    gateway.refresh_planning()
    gateway.record_plan_prediction(_prediction(1))
    _estimate(gateway, 1)

    # Other work consumes the same allocation while the plan is being reviewed.
    fakes.budgets.record_spending("budget-compute", "30")
    changed = gateway.refresh_planning()
    clock.now += timedelta(hours=2)
    expired = gateway.planning()

    assert _codes(changed) == ["HW-PLAN-004"]
    assert {item.budget_id: item.available for item in changed.budgets}["budget-compute"] == "10"
    assert _codes(expired) == ["HW-PLAN-003", "HW-PLAN-004"]
    assert expired.plans[0].comparison[0].state == "expired"

    renewed = _estimate(gateway, 1)

    # The newer estimate replaces the stale one in the outcomes; both stay recorded.
    assert len(renewed.plans[0].revisions[0].overview.estimates) == 2
    assert renewed.outcomes == ()


def test_unavailable_service_keeps_recorded_planning_visible(
    gateway: SessionGateway, fakes: RunningFakes
) -> None:
    gateway.refresh_planning()
    gateway.record_plan_prediction(_prediction(1))
    _estimate(gateway, 1)
    fakes.budgets.available = False

    projection = gateway.refresh_planning()
    estimate = _estimate(gateway, 1, "batch")

    assert projection.services is not None
    assert projection.services.budgets == "unavailable"
    assert _codes(projection) == ["HW-PLAN-001"]
    assert len(projection.budgets) == 2
    assert len(estimate.plans[0].revisions[0].overview.estimates) == 1
    assert _codes(estimate) == ["HW-PLAN-001"]

    fakes.budgets.available = True
    assert gateway.refresh_planning().outcomes == ()


def test_the_platform_refuses_another_billing_binding(
    tmp_path: Path, fakes: RunningFakes, clock: _Clock
) -> None:
    # A client that names another platform project still holds project A's token.
    gateway = _gateway(
        tmp_path,
        PlatformPlanningBinding(_LAUNCH, _client(fakes, project_id=OTHER_PROJECT_ID), clock),
    )
    try:
        projection = gateway.refresh_planning()
        with pytest.raises(PlanningRecordError):
            gateway.record_plan_prediction(
                _prediction(1).model_copy(update={"project_context_id": "launch-synthetic-2"})
            )
    finally:
        gateway.stop()

    assert projection.services is not None
    assert projection.services.budgets == "refused"
    assert _codes(projection) == ["HW-PLAN-002"]
    assert projection.budgets == ()


@dataclass
class _ScriptedClient:
    """A planning client that returns fixed records, for checks of Heartwood's own rules."""

    discovery: PlanningDiscovery = field(default_factory=lambda: PlanningDiscovery(True, True))
    observations: tuple[BudgetObservation, ...] = ()
    answer: PlatformEstimate | None = None
    estimates: int = 0

    def discover(self) -> PlanningDiscovery:
        return self.discovery

    def budgets(self) -> tuple[BudgetObservation, ...]:
        return self.observations

    def estimate(
        self,
        prediction: WorkloadPrediction,  # noqa: ARG002
        *,
        scope: Literal["plan", "elements"],  # noqa: ARG002
        element_ids: Sequence[str],  # noqa: ARG002
    ) -> PlatformEstimate:
        self.estimates += 1
        if self.answer is None:
            raise PlanningServiceError("unavailable")
        return self.answer


def test_capabilities_that_are_not_offered_are_not_assumed(tmp_path: Path, clock: _Clock) -> None:
    client = _ScriptedClient(discovery=PlanningDiscovery(False, False))
    gateway = _gateway(tmp_path, PlatformPlanningBinding(_LAUNCH, client, clock))
    try:
        gateway.record_plan_prediction(_prediction(1))
        refreshed = gateway.refresh_planning()
        projection = _estimate(gateway, 1)
    finally:
        gateway.stop()

    assert refreshed.services is not None
    assert (refreshed.services.budgets, refreshed.services.estimates) == (
        "not-offered",
        "not-offered",
    )
    assert client.estimates == 0
    assert projection.plans[0].revisions[0].overview.estimates == ()
    assert projection.outcomes == ()


def test_an_estimate_tool_that_is_not_read_only_is_never_called() -> None:
    server = FastMCP("Synthetic planning service")
    calls: list[str] = []

    def get_budgets(project_id: str) -> dict[str, Any]:
        calls.append(project_id)
        return {"budgets": []}

    def estimate_workload(project_id: str) -> dict[str, Any]:
        calls.append(project_id)
        return {}

    read_only = ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    )
    persisting = read_only.model_copy(update={"read_only_hint": False})
    server.tool(get_budgets, annotations=read_only)
    server.tool(estimate_workload, annotations=persisting)
    with serve(server.http_app(path="/mcp", json_response=True, stateless_http=True)) as origin:
        profile = _profile(f"{origin}/mcp")
        client = McpPlanningClient(McpEndpoint(profile), project_id=PROJECT_ID)

        assert client.discover() == PlanningDiscovery(budgets=True, estimates=False)
    assert calls == []


def _profile(url: str) -> PlatformMcpServiceProfile:
    return PlatformMcpServiceProfile.model_validate_json(
        json.dumps(
            {
                "service_id": "synthetic-planning",
                "url": url,
                "credential_ref": "synthetic-planning-token",
                "authority_ref": "synthetic-platform",
                "policy_ref": "synthetic-policy-v1",
                "supported_protocol_versions": ["2025-11-25"],
            }
        )
    )


def test_platform_records_must_match_the_launch_context_and_request(
    tmp_path: Path, fakes: RunningFakes, clock: _Clock
) -> None:
    gateway = _gateway(tmp_path, PlatformPlanningBinding(_LAUNCH, _client(fakes), clock))
    try:
        gateway.record_plan_prediction(_prediction(1))
        genuine = _estimate(gateway, 1).plans[0].revisions[0].overview.estimates[0]
    finally:
        gateway.stop()
    substituted = genuine.model_copy(
        update={"project_context_id": "launch-synthetic-2", "estimate_id": "est-substituted"}
    )
    other_scope = genuine.model_copy(update={"estimate_id": "est-other"})
    observation = BudgetObservation.model_validate_json(
        json.dumps(
            {
                "project_context_id": "launch-synthetic-2",
                "budget_id": "budget-compute-b",
                "observed_at": "2026-07-01T00:00:00Z",
                "denomination": {"kind": "currency", "unit": "USD"},
                "allocated": "5",
                "reserved": "0",
                "spent": "0",
                "available": "5",
                "platform_record_ref": "synthetic-budget:budget-compute-b:1",
            }
        )
    )
    client = _ScriptedClient(observations=(observation,), answer=substituted)
    scripted = _gateway(tmp_path / "scripted", PlatformPlanningBinding(_LAUNCH, client, clock))
    try:
        scripted.record_plan_prediction(_prediction(1))
        refused_budget = scripted.refresh_planning()
        refused_estimate = _estimate(scripted, 1)
        client.answer = other_scope
        wrong_scope = _estimate(scripted, 1, "batch")
    finally:
        scripted.stop()

    assert refused_budget.budgets == ()
    assert _codes(refused_budget) == ["HW-PLAN-001"]
    assert refused_estimate.plans[0].revisions[0].overview.estimates == ()
    assert _codes(refused_estimate) == ["HW-PLAN-001"]
    assert wrong_scope.plans[0].revisions[0].overview.estimates == ()


def test_prediction_revisions_are_immutable_and_chained(gateway: SessionGateway) -> None:
    with pytest.raises(PlanningRecordError, match="superseded"):
        gateway.record_plan_prediction(_prediction(2))
    gateway.record_plan_prediction(_prediction(1))
    gateway.record_plan_prediction(_prediction(1))
    changed = _prediction(1).model_copy(update={"uncertainty": "Changed after review."})

    with pytest.raises(PlanningRecordError, match="already recorded"):
        gateway.record_plan_prediction(changed)
    with pytest.raises(PlanningRecordError, match="not recorded"):
        gateway.request_plan_estimate(PlanningEstimateRequest(prediction=_prediction(2).prediction))
    with pytest.raises(PlanningRecordError, match="Name existing elements"):
        _estimate(gateway, 1, "unknown-element")
    assert len(gateway.planning().plans[0].revisions) == 1


def _proposal(estimate: PlatformEstimate, operations: dict[str, tuple[str, ...]]) -> bytes:
    request = {
        "service_id": "synthetic-jobs",
        "protocol": "ga4gh-tes",
        "request_ref": "project://requests/bootstrap.json",
        "request_sha256": hashlib.sha256(b"synthetic request").hexdigest(),
    }
    document = {
        "schema_version": "heartwood.operation-set-proposal.v1",
        "project_context_id": estimate.project_context_id,
        "proposal": {"record_id": "cohort-proposal", "revision": estimate.prediction.revision},
        "plan": estimate.plan.model_dump(mode="json"),
        "prediction": estimate.prediction.model_dump(mode="json"),
        "created_at": "2026-07-01T00:00:00Z",
        "operations": [
            {
                "operation_id": operation_id,
                "element_ids": list(elements),
                "description": f"Synthetic {operation_id} operation",
                "resource_limits": [],
                "data_scope": "Synthetic cohort extract",
                "destination": "project://results",
                "estimate_ids": [estimate.estimate_id],
                "request": request,
            }
            for operation_id, elements in operations.items()
        ],
        "estimates": [estimate.model_dump(mode="json")],
    }
    return json.dumps(document, indent=2).encode()


def _record[T: BaseModel](model: type[T], **values: object) -> T:
    return model.model_validate_json(json.dumps(values))


def test_approval_scope_survives_revisions_and_platform_denials_stay_visible(
    gateway: SessionGateway,
) -> None:
    gateway.refresh_planning()
    gateway.record_plan_prediction(_prediction(1))
    estimate = _estimate(gateway, 1).plans[0].revisions[0].overview.estimates[0]
    document = _proposal(estimate, {"op-explore": ("exploration",), "op-batch": ("batch",)})
    digest = hashlib.sha256(document).hexdigest()
    planning = gateway.platform_planning
    assert planning is not None
    proposal = {"record_id": "cohort-proposal", "revision": 1}

    planning.link_operation_set(document, reference="project://proposals/cohort-1.json")
    planning.link_platform_record(
        _record(
            ResearcherDecision,
            project_context_id=_LAUNCH.project_context_id,
            decision_id="decision-1",
            proposal=proposal,
            document_sha256=digest,
            decision="approved",
            decided_at="2026-07-01T00:05:00Z",
            actor_ref="synthetic-researcher",
            audit_ref="session:planning/audit/42",
        )
    )
    # The researcher approved, and the platform still refused: approval is not admission.
    planning.link_platform_record(
        _record(
            PlatformAuthorization,
            project_context_id=_LAUNCH.project_context_id,
            authorization_id="authorization-1",
            proposal=proposal,
            document_sha256=digest,
            status="denied",
            observed_at="2026-07-01T00:06:00Z",
            budget_id="budget-compute",
            reason="Synthetic allocation is committed to other work.",
            platform_record_ref="synthetic-audit:authorization-1",
        )
    )
    planning.link_platform_record(
        _record(
            PlatformOperationObservation,
            project_context_id=_LAUNCH.project_context_id,
            operation_id="op-batch",
            service_id="synthetic-jobs",
            backend_operation_ref="op-batch",
            observed_at="2026-07-01T00:06:00Z",
            status="denied",
            platform_record_ref="synthetic-audit:op-batch",
        )
    )
    first = gateway.planning()
    gateway.record_plan_prediction(_prediction(2))
    revised = gateway.planning()

    reviews = {item.element_id: item for item in first.plans[0].revisions[0].elements}
    assert {key: (item.review, item.platform) for key, item in reviews.items()} == {
        "exploration": ("approved", "denied"),
        "batch": ("approved", "denied"),
        # Agreeing to a proposal does not approve elements it does not contain.
        "inference": ("requires-review", "not-requested"),
    }
    assert first.plans[0].revisions[0].overview.researcher_decisions[0].audit_ref == (
        "session:planning/audit/42"
    )
    assert _codes(first) == ["HW-PLAN-006", "HW-PLAN-006"]
    later = {item.element_id: item for item in revised.plans[0].revisions[1].elements}
    assert {key: item.review for key, item in later.items()} == {
        "exploration": "unchanged-since-approval",
        "batch": "requires-review",
        "inference": "requires-review",
    }
    assert later["exploration"].approved_in == _prediction(1).prediction
    # Denials in the superseded revision no longer need attention in the latest one.
    assert _codes(revised) == []


def test_linked_records_must_bind_the_reviewed_proposal(gateway: SessionGateway) -> None:
    gateway.record_plan_prediction(_prediction(1))
    estimate = _estimate(gateway, 1).plans[0].revisions[0].overview.estimates[0]
    document = _proposal(estimate, {"op-batch": ("batch",)})
    planning = gateway.platform_planning
    assert planning is not None
    planning.link_operation_set(document, reference="project://proposals/cohort-1.json")
    planning.link_operation_set(document, reference="project://proposals/cohort-1.json")

    with pytest.raises(PlanningRecordError):
        planning.link_platform_record(
            _record(
                ResearcherDecision,
                project_context_id=_LAUNCH.project_context_id,
                decision_id="decision-1",
                proposal={"record_id": "cohort-proposal", "revision": 1},
                document_sha256=hashlib.sha256(document + b" ").hexdigest(),
                decision="approved",
                decided_at="2026-07-01T00:05:00Z",
                actor_ref="synthetic-researcher",
                audit_ref="session:planning/audit/42",
            )
        )
    with pytest.raises(PlanningRecordError, match="different record"):
        planning.link_operation_set(document + b"\n", reference="project://proposals/other.json")
    with pytest.raises(PlanningRecordError, match="invalid"):
        planning.link_operation_set(b"{", reference="project://proposals/broken.json")


def test_planning_state_survives_a_gateway_restart(
    tmp_path: Path, fakes: RunningFakes, clock: _Clock
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    identity = uuid4()
    project = ProjectContext(
        root, protected_state=tmp_path / "protected" / str(identity), project_id=identity
    )
    binding = PlatformPlanningBinding(_LAUNCH, _client(fakes), clock)
    first = SessionGateway(project=project, env={}, platform_planning=binding)
    try:
        first.refresh_planning()
        first.record_plan_prediction(_prediction(1))
        expected = _estimate(first, 1)
    finally:
        first.stop()
    second = SessionGateway(project=project, env={}, platform_planning=binding)
    try:
        restored = second.planning()
    finally:
        second.stop()

    assert restored.plans == expected.plans
    assert restored.budgets == expected.budgets
    assert not (root / ".heartwood").exists()


def test_rest_routes_share_the_projection_and_hide_an_unoffered_service(
    tmp_path: Path, gateway: SessionGateway
) -> None:
    rest = RestGateway(gateway)
    body = (_EXAMPLES / "research-plan.r1.prediction.json").read_text("utf-8")

    recorded = rest.handle(RestRequest("POST", "/project/planning/predictions", body))
    estimated = rest.handle(
        RestRequest(
            "POST",
            "/project/planning/estimates",
            json.dumps({"prediction": {"record_id": "cohort-prediction", "revision": 1}}),
        )
    )
    viewed = rest.handle(RestRequest("GET", "/project/planning"))
    invalid = rest.handle(RestRequest("POST", "/project/planning/estimates", "{}"))

    assert recorded.status_code == estimated.status_code == viewed.status_code == 200
    assert viewed.body == gateway.planning().model_dump(mode="json")
    assert invalid.status_code == 422

    plain = _gateway(tmp_path / "plain", None)
    try:
        assert plain.planning() == PlanningProjection(offered=False)
        refused = RestGateway(plain).handle(RestRequest("POST", "/project/planning/refresh"))
    finally:
        plain.stop()
    assert refused.status_code == 404


def test_planning_requires_protected_state(tmp_path: Path, clock: _Clock) -> None:
    with pytest.raises(ValueError, match="protected state"):
        SessionGateway(
            project=ProjectContext(tmp_path),
            env={},
            platform_planning=PlatformPlanningBinding(_LAUNCH, _ScriptedClient(), clock),
        )


def _produce(
    code: str, gateway: SessionGateway, fakes: RunningFakes, clock: _Clock
) -> PlanningProjection:
    """Bring the plan into the state that reports ``code``."""
    gateway.record_plan_prediction(_prediction(1))
    if code == "HW-PLAN-001":
        fakes.budgets.available = False
        return gateway.refresh_planning()
    planning = gateway.platform_planning
    assert planning is not None
    if code == "HW-PLAN-002":
        # The token is for project A; the platform refuses a request naming project B.
        planning.client = _client(fakes, project_id=OTHER_PROJECT_ID)
        return gateway.refresh_planning()
    if code == "HW-PLAN-005":
        fakes.budgets.estimate_kinds = frozenset({"exploration", "batch"})
        return _estimate(gateway, 1)
    gateway.refresh_planning()
    estimate = _estimate(gateway, 1).plans[0].revisions[0].overview.estimates[0]
    if code == "HW-PLAN-003":
        clock.now += timedelta(hours=2)
        return gateway.planning()
    if code == "HW-PLAN-004":
        fakes.budgets.set_allocation("budget-compute", "25")
        return gateway.refresh_planning()
    document = _proposal(estimate, {"op-batch": ("batch",)})
    planning.link_operation_set(document, reference="project://proposals/cohort-1.json")
    planning.link_platform_record(
        _record(
            PlatformAuthorization,
            project_context_id=_LAUNCH.project_context_id,
            authorization_id="authorization-1",
            proposal={"record_id": "cohort-proposal", "revision": 1},
            document_sha256=hashlib.sha256(document).hexdigest(),
            status="denied",
            observed_at="2026-07-01T00:06:00Z",
            reason="Synthetic allocation is committed to other work.",
            platform_record_ref="synthetic-audit:authorization-1",
        )
    )
    return gateway.planning()


@pytest.mark.parametrize(
    "state",
    [item for item in _STATES if item["surface"] == "planning"],
    ids=lambda item: item["code"],
)
def test_planning_outcomes_show_the_same_diagnostic_in_every_interface(
    gateway: SessionGateway, fakes: RunningFakes, clock: _Clock, state: dict[str, str]
) -> None:
    projection = _produce(state["code"], gateway, fakes, clock)
    browser = RestGateway(gateway).handle(RestRequest("GET", "/project/planning")).body
    terminal = InteractiveSession(gateway, session_id="planning").submit("/plan")
    notebook = build_planning_spec(NotebookSession(gateway=gateway).planning())

    assert [item.diagnostic for item in projection.outcomes] == [state["diagnostic"]]
    assert [item["diagnostic"] for item in cast(list[Any], browser["outcomes"])] == [
        state["diagnostic"]
    ]
    shown = {"terminal": (terminal.message or "").splitlines(), "notebook": notebook.items}
    for interface, lines in shown.items():
        assert set(re.findall(r"HW-[A-Z]+-\d{3}", "\n".join(lines))) == {state["code"]}, interface
        assert any(line.startswith(f"{state['diagnostic']} (") for line in lines), interface


def test_terminal_plan_commands_record_estimate_and_hide_without_planning(
    tmp_path: Path, gateway: SessionGateway
) -> None:
    (gateway.project.root / "plan.json").write_bytes(
        (_EXAMPLES / "research-plan.r1.prediction.json").read_bytes()
    )
    terminal = InteractiveSession(gateway, session_id="planning")

    recorded = terminal.submit("/plan record plan.json")
    estimated = terminal.submit("/plan estimate cohort-prediction 1 batch")
    outside = terminal.submit("/plan record ../outside.json")
    malformed = terminal.submit("/plan estimate cohort-prediction one")

    assert not recorded.error
    assert "Revision 1: prediction cohort-prediction revision 1" in (recorded.message or "")
    assert "for elements batch: 19.8 USD (from 15.84 USD to 29.7 USD), complete coverage" in (
        estimated.message or ""
    )
    assert outside.error
    assert malformed.error
    assert "/plan" in (terminal.submit("/help").message or "")

    plain = _gateway(tmp_path / "plain", None)
    try:
        hidden = InteractiveSession(plain, session_id="planning")
        assert "/plan" not in (hidden.submit("/help").message or "")
        assert (hidden.submit("/plan").message or "").startswith("Unknown command")
    finally:
        plain.stop()
