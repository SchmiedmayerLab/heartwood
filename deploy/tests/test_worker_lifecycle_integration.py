# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Migration, manifest launch, restart, cancellation, rotation and specialists in real workers."""

from __future__ import annotations

import json
import os
import secrets
import signal
import socket
import subprocess
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import tomli_w
from test_worker_native_integration import _application, _assert_revoked, _stop, _submit
from test_worker_native_integration import infrastructure as infrastructure
from worker_native_fixture import NativeFixture, command

from heartwood.core_adapter import FileSessionStore
from heartwood.core_adapter.worker_attempts import current_worker_attempt
from heartwood.gateway import ProjectContext, SessionGateway
from heartwood.gateway._model_settings import ModelProfile, ModelSettings
from heartwood.gateway._openhands_sdk import OpenHandsSdkBackend
from heartwood.gateway._project_config import ProjectConfig, ProjectConfigStore
from heartwood.gateway._project_migration import migrate_project_state
from heartwood.gateway._workspace import WorkspaceInspector
from heartwood.gateway.worker_inputs import WorkerInputManifest
from heartwood.schemas import PolicyProfile
from heartwood.session import CommandKind, EventKind

_FINISH = ("finish", {"message": "Synthetic lifecycle step completed."})


def _pending(application: SessionGateway, *, tool: str = "terminal") -> str:
    """Wait until the worker's next action set is journaled for review."""
    service = application._service("composition")
    backend = service.backend
    assert isinstance(backend, OpenHandsSdkBackend)
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        service.reconcile()
        group = backend.pending_action_group(session_id="composition")
        if group is not None and any(
            event.kind == EventKind.CONFIRMATION_REQUESTED
            and isinstance(request := event.payload.get("request"), dict)
            and request.get("group_id") == group.group_id
            for event in service.replay_events()
        ):
            assert [action.tool_name for action in group.actions] == [tool]
            return group.group_id
        time.sleep(0.1)
    raise AssertionError("Worker did not present its action for review")


def _settle(application: SessionGateway, *, status: str = "finished") -> None:
    service = application._service("composition")
    backend = service.backend
    assert isinstance(backend, OpenHandsSdkBackend)
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        service.reconcile()
        conversation = backend._conversation
        if (
            backend.wait_for_idle(0.1)
            and conversation is not None
            and conversation.state.execution_status.value in {"finished", "error", "stuck", status}
        ):
            assert conversation.state.execution_status.value == status
            return
        time.sleep(0.1)
    raise AssertionError("Native conversation did not settle before its deadline")


def _approve(application: SessionGateway, group_id: str) -> None:
    _submit(application, CommandKind.APPROVE, {"target_type": "action-set", "target_id": group_id})


def _executions(application: SessionGateway) -> list[dict[str, Any]]:
    return [
        dict(event.payload)
        for event in application._service("composition").replay_events()
        if event.kind == EventKind.TOOL_EXECUTION_RECORDED
    ]


def _worker(application: SessionGateway) -> tuple[str, str]:
    attempt = application._service("composition").worker_attempt()
    assert attempt is not None
    assert attempt.phase == "active"
    return str(attempt.intent.attempt_id), "hw-" + attempt.intent.attempt_id.hex[:16]


def _running_image(_fixture: NativeFixture, worker: str, sandbox_id: str) -> str:
    """Return the image identifier of the worker's container."""
    observed = json.loads(
        command(["docker", "inspect", f"openshell-default--{worker}-{sandbox_id}"])
    )[0]
    return str(observed["Image"])


def _read(fixture: NativeFixture, worker: str, path: str) -> str:
    observed = fixture.control.exec(
        worker,
        ["/bin/sh", "-c", f"cat {path} 2>/dev/null || true"],
        workspace="default",
        timeout_seconds=15,
        no_login_shell=True,
    )
    assert observed.exit_code == 0, observed.stderr
    assert isinstance(observed.stdout, str)
    return observed.stdout


