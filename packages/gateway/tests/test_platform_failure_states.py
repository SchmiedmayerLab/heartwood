# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Platform contract failure states, produced through the fakes and shown by every interface.

Session failures run the real OpenHands backend against the inference fake, so the
diagnostic comes from the runtime's own failure classification. The terminal and notebook
render the resulting projection here; the browser test renders the same fixture.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

import heartwood.gateway._openhands_sdk as openhands_sdk_module
from heartwood.cli import main as terminal_main
from heartwood.cli._interactive import format_projection_lines
from heartwood.core_adapter import (
    AgentBackend,
    BackendEvent,
    DeterministicAgentBackend,
    SessionService,
)
from heartwood.core_adapter.worker_attempts import WorkerGatewayCapabilityError
from heartwood.gateway import (
    GatewayAsgiApp,
    IdentityAccess,
    IdentityDeployment,
    IngressPolicy,
    LaunchCapability,
    ModelProfile,
    OpenHandsSdkBackend,
    ProjectContext,
    RestGateway,
    RestRequest,
    SessionGateway,
    SessionProjection,
    project_session,
)
from heartwood.gateway._openhands_failures import backend_error
from heartwood.gateway.platform_conformance import run_platform_conformance
from heartwood.gateway.platform_fakes import (
    INFERENCE_API_KEY,
    RunningFakes,
    running_platform_fakes,
)
from heartwood.notebook import NotebookSession, build_view_model, build_widget_spec
from heartwood.schemas import PolicyProfile
from heartwood.session import CommandKind, EventKind, SessionCommand, SessionEvent
from packages.gateway.tests.platform_operation_fixtures import CPU, SignIn, job_platform

_ROOT = Path(__file__).resolve().parents[3]
_STATES = json.loads(
    (_ROOT / "fixtures/synthetic/platform-contract/failure-states.json").read_text("utf-8")
)["states"]
_KEY_NAME = "HEARTWOOD_SYNTHETIC_PLATFORM_KEY"
_SESSION = "platform-failure"
# Gateway refusals that only identity-proxy ingress produces.
_IDENTITY_CODES = {"HW-INGRESS-004", "HW-INGRESS-005"}
_SECRET = "synthetic" * 5


