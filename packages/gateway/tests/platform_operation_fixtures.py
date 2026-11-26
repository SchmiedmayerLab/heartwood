# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""A gateway connected to the job and budget fakes, with a synthetic worker that calls them."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Never

from pydantic import AnyHttpUrl

from heartwood.core_adapter import (
    BackendEventSink,
    BackendToolCallEvent,
    DeterministicAgentBackend,
    ProposedToolCall,
    SessionService,
    TokenDeltaSink,
)
from heartwood.core_adapter.external_actions import (
    ExternalActionRecord,
    external_action_records,
)
from heartwood.core_adapter.worker_attempts import WorkerControlPlane, WorkerRuntimeBinding
from heartwood.gateway import (
    McpPlanningClient,
    ProjectContext,
    SessionGateway,
)
from heartwood.gateway._access import AccessUnavailableError, TokenExchangeRefusedError
from heartwood.gateway._mcp_policy import (
    McpDestination,
    McpOperationProfile,
    TokenExchange,
)
from heartwood.gateway._mcp_servers import McpOutsideAdapter, advance_outside_calls
from heartwood.gateway._model_settings import ModelProfile, ModelSettings
from heartwood.gateway._platform_planning import PlatformPlanningBinding
from heartwood.gateway._session_projection import (
    ProjectionActionRecord,
    project_session,
)
from heartwood.gateway.platform_conformance import McpEndpoint
from heartwood.gateway.platform_fakes import (
    BUDGETS_AUDIENCE,
    JOBS_AUDIENCE,
    PROJECT_ID,
    SYNTHETIC_LAUNCH_CONTEXTS,
    FakeBudgetService,
    FakeJobService,
    FakeTokenIssuer,
    serve,
)
from heartwood.gateway.worker_sessions import WorkerSessionPlan, WorkerSessionRequest
from heartwood.schemas import (
    McpServerAddRequest,
    McpServerEnabledRequest,
    McpServerTestRequest,
    McpServerToolsRequest,
    McpToolChoice,
    PolicyProfile,
    WorkloadPrediction,
)
from heartwood.schemas.planning import PlanningEstimateRequest
from heartwood.session import CommandKind, SessionCommand

EXAMPLES = Path(__file__).parents[3] / "fixtures/synthetic/platform-contract"
LAUNCH = SYNTHETIC_LAUNCH_CONTEXTS[0]
START = datetime(2026, 7, 1, tzinfo=UTC)
CONTROL_PLANE = WorkerControlPlane(control_plane_sha256="1" * 64, container_runtime_sha256="2" * 64)
CPU = {
    "name": "synthetic-analysis",
    "command": ["python", "analysis.py"],
    "resources": {"cpu_cores": 2, "ram_gb": 4.0, "max_runtime_seconds": 1800},
    "inputs": ["obj-cohort"],
}
GPU = {
    "name": "synthetic-training",
    "command": ["python", "train.py"],
    "resources": {"cpu_cores": 4, "ram_gb": 16.0, "gpu_type": "synthetic-gpu", "gpu_count": 1},
}


@dataclass
class _Clock:
    now: datetime = START

    def __call__(self) -> datetime:
        return self.now


class _Workers:
    """A worker platform that is never launched; sessions here run the synthetic worker."""

    def prepare(self, request: WorkerSessionRequest) -> WorkerSessionPlan:
        del request

        def unused(*_args: object) -> Never:
            raise RuntimeError("the synthetic worker is not launched")

        return WorkerSessionPlan(
            "sha256:" + "a" * 64, "b" * 64, "c" * 64, unused, unused, unused, CONTROL_PLANE
        )