def _absent(attempt_id: str) -> None:
    for resource in ("container", "volume"):
        arguments = ["docker", resource, "ls", "--quiet"]
        if resource == "container":
            arguments.append("--all")
        assert not command(
            [*arguments, "--filter", "label=heartwood.attempt=" + attempt_id]
        ).strip()


def _write_evidence(fixture: NativeFixture, report: dict[str, object]) -> None:
    (fixture.output / "evidence.json").write_text(
        json.dumps(
            {
                **report,
                "worker_image": fixture.worker,
                "gateway_image": fixture.gateway_image,
                "sensitive_data_ready": False,
            },
            indent=2,
        )
        + "\n"
    )


@pytest.mark.parametrize("pending", [False, True], ids=["settled", "pending"])
def test_native_gateway_restart_reconciles_the_session(
    infrastructure: NativeFixture, pending: bool
) -> None:
    """A restarted gateway continues a settled session in a replacement worker only."""
    fixture = infrastructure
    first = "printf first >> /workspace/effect.txt"
    second = "printf second >> /workspace/effect.txt"
    fixture.provider.scripts = {
        "restart-first": [("terminal", {"command": first}), _FINISH],
        "restart-second": [("terminal", {"command": second}), _FINISH],
    }
    settings = fixture.deployment(WorkerInputManifest(files=()))
    application = _application(fixture, deployment=settings)
    project = application.project
    service = application._service("composition")
    _submit(application, CommandKind.CHAT, {"prompt": "restart-first: append one marker."})
    group = _pending(application)
    earlier, worker = _worker(application)
    if not pending:
        _approve(application, group)
        _settle(application)
        assert _read(fixture, worker, "/workspace/effect.txt") == "first"
    _stop(application)
    _assert_revoked(service)
    requests = len(fixture.provider.observations)

    restarted = _application(fixture, deployment=settings, project=project)
    try:
        if pending:
            # The earlier worker left a review unsettled; the decision cannot reach it.
            before = len(restarted._service("composition").replay_events())
            _approve(restarted, group)
            events = restarted._service("composition").replay_events()[before:]
            assert any(event.kind == EventKind.ERROR_RECORDED for event in events)
            assert not any(event.kind == EventKind.APPROVAL_RECORDED for event in events)
            retained = restarted._service("composition").worker_attempt()
            assert retained is not None
            assert retained.phase == "revoked"
            assert not _executions(restarted)
            assert len(fixture.provider.observations) == requests
            outcome = "refused-unsettled-review"
        else:
            _submit(restarted, CommandKind.CHAT, {"prompt": "restart-second: append another."})
            replacement, worker = _worker(restarted)
            assert replacement != earlier
            _approve(restarted, _pending(restarted))
            _settle(restarted)
            # The replacement starts with an empty workspace: the earlier effect is not repeated.
            assert _read(fixture, worker, "/workspace/effect.txt") == "second"
            assert [item["exit_code"] for item in _executions(restarted)] == [0, 0]
            outcome = "continued-in-replacement"
        _stop(restarted)
        _absent(earlier)
        _write_evidence(
            fixture,
            {
                "drill_id": "native-gateway-restart.v1",
                "restart_while_pending": pending,
                "outcome": outcome,
                "earlier_attempt_revoked": True,
                "duplicate_effects": 0,
                "provider_requests": len(fixture.provider.observations),
            },
        )
    finally:
        _stop(restarted)