@pytest.fixture(autouse=True)
def _fast_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    monkeypatch.setattr(openhands_sdk_module, "_AGENT_LLM_LOCAL_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(openhands_sdk_module, "_AGENT_LLM_RETRY_MIN_WAIT_SECONDS", 0)
    monkeypatch.setattr(openhands_sdk_module, "_AGENT_LLM_RETRY_MAX_WAIT_SECONDS", 0)
    monkeypatch.setattr(openhands_sdk_module, "_AGENT_PROGRESS_POLL_SECONDS", 0.01)


@pytest.fixture
def fakes() -> Iterator[RunningFakes]:
    with running_platform_fakes() as running:
        yield running


def _service(tmp_path: Path, fakes: RunningFakes) -> SessionService:
    profile = ModelProfile(
        profile_id="synthetic-platform",
        model="openai/synthetic-coder",
        policy_endpoint=f"{fakes.inference_url}/chat/completions",
        base_url=fakes.inference_url,
        credential_kind="environment",
        api_key_env=_KEY_NAME,
        max_input_tokens=16_384,
        max_output_tokens=256,
    )
    environment = {_KEY_NAME: INFERENCE_API_KEY}
    backend = OpenHandsSdkBackend(
        profile=profile,
        workspace=tmp_path / "project",
        persistence_dir=tmp_path / "openhands",
        conversation_key=f"platform-failure:{tmp_path.name}",
        credential_environment_names=(_KEY_NAME,),
        env=environment,
    )
    return _session_service(tmp_path, backend, profile.policy_endpoint, environment)


def _session_service(
    tmp_path: Path, backend: AgentBackend, endpoint: str, environment: dict[str, str]
) -> SessionService:
    (tmp_path / "project").mkdir(exist_ok=True)
    policy = PolicyProfile(
        policy_id="synthetic-platform",
        platform_id="generic",
        allowed_model_endpoints=(endpoint,),
        allowed_capability_tiers=("supervised",),
        allowed_action_review_modes=("review-all",),
        credential_allowlist=tuple(environment),
    )
    return SessionService.local_default(
        tmp_path / "sessions",
        session_id=_SESSION,
        backend=backend,
        policy_profile=policy,
        env=environment,
        clock=lambda: "2026-07-26T00:00:00Z",
    )


def _chat(service: SessionService) -> tuple[SessionEvent, ...]:
    _submit(service)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        events = service.replay_events()
        statuses = [
            event.payload.get("status")
            for event in events
            if event.kind == EventKind.AGENT_LIFECYCLE_UPDATED
        ]
        settled = any(event.kind == EventKind.ERROR_RECORDED for event in events)
        if statuses and (statuses[-1] == "finished" or (statuses[-1] == "error" and settled)):
            return events
        time.sleep(0.02)
    raise AssertionError("synthetic platform task did not settle")


def _submit(service: SessionService) -> None:
    service.handle(
        SessionCommand(
            command_id="platform-task",
            session_id=_SESSION,
            kind=CommandKind.CHAT,
            actor_id="synthetic-user",
            created_at="2026-07-26T00:00:00Z",
            payload={"prompt": "Return one short synthetic result without using tools."},
        )
    )


def _assert_every_interface_shows(projection: SessionProjection, diagnostic: str) -> None:
    browser = projection.model_dump(mode="json", by_alias=True)
    terminal = format_projection_lines(projection)
    notebook = [
        item
        for section in build_widget_spec(build_view_model(projection))
        for item in section.items
    ]

    code = diagnostic.split(":", 1)[0]
    shown = {
        "browser": [str(message["detail"]) for message in browser["conversation"]],
        "terminal": list(terminal),
        "notebook": notebook,
    }
    for interface, lines in shown.items():
        # Each interface shows this diagnostic and no other stable code.
        assert set(re.findall(r"HW-[A-Z]+-\d{3}", "\n".join(lines))) == {code}, interface
    assert diagnostic in shown["browser"]
    assert any(line.strip() == diagnostic for line in terminal)
    assert any(item.endswith(f"\n{diagnostic}") for item in notebook)


def test_fixture_covers_each_coded_failure_state_in_the_contract() -> None:
    contract = (_ROOT / "documentation/operate/platform-contract.md").read_text("utf-8")
    table = contract.split("### Failure States", 1)[1].split("\n\n", 2)[1]

    assert set(re.findall(r"HW-[A-Z]+-\d{3}", table)) == {state["code"] for state in _STATES}
    # Each surface below has a test that shows its states in every interface, except
    # the gateway-response states HW-INGRESS-002 and HW-INGRESS-003: they refuse HTTP
    # requests, which only the browser sends, so their tests check the HTTP refusal.
    # HW-INGRESS-004 and HW-INGRESS-005 also refuse platform calls, shown everywhere.
    # Model discovery states are tested in test_platform_model_connections.py,
    # planning states in test_platform_planning.py and operation states in
    # test_platform_operations.py.
    assert {state["surface"] for state in _STATES} == {
        "gateway-response",
        "model-discovery",
        "operation",
        "planning",
        "session",
        "setup-refusal",
    }


@pytest.mark.parametrize(
    "state",
    [
        pytest.param(
            state,
            id=state["code"],
            marks=(
                pytest.mark.xfail(
                    strict=True,
                    reason=(
                        "OpenHands also records its rate-limit classification of the quota "
                        "response, so every interface shows HW-AGENT-010 beside HW-AGENT-009"
                    ),
                )
                if state["inference_failure"] == "quota"
                else ()
            ),
        )
        for state in _STATES
        if "inference_failure" in state
    ],
)
def test_inference_failures_show_the_same_diagnostic_in_every_interface(
    tmp_path: Path, fakes: RunningFakes, state: dict[str, str]
) -> None:
    fakes.inference.failure = state["inference_failure"]  # type: ignore[assignment]
    service = _service(tmp_path, fakes)
    try:
        events = _chat(service)
    finally:
        service.close()
    projection = project_session(events, session_id=_SESSION)

    _assert_every_interface_shows(projection, state["diagnostic"])
    assert projection.lifecycle.status == "error"
    assert projection.last_command_outcome is not None
    assert projection.last_command_outcome.error_code == state["code"]


class _IncapableWorkerGateway(DeterministicAgentBackend):
    def submit_turn(self, *, session_id: str, prompt: str) -> tuple[BackendEvent, ...]:  # noqa: ARG002
        return (backend_error(WorkerGatewayCapabilityError("synthetic gateway lacks deletion")),)


def test_worker_gateway_failure_shows_the_same_diagnostic_in_every_interface(
    tmp_path: Path,
) -> None:
    state = next(item for item in _STATES if item["code"] == "HW-WORKER-001")
    backend = _IncapableWorkerGateway()
    service = _session_service(tmp_path, backend, backend.model_endpoint, {})
    try:
        _submit(service)
        events = service.replay_events()
    finally:
        service.close()

    _assert_every_interface_shows(project_session(events, session_id=_SESSION), state["diagnostic"])


@pytest.mark.parametrize(
    "state",
    [
        pytest.param(item, id=item["code"])
        for item in _STATES
        if item["surface"] == "gateway-response" and item["code"] not in _IDENTITY_CODES
    ],
)
def test_gateway_refusals_carry_the_fixture_diagnostic(
    tmp_path: Path, state: dict[str, str]
) -> None:
    gateway = SessionGateway(project=ProjectContext(tmp_path), env={}, backend_id="deterministic")
    ingress = IngressPolicy.create(external_base_path="/proxy/8767")
    app = GatewayAsgiApp(
        gateway,
        access=LaunchCapability(_SECRET, launch_token="synthetic-launch-token"),
        ingress=ingress,
    )
    path = "/proxy/8767/sessions" if state["code"] == "HW-INGRESS-003" else "/sessions"

    async def request() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=cast(Any, app)), base_url="http://127.0.0.1:8767"
        ) as client:
            return await client.get(path, headers={"origin": "http://127.0.0.1:8767"})

    try:
        body = asyncio.run(request()).json()
    finally:
        gateway.stop()

    assert f"{body['code']}: {body['error']}" == state["diagnostic"]