class ProposalWorker(DeterministicAgentBackend):
    """Stands in for the sandboxed worker that emits client-tool calls."""

    def bind_runtime(self, *, event_sink: BackendEventSink, token_sink: TokenDeltaSink) -> None:
        super().bind_runtime(event_sink=event_sink, token_sink=token_sink)
        self.emit = event_sink

    def call(self, tool_name: str, arguments: dict[str, Any]) -> None:
        identity = uuid.uuid4().hex
        self.emit(
            (
                BackendToolCallEvent(
                    tool_call=ProposedToolCall(
                        tool_call_id=f"call-{identity}",
                        tool_name=tool_name,
                        risk="unknown",
                        summary="Run a synthetic job",
                        arguments=arguments,
                    ),
                    source_event_id=f"event-{identity}",
                ),
            )
        )


@dataclass
class Platform:
    gateway: SessionGateway
    jobs: FakeJobService
    budgets: FakeBudgetService
    clock: _Clock
    session: SessionService
    worker: ProposalWorker
    adapter: McpOutsideAdapter

    def tool(self, name: str) -> str:
        return next(item for item in self.adapter.tool_names if f"__{name}_" in item)

    def attempt(self, name: str, arguments: dict[str, Any]) -> ExternalActionRecord | None:
        """Emit one worker call and return the review it opened, if any."""
        before = set(external_action_records(self.session.replay_events()))
        self.worker.call(self.tool(name), arguments)
        self.advance()
        created = [
            record
            for key, record in external_action_records(self.session.replay_events()).items()
            if key not in before
        ]
        assert len(created) <= 1
        return created[0] if created else None

    def call(self, name: str, arguments: dict[str, Any]) -> ExternalActionRecord:
        record = self.attempt(name, arguments)
        assert record is not None
        return record

    def advance(self) -> None:
        # A fresh interval each time, so status is read on every call.
        self.adapter.last_reconciled = 0.0
        advance_outside_calls(self.session, self.adapter, clock=lambda: 1e12)

    def decide(self, record: ExternalActionRecord, kind: CommandKind = CommandKind.APPROVE) -> str:
        command_id = str(uuid.uuid4())
        self.session.handle(
            SessionCommand(
                command_id=command_id,
                session_id=self.session.store.session_id,
                kind=kind,
                actor_id="researcher",
                created_at=datetime.now(UTC).isoformat(),
                payload={"target_type": "action-set", "target_id": record.proposal.group_id},
            )
        )
        self.advance()
        return command_id

    def record(self, record: ExternalActionRecord) -> ExternalActionRecord:
        return external_action_records(self.session.replay_events())[record.proposal.operation_id]

    def action(self, record: ExternalActionRecord) -> ProjectionActionRecord:
        projection = project_session(
            self.session.replay_events(), session_id=self.session.store.session_id
        )
        return next(
            item for item in projection.actions if item.action_id == record.proposal.action_id
        )

    def plan(self, revision: int, *elements: str) -> None:
        prediction = WorkloadPrediction.model_validate_json(
            (EXAMPLES / f"research-plan.r{revision}.prediction.json").read_text("utf-8")
        )
        self.gateway.record_plan_prediction(prediction)
        self.gateway.request_plan_estimate(
            PlanningEstimateRequest(
                prediction=prediction.prediction,
                scope="elements" if elements else "plan",
                element_ids=elements,
            )
        )


@dataclass
class SignIn:
    """The researcher's sign-in at the gateway, as the gateway's token exchange sees it."""

    state: Literal["live", "ended", "refused", "provider-unavailable"] = "live"
    issuer: FakeTokenIssuer | None = None

    def __call__(self, scope: str, audience: str) -> str | None:
        del scope
        if self.state == "provider-unavailable":
            raise AccessUnavailableError(502)
        if self.state == "refused":
            raise TokenExchangeRefusedError
        if self.state == "ended" or self.issuer is None:
            return None
        return self.issuer.exchange(audience=audience, project_id=PROJECT_ID)