def test_native_cancellation_settles_without_repeating_the_effect(
    infrastructure: NativeFixture,
) -> None:
    fixture = infrastructure
    slow = "sleep 15; printf once >> /workspace/cancelled.txt"
    fixture.provider.scripts = {"cancel-check": [("terminal", {"command": slow}), _FINISH, _FINISH]}
    application = _application(fixture)
    service = application._service("composition")
    try:
        _submit(application, CommandKind.CHAT, {"prompt": "cancel-check: run the slow step."})
        _approve(application, _pending(application))
        attempt, worker = _worker(application)
        time.sleep(2)
        _submit(application, CommandKind.PAUSE, {})
        # Without the pause the script would continue past the step and finish the turn.
        _settle(application, status="paused")
        paused = len(fixture.provider.observations)
        effect = _read(fixture, worker, "/workspace/cancelled.txt")
        events = service.replay_events()
        unknown = [
            event
            for event in events
            if event.kind == EventKind.ERROR_RECORDED
            and event.payload.get("code") == "HW-AGENT-006"
        ]
        executions = _executions(application)
        succeeded = [item for item in executions if item.get("exit_code") == 0]
        interrupted = [item for item in executions if item.get("exit_code") != 0]
        # The record agrees with the observed effect: a completed step is recorded once
        # with success, and an interrupted step has exactly one cancelled or unknown record.
        if effect == "once":
            assert (len(succeeded), len(interrupted), len(unknown)) == (1, 0, 0)
        else:
            assert effect == ""
            assert not succeeded
            assert len(interrupted) + len(unknown) == 1
        _submit(application, CommandKind.RESUME, {})
        _settle(application)
        # Outlast the cancelled step: neither it nor the resumed turn may repeat the effect.
        time.sleep(16)
        final = _read(fixture, worker, "/workspace/cancelled.txt")
        assert final in {"", "once"}
        resumed = _executions(application)
        assert len(resumed) <= 1
        assert not [
            observation
            for observation in fixture.provider.observations[paused:]
            if observation["route"] != "cancel-check"
        ]
        _stop(application)
        _assert_revoked(service)
        _absent(attempt)
        _write_evidence(
            fixture,
            {
                "drill_id": "native-cancellation.v1",
                "effect_after_cancellation": effect,
                "effect_after_resume": final,
                "recorded_executions": len(resumed),
                "recorded_exit_codes": [item.get("exit_code") for item in resumed],
                "unknown_outcomes": len(unknown),
                "duplicate_effects": 0,
            },
        )
    finally:
        _stop(application)