@pytest.mark.parametrize(
    "state",
    [
        pytest.param(item, id=item["code"])
        for item in _STATES
        if item["surface"] == "gateway-response" and item["code"] in _IDENTITY_CODES
    ],
)
def test_identity_refusals_carry_the_fixture_diagnostic(
    tmp_path: Path, state: dict[str, str]
) -> None:
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("synthetic provider detail", request=request)

    origin = "http://gateway.synthetic.invalid"
    ingress = IngressPolicy.create(
        mode="identity-proxy",
        bind_host="0.0.0.0",
        external_origin=origin,
        trusted_proxy_sources=("127.0.0.1",),
    )
    access = IdentityAccess(
        IdentityDeployment.model_validate(
            {
                "issuer": "https://identity.synthetic.invalid",
                "client_id": "hw-proj-synthetic",
                "client_secret_file": str(tmp_path / "client-secret"),
                "actor_key_file": str(tmp_path / "actor-key"),
                "subject": "auth0|synthetic-researcher",
            }
        ),
        ingress,
        client_secret=_SECRET,
        actor_key=_SECRET.encode(),
        http=httpx.Client(transport=httpx.MockTransport(unreachable)),
    )
    gateway = SessionGateway(project=ProjectContext(tmp_path), env={}, backend_id="deterministic")
    app = GatewayAsgiApp(gateway, access=access, ingress=ingress)
    # A request without identity is refused; a sign-in needs the provider, which is down.
    path = "/sessions" if state["code"] == "HW-INGRESS-004" else "/identity/login"

    async def request() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=cast(Any, app)), base_url=origin
        ) as client:
            return await client.get(path)

    try:
        body = asyncio.run(request()).json()
    finally:
        gateway.stop()

    assert f"{body['code']}: {body['error']}" == state["diagnostic"]


@pytest.mark.parametrize(
    "state",
    [
        pytest.param(item, id=item["code"])
        for item in _STATES
        if item["surface"] == "gateway-response" and item["code"] in _IDENTITY_CODES
    ],
)
def test_identity_refusals_of_a_platform_call_show_the_same_diagnostic_in_every_interface(
    tmp_path: Path, state: dict[str, str]
) -> None:
    sign_in = SignIn()
    with job_platform(tmp_path, sign_in=sign_in) as service:
        record = service.call("create_task", CPU)
        # The sign-in ends, or the provider goes down, before the researcher approves.
        sign_in.state = "ended" if state["code"] == "HW-INGRESS-004" else "provider-unavailable"
        approval = SessionCommand(
            command_id="approve-synthetic-task",
            session_id=service.session.store.session_id,
            kind=CommandKind.APPROVE,
            actor_id="researcher",
            created_at="2026-07-01T00:00:00Z",
            payload={"target_type": "action-set", "target_id": record.proposal.group_id},
        )
        service.session.handle(approval)
        projection = project_session(
            service.session.replay_events(), session_id=service.session.store.session_id
        )
        refused = service.record(record).phase

        # The review stays open, and an approval after the next sign-in sends the task.
        sign_in.state = "live"
        service.session.handle(approval.model_copy(update={"command_id": "approve-again"}))
        approved = service.record(record).phase

    _assert_every_interface_shows(projection, state["diagnostic"])
    assert projection.last_command_outcome is not None
    assert projection.last_command_outcome.error_code == state["code"]
    assert refused == "proposed"
    assert approved == "observed"
    assert service.jobs.task_count == 1