@contextmanager
def job_platform(
    tmp_path: Path,
    *,
    planning: bool = True,
    gpu_projects: frozenset[str] = frozenset(),
    sign_in: SignIn | None = None,
) -> Iterator[Platform]:
    """Connect the job service; with ``sign_in`` it takes the token exchanged for it."""
    clock = _Clock()
    issuer = FakeTokenIssuer()
    jobs = FakeJobService(
        issuer,
        gpu_projects=gpu_projects,
        clock=lambda: clock.now,
        contexts={PROJECT_ID: LAUNCH.project_context_id},
    )
    budgets = FakeBudgetService(issuer, clock=lambda: clock.now)
    if sign_in is not None:
        sign_in.issuer = issuer
    with serve(jobs.app) as jobs_origin, serve(budgets.app) as budgets_origin:
        destination = McpDestination(
            service_id=jobs.service_id,
            origin=jobs_origin,
            tools=("create_task", "cancel_task"),
            observation_tool="get_task",
            operations=McpOperationProfile(
                capabilities_tool="describe_operations",
                proposal_tool="propose_task",
                cancel_tool="cancel_task",
            ),
            call_timeout_seconds=2,
            token_exchange=(
                None if sign_in is None else TokenExchange(scope="svc-jobs", audience=JOBS_AUDIENCE)
            ),
        )
        root = tmp_path / "project"
        root.mkdir()
        identity = uuid.uuid4()
        binding = (
            PlatformPlanningBinding(
                LAUNCH,
                McpPlanningClient(
                    McpEndpoint(
                        budgets.profile(f"{budgets_origin}/mcp"),
                        issuer.exchange(audience=BUDGETS_AUDIENCE),
                    ),
                    project_id=PROJECT_ID,
                ),
                clock,
            )
            if planning
            else None
        )
        gateway = SessionGateway(
            project=ProjectContext(
                root, protected_state=tmp_path / "protected" / str(identity), project_id=identity
            ),
            worker_plan_factory=_Workers().prepare,
            mcp_destinations=(destination,),
            platform_planning=binding,
            service_tokens=sign_in,
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
                model_settings=ModelSettings(
                    active_profile=profile.profile_id, profiles=(profile,)
                ),
                policy=PolicyProfile(
                    policy_id="synthetic",
                    platform_id="generic",
                    allowed_model_endpoints=(profile.policy_endpoint,),
                ),
            )
        )
        project = PROJECT_ID if planning else str(identity)
        gateway.add_mcp_server(
            McpServerAddRequest(
                server_id="jobs",
                label="Synthetic jobs",
                url=f"{jobs_origin}/mcp",
                token=(
                    issuer.exchange(audience=JOBS_AUDIENCE, project_id=project)
                    if sign_in is None
                    else None
                ),
            )
        )
        tested = gateway.test_mcp_server("jobs", McpServerTestRequest())
        digests = {tool["name"]: tool["sha256"] for tool in tested["tools"]}
        gateway.select_mcp_tools(
            "jobs",
            McpServerToolsRequest(
                tools=[
                    McpToolChoice(name=name, sha256=digests[name])
                    for name in ("create_task", "cancel_task")
                ]
            ),
        )
        gateway.set_mcp_server_enabled("jobs", McpServerEnabledRequest(enabled=True))
        adapter = McpOutsideAdapter(
            gateway.mcp_servers,
            gateway.mcp_servers.bound_tools(),
            planning=gateway.platform_planning,
        )
        worker = ProposalWorker()
        session = SessionService.local_default(
            tmp_path / "sessions",
            backend=worker,
            worker_project_id=identity,
            external_actions=adapter,
        )
        try:
            intent = session.prepare_worker_attempt(
                image_digest="sha256:" + "b" * 64,
                sandbox_policy_sha256="c" * 64,
                input_manifest_sha256="d" * 64,
                control_plane=CONTROL_PLANE,
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
            yield Platform(gateway, jobs, budgets, clock, session, worker, adapter)
        finally:
            session.close()
            gateway.stop()