def test_native_new_worker_image_and_rotated_credential_after_restart(
    infrastructure: NativeFixture,
) -> None:
    """A new image applies at restart; a rotation fails the attached worker closed."""
    fixture = infrastructure
    fixture.provider.scripts = {
        "image-first": [("terminal", {"command": "printf first > /workspace/step.txt"}), _FINISH],
        "image-second": [
            ("terminal", {"command": "printf second > /workspace/step.txt"}),
            _FINISH,
            ("terminal", {"command": "printf unexpected > /workspace/step.txt"}),
            _FINISH,
        ],
        "rotation-after": [
            ("terminal", {"command": "printf rotated > /workspace/step.txt"}),
            _FINISH,
        ],
    }
    settings = fixture.deployment(WorkerInputManifest(files=()))
    application = _application(fixture, deployment=settings)
    project = application.project
    service = application._service("composition")
    _submit(application, CommandKind.CHAT, {"prompt": "image-first: record a value."})
    _approve(application, _pending(application))
    _settle(application)
    earlier, _name = _worker(application)
    _stop(application)
    _assert_revoked(service)

    # The operator qualifies a new worker image and restarts the gateway with it.
    replacement_image = os.environ["HW_REPLACEMENT_WORKER_IMAGE"]
    assert replacement_image != settings.image_digest
    replacement_settings = settings.model_copy(update={"image_digest": replacement_image})
    restarted = _application(fixture, deployment=replacement_settings, project=project)
    service = restarted._service("composition")
    _submit(restarted, CommandKind.CHAT, {"prompt": "image-second: record another value."})
    attempt = service.worker_attempt()
    assert attempt is not None
    assert attempt.intent.image_digest == replacement_image
    assert attempt.runtime is not None
    replacement, worker = _worker(restarted)
    assert replacement != earlier
    assert _running_image(fixture, worker, attempt.runtime.sandbox_id) == replacement_image
    _approve(restarted, _pending(restarted))
    _settle(restarted)
    assert _read(fixture, worker, "/workspace/step.txt") == "second"

    # Rotating the outside provider credential fails the attached worker closed.
    rotated = secrets.token_urlsafe(32)
    fixture.provider.forbidden_values = (*fixture.provider.forbidden_values, rotated)
    fixture.cli_command(
        ["provider", "update", fixture.identity, "--credential", "HEARTWOOD_QUALIFICATION_KEY"],
        HEARTWOOD_QUALIFICATION_KEY=rotated,
    )
    fixture.provider.credential = rotated
    executions = len(_executions(restarted))
    recorded = len(service.replay_events())
    rotated_at = len(fixture.provider.observations)
    _submit(restarted, CommandKind.CHAT, {"prompt": "Continue with the next value."})
    codes: set[object] = set()
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline and not codes & {"HW-AGENT-004", "HW-AGENT-012"}:
        service.reconcile()
        codes = {
            event.payload.get("code")
            for event in service.replay_events()[recorded:]
            if event.kind == EventKind.ERROR_RECORDED
        }
        time.sleep(0.5)
    assert codes & {"HW-AGENT-004", "HW-AGENT-012"}, "the attached worker did not fail closed"
    assert len(_executions(restarted)) == executions
    assert not any(
        row["authorized"] and row["route"] == "image-second"
        for row in fixture.provider.observations[rotated_at:]
    )
    _stop(restarted)
    _assert_revoked(service)
    _absent(replacement)

    # The next start attaches the rotated credential to a replacement worker.
    resumed = _application(fixture, deployment=replacement_settings, project=project)
    try:
        before = len(fixture.provider.observations)
        _submit(resumed, CommandKind.CHAT, {"prompt": "rotation-after: record the last value."})
        final, worker = _worker(resumed)
        assert final not in {earlier, replacement}
        _approve(resumed, _pending(resumed))
        _settle(resumed)
        assert _read(fixture, worker, "/workspace/step.txt") == "rotated"
        assert fixture.provider.observations[before:]
        assert all(row["authorized"] for row in fixture.provider.observations[before:])
        _stop(resumed)
        _absent(earlier)
        _absent(final)
        _write_evidence(
            fixture,
            {
                "drill_id": "native-image-change-and-rotation.v1",
                "replacement_image": replacement_image,
                "replacement_attempt_admitted_after_restart": True,
                "rotation_failed_attached_worker_closed": sorted(
                    str(code) for code in codes if code is not None
                ),
                "rotated_credential_in_replacement_worker": True,
                "duplicate_effects": 0,
                "provider_requests": len(fixture.provider.observations),
            },
        )
    finally:
        _stop(resumed)