@pytest.mark.parametrize(
    "state",
    [
        pytest.param(item, id=item["code"])
        for item in _STATES
        if item["surface"] == "gateway-response" and item["code"] in _IDENTITY_CODES
    ],
)
def test_a_platform_call_without_review_shows_its_identity_refusal_in_every_interface(
    tmp_path: Path, state: dict[str, str]
) -> None:
    sign_in = SignIn()
    with job_platform(tmp_path, sign_in=sign_in) as service:
        sign_in.state = "ended" if state["code"] == "HW-INGRESS-004" else "provider-unavailable"
        # The platform cannot describe the call, so no review opens; reads repeat nothing.
        assert service.attempt("create_task", CPU) is None
        service.advance()
        projection = project_session(
            service.session.replay_events(), session_id=service.session.store.session_id
        )

    _assert_every_interface_shows(projection, state["diagnostic"])
    # One message for the call, however often the session is read.
    assert [
        message.detail
        for message in projection.conversation
        if state["code"] in (message.detail or "")
    ] == [state["diagnostic"]]


@pytest.mark.parametrize(
    "state",
    [pytest.param(item, id=item["code"]) for item in _STATES if item["surface"] == "setup-refusal"],
)
def test_capability_not_offered_shows_the_same_diagnostic_in_every_interface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    state: dict[str, str],
) -> None:
    project = ProjectContext(tmp_path)
    workstation = SessionGateway(project=project, env={}, backend_id="deterministic")
    try:
        workstation.configure_model_source("openai")
    finally:
        workstation.stop()
    # Carina offers neither the OpenAI API nor the stored choice made on a workstation.
    carina = {"HEARTWOOD_PLATFORM": "carina"}
    gateway = SessionGateway(project=project, env=carina, backend_id="deterministic")
    try:
        readiness = gateway.project_readiness()
        browser = RestGateway(gateway).handle(
            RestRequest(
                method="PUT",
                path="/settings/models/source",
                body=json.dumps({"source_id": "openai"}),
            )
        )
        with pytest.raises(ValueError, match=re.escape(state["diagnostic"])) as notebook:
            NotebookSession(gateway=gateway).connect_model("openai", "gpt-synthetic")
    finally:
        gateway.stop()
    monkeypatch.chdir(tmp_path)
    for name, value in carina.items():
        monkeypatch.setenv(name, value)
    terminal_main(["doctor"])
    doctor = capsys.readouterr().out
    with pytest.raises(SystemExit):
        terminal_main(["models", "connect", "openai", "gpt-synthetic"])
    terminal = capsys.readouterr().err

    assert [
        f"{check['code']}: {check['summary']}"
        for check in readiness["checks"]
        if check.get("code") == state["code"]
    ] == [state["diagnostic"]]
    assert browser.status_code == 422
    assert f"{browser.body['code']}: {browser.body['error']}" == state["diagnostic"]
    assert str(notebook.value) == state["diagnostic"]
    assert terminal.rstrip().endswith(state["diagnostic"])
    assert state["diagnostic"].split(": ", 1)[1] in doctor
    assert state["code"] in doctor


def test_inference_keeps_working_while_platform_tools_are_down(
    tmp_path: Path, fakes: RunningFakes
) -> None:
    # The session configures no MCP server; this shows the agent runtime completes a turn
    # through the inference fake while the kit observes both services as unavailable.
    fakes.data.available = False
    fakes.jobs.available = False
    fakes.budgets.available = False
    service = _service(tmp_path, fakes)
    try:
        events = _chat(service)
    finally:
        service.close()
    tools = run_platform_conformance(None, fakes.endpoints()[1])

    assert [result.status for result in tools] == ["unavailable", "unavailable", "unavailable"]
    assert not any(event.kind == EventKind.ERROR_RECORDED for event in events)
    assert [
        event.payload.get("content")
        for event in events
        if event.kind == EventKind.AGENT_MESSAGE_EMITTED
    ] == [fakes.inference.reply]