def test_native_advisory_specialist_runs_inside_the_parent_worker(
    infrastructure: NativeFixture,
) -> None:
    fixture = infrastructure
    delegated = "specialist-delegation: review the synthetic aggregate plan."
    fixture.provider.scripts = {
        "specialist-parent": [
            (
                "task",
                {
                    "description": "Review synthetic plan",
                    "prompt": delegated,
                    "subagent_type": "research-planner",
                },
            ),
            _FINISH,
        ],
        "specialist-delegation": [("finish", {"message": "The synthetic plan is complete."})],
    }
    application = _application(fixture)
    service = application._service("composition")
    try:
        _submit(application, CommandKind.CHAT, {"prompt": "specialist-parent: ask the planner."})
        _approve(application, _pending(application, tool="task"))
        _settle(application)
        attempt, _worker_name = _worker(application)
        routes = [row["route"] for row in fixture.provider.observations]
        assert routes.count("specialist-delegation") == 1
        assert all(row["authorized"] for row in fixture.provider.observations)
        subagents = [
            event.payload["subagent"]
            for event in service.replay_events()
            if event.kind == EventKind.SUBAGENT_UPDATED
        ]
        assert {item["status"] for item in subagents} >= {"running", "completed"}
        assert {item["agent_name"] for item in subagents} == {"research-planner"}
        _stop(application)
        _assert_revoked(service)
        _absent(attempt)
        _write_evidence(
            fixture,
            {
                "drill_id": "native-advisory-specialist.v1",
                "specialist": "research-planner",
                "specialist_requests_through_parent_route": routes.count("specialist-delegation"),
                "separate_specialist_sandbox": False,
            },
        )
    finally:
        _stop(application)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class _ServedGateway:
    """Run `heartwood gateway serve --deployment` as the operator's launcher would."""

    def __init__(self, fixture: NativeFixture, manifest: Path, workspace: Path) -> None:
        self.secret = secrets.token_urlsafe(32)
        self.port = _free_port()
        read, write = os.pipe()
        os.write(write, self.secret.encode())
        os.close(write)
        log = (fixture.output / f"gateway-serve-{uuid4().hex[:8]}.log").open("w")
        environment = {
            **fixture.env,
            "HEARTWOOD_PLATFORM": "generic",
            "HOME": str(fixture.private),
        }
        self.process = subprocess.Popen(
            [
                "heartwood",
                "gateway",
                "serve",
                "--deployment",
                str(manifest),
                "--port",
                str(self.port),
                "--capability-fd",
                str(read),
            ],
            cwd=workspace,
            env=environment,
            pass_fds=(read,),
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        os.close(read)
        log.close()
        self.client = httpx.Client(
            base_url=f"http://127.0.0.1:{self.port}",
            headers={"x-heartwood-capability": self.secret},
            timeout=150,
        )
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            assert self.process.poll() is None, "gateway serve exited during startup"
            try:
                if self.client.get("/project/readiness").status_code == 200:
                    return
            except httpx.TransportError:
                # The gateway is not listening yet; poll again until the deadline.
                pass
            time.sleep(0.5)
        raise AssertionError("gateway serve did not become ready")

    def command(self, kind: CommandKind, payload: dict[str, object]) -> dict[str, Any]:
        response = self.client.post(
            "/sessions/composition/commands",
            json={
                "command_id": str(uuid4()),
                "session_id": "composition",
                "kind": kind.value,
                "created_at": datetime.now(UTC).isoformat(),
                "payload": payload,
            },
        )
        assert response.status_code == 200, response.text
        return dict(response.json())

    def pending(self) -> str:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            projection = self.client.get("/sessions/composition/projection").json()
            group = projection.get("pendingApproval")
            if group is not None and group.get("decision") is None:
                return str(group["groupId"])
            time.sleep(0.5)
        raise AssertionError("the served session presented no action for review")

    def finished(self) -> None:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            projection = self.client.get("/sessions/composition/projection").json()
            status = projection["lifecycle"]["status"]
            if status in {"finished", "error"}:
                assert status == "finished"
                return
            time.sleep(0.5)
        raise AssertionError("the served session did not finish")

    def stop(self, *, kill: bool = False) -> None:
        self.client.close()
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGKILL if kill else signal.SIGINT)
            self.process.wait(timeout=240)


def test_native_migrated_project_serves_through_the_operator_manifest(
    infrastructure: NativeFixture,
) -> None:
    """Migrate local state, launch through the manifest, crash, restart and continue."""
    fixture = infrastructure
    fixture.provider.scripts = {
        "served-first": [
            ("terminal", {"command": "printf served > /workspace/served.txt"}),
            _FINISH,
        ],
        "served-second": [
            ("terminal", {"command": "printf again > /workspace/served.txt"}),
            _FINISH,
        ],
    }
    workspace = fixture.private / "migrated-project"
    workspace.mkdir()
    (workspace / "summary.json").write_text(json.dumps({"summary": {"participant_count": 10}}))
    local = ProjectContext(workspace)
    local.initialize()
    settings = fixture.deployment(
        WorkerInputManifest(
            files=(WorkspaceInspector(local).fingerprint("summary.json", max_bytes=1024),)
        )
    )
    profile = ModelProfile(
        profile_id=settings.profile_id,
        model=settings.model,
        base_url=fixture.base_url,
        policy_endpoint=settings.model_endpoint,
        credential_kind="environment",
        api_key_env="SYNTHETIC_MODEL_KEY",
    )
    policy = PolicyProfile(
        policy_id="synthetic-site",
        platform_id="generic",
        allowed_model_endpoints=(settings.model_endpoint,),
        credential_allowlist=("SYNTHETIC_MODEL_KEY",),
        allowed_action_review_modes=("review-all",),
        allowed_credential_backends=("mounted-file",),
    )
    store = ProjectConfigStore(local, ProjectConfig(platform_id="generic", policy=policy))
    store.save(
        replace(
            store.load(),
            model_settings=ModelSettings(active_profile=profile.profile_id, profiles=(profile,)),
        )
    )
    identity = str(uuid4())
    (fixture.private / "served-state").mkdir(mode=0o700)
    state_root = fixture.private / "served-state" / identity
    manifest = fixture.private / "served-deployment.toml"
    manifest.write_text(
        tomli_w.dumps(
            {
                "schema_version": "heartwood.project-deployment.v1",
                "project_id": identity,
                "workspace": str(workspace),
                "state_root": str(state_root),
                "policy": policy.model_dump(mode="json", exclude_none=True),
                "requirements": {"isolated_worker": True},
                "worker": settings.model_dump(mode="json", exclude_none=True),
            }
        )
    )
    manifest.chmod(0o600)
    migrate_project_state(
        deployment=manifest, workspace=workspace, backup=fixture.private / "served-backup"
    )
    assert (workspace / ".heartwood-migration.json").is_file()
    assert not (workspace / ".heartwood").exists()

    served = _ServedGateway(fixture, manifest, workspace)
    try:
        readiness = served.client.get("/project/readiness").json()
        assert readiness["policy"]["source"] == "deployment"
        assert readiness["policy"]["manifest"] == str(manifest)
        assert readiness["policy"]["isolated_worker"] == "required"
        assert readiness["state_root"] == str(state_root)
        served.command(CommandKind.CHAT, {"prompt": "served-first: write the marker."})
        served.command(
            CommandKind.APPROVE, {"target_type": "action-set", "target_id": served.pending()}
        )
        served.finished()
    finally:
        # An abrupt end leaves the worker running for the next start to reconcile.
        served.stop(kill=True)
    first = ProjectContext(workspace, protected_state=state_root, project_id=UUID(identity))
    record = current_worker_attempt(
        FileSessionStore(first.sessions_dir, "composition").replay_events()
    )
    assert record is not None
    assert record.phase == "active"
    earlier = str(record.intent.attempt_id)

    restarted = _ServedGateway(fixture, manifest, workspace)
    try:
        # Startup reconciliation revoked the attempt the killed process left running.
        _absent(earlier)
        served_events = FileSessionStore(first.sessions_dir, "composition")
        record = current_worker_attempt(served_events.replay_events())
        assert record is not None
        assert record.phase == "revoked"
        restarted.command(CommandKind.CHAT, {"prompt": "served-second: write again."})
        restarted.command(
            CommandKind.APPROVE, {"target_type": "action-set", "target_id": restarted.pending()}
        )
        restarted.finished()
    finally:
        restarted.stop()
    events = FileSessionStore(first.sessions_dir, "composition").replay_events()
    record = current_worker_attempt(events)
    assert record is not None
    assert record.phase == "revoked"
    _absent(str(record.intent.attempt_id))
    executions = [event for event in events if event.kind == EventKind.TOOL_EXECUTION_RECORDED]
    assert [event.payload["exit_code"] for event in executions] == [0, 0]
    _write_evidence(
        fixture,
        {
            "drill_id": "native-manifest-launch.v1",
            "migrated_before_launch": True,
            "launched_with": "heartwood gateway serve --deployment",
            "effective_policy_source": "deployment",
            "killed_gateway_attempt_revoked_at_restart": True,
            "continued_in_replacement": True,
            "duplicate_effects": 0,
        },
    )
