# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Two live workers retain separate state, credentials, approvals and revocation."""

from __future__ import annotations

import json
import shlex
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import PurePosixPath
from typing import Any
from uuid import uuid4

import httpx
import pytest
from test_worker_native_integration import _application, _assert_revoked, _stop, _submit
from test_worker_native_integration import infrastructure as infrastructure
from worker_native_fixture import PYTHON, NativeFixture, command

from heartwood.core_adapter.worker_attempts import WorkerRuntimeBinding
from heartwood.gateway import SessionGateway
from heartwood.gateway._openhands_sdk import OpenHandsSdkBackend
from heartwood.gateway.worker_inputs import WorkerInputManifest
from heartwood.gateway.worker_native import _NativeWorkerAttempt
from heartwood.gateway.worker_sessions import WorkerSessionPlan
from heartwood.session import CommandKind, EventKind


def _pending(application: SessionGateway, terminal: str) -> str:
    service = application._service("composition")
    backend = service.backend
    assert isinstance(backend, OpenHandsSdkBackend)
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        service.reconcile()
        group = backend.pending_action_group(session_id="composition")
        if group is not None and any(
            event.kind == EventKind.CONFIRMATION_REQUESTED
            and isinstance(request := event.payload.get("request"), dict)
            and request.get("group_id") == group.group_id
            for event in service.replay_events()
        ):
            assert len(group.actions) == 1
            assert group.actions[0].tool_name == "terminal"
            assert group.actions[0].arguments.get("command") == terminal
            return group.group_id
        time.sleep(0.1)
    raise AssertionError("Worker did not present its own action for review")


def _finish(application: SessionGateway, group_id: str) -> None:
    service = application._service("composition")
    backend = service.backend
    assert isinstance(backend, OpenHandsSdkBackend)
    _submit(application, CommandKind.APPROVE, {"target_type": "action-set", "target_id": group_id})
    assert backend.wait_for_idle(60)
    service.reconcile()
    conversation = backend._conversation
    assert conversation is not None
    assert conversation.state.execution_status.value == "finished"
    executions = [
        event
        for event in service.replay_events()
        if event.kind == EventKind.TOOL_EXECUTION_RECORDED
    ]
    assert len(executions) == 1
    assert executions[0].payload["exit_code"] == 0


def _probe(
    fixture: NativeFixture, worker: str, name: str, peer: str, marker: str, *, completed: bool
) -> str:
    program = """import hashlib,json,os,secrets,sys
from pathlib import Path
name,peer,marker,completed=sys.argv[1:]
assert os.getuid()==10001
assert json.loads(Path('/inputs/summary.json').read_text())['worker_marker']==marker
assert Path('/skills/aggregate-export/SKILL.md').is_file()
secret=Path('/workspace')/(name+'.private')
if not secret.exists():
 secret.write_text(secrets.token_urlsafe(32))
 secret.chmod(0o600)
assert len(secret.read_text())>=32
assert not (Path('/workspace')/(peer+'.private')).exists()
assert not (Path('/workspace')/(peer+'.json')).exists()
artifact=Path('/workspace')/(name+'.json')
if completed=='yes':
 assert json.loads(artifact.read_text())=={'marker':marker}
else:
 assert not artifact.exists()
print(hashlib.sha256(secret.read_bytes()).hexdigest())
"""
    observed = fixture.control.exec(
        worker,
        [PYTHON, "-I", "-B", "-c", program, name, peer, marker, "yes" if completed else "no"],
        workspace="default",
        timeout_seconds=30,
        no_login_shell=True,
    )
    assert observed.exit_code == 0, observed.stderr
    assert isinstance(observed.stdout, str)
    digest = observed.stdout.strip()
    assert len(digest) == 64
    return digest


def test_two_native_workers_keep_authority_and_effects_separate(
    infrastructure: NativeFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = infrastructure
    names = ("left", "right")
    markers = {name: f"worker-{name}-{uuid4().hex}" for name in names}
    terminals = {
        name: shlex.join(
            [
                PYTHON,
                "-I",
                "-B",
                "-c",
                "import json;from pathlib import Path;"
                "value=json.loads(Path('/inputs/summary.json').read_text())['worker_marker'];"
                f"assert value=={markers[name]!r};"
                f"Path('/workspace/{name}.json').write_text(json.dumps({{'marker':value}}))",
            ]
        )
        for name in names
    }
    fixture.provider.scripts = {
        markers[name]: [
            ("terminal", {"command": terminals[name]}),
            ("finish", {"message": "Synthetic worker effect completed."}),
        ]
        for name in names
    }
    native: dict[str, _NativeWorkerAttempt] = {}
    plan = _NativeWorkerAttempt.plan

    def capture(attempt: _NativeWorkerAttempt) -> WorkerSessionPlan:
        native[attempt.request.project.root.name] = attempt
        with fixture.provider._request_lock:
            fixture.provider.forbidden_values += (attempt._key,)
            fixture.collector.forbidden_values += (attempt._key,)
        return plan(attempt)

    monkeypatch.setattr(_NativeWorkerAttempt, "plan", capture)
    deployment = fixture.deployment(WorkerInputManifest())
    applications = {
        name: _application(
            fixture, project_name=name, input_marker=markers[name], deployment=deployment
        )
        for name in names
    }
    services = {
        name: application._service("composition") for name, application in applications.items()
    }
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            starts = [
                executor.submit(
                    _submit,
                    applications[name],
                    CommandKind.CHAT,
                    {"prompt": f"Perform the synthetic isolation check {markers[name]}."},
                )
                for name in names
            ]
            for started in starts:
                started.result(timeout=120)
        groups = {name: _pending(applications[name], terminals[name]) for name in names}
        assert groups["left"] != groups["right"]
        attempts = {name: services[name].worker_attempt() for name in names}
        workers: dict[str, str] = {}
        endpoints: dict[str, str] = {}
        conversations: dict[str, str] = {}
        volumes: dict[str, dict[str, str]] = {}
        intents: dict[str, str] = {}
        bindings: dict[str, WorkerRuntimeBinding] = {}
        requests: dict[str, str] = {}
        containers: dict[str, str] = {}
        for name in names:
            attempt = attempts[name]
            assert attempt is not None
            assert attempt.phase == "active"
            assert attempt.runtime is not None
            bindings[name] = attempt.runtime
            assert services[name].store.owns_writer
            intents[name] = str(attempt.intent.attempt_id)
            workers[name] = "hw-" + attempt.intent.attempt_id.hex[:16]
            endpoints[name] = str(attempt.runtime.endpoint)
            backend = services[name].backend
            assert isinstance(backend, OpenHandsSdkBackend)
            assert backend._conversation is not None
            conversations[name] = str(backend._conversation.id)
            observed = json.loads(
                command(
                    [
                        "docker",
                        "inspect",
                        f"openshell-default--{workers[name]}-{attempt.runtime.sandbox_id}",
                    ]
                )
            )[0]
            assert observed["HostConfig"]["NetworkMode"] == "none"
            assert observed["HostConfig"]["PidMode"] in {"", "private"}
            containers[name] = observed["Id"]
            workspace = PurePosixPath("/workspace")
            for mount in observed["Mounts"]:
                mount_path = PurePosixPath(mount["Destination"])
                assert mount_path != workspace
                assert not workspace.is_relative_to(mount_path)
                assert not mount_path.is_relative_to(workspace)
            volumes[name] = {}
            for destination in ("/inputs", "/skills"):
                mounts = [m for m in observed["Mounts"] if m["Destination"] == destination]
                assert len(mounts) == 1
                assert mounts[0]["Type"] == "volume"
                assert mounts[0]["RW"] is False
                volumes[name][destination] = mounts[0]["Name"]
            proposals = [
                e for e in services[name].replay_events() if e.kind == EventKind.TOOL_CALL_PROPOSED
            ]
            assert len(proposals) == 1
            assert proposals[0].payload["worker_attempt_id"] == intents[name]
            confirmations = [
                request
                for event in services[name].replay_events()
                if event.kind == EventKind.CONFIRMATION_REQUESTED
                and isinstance(request := event.payload.get("request"), dict)
                and request.get("group_id") == groups[name]
            ]
            assert len(confirmations) == 1
            assert confirmations[0]["tool_call_id"] == proposals[0].payload["tool_call_id"]
            request_id = confirmations[0]["request_id"]
            assert isinstance(request_id, str)
            requests[name] = request_id
        assert intents["left"] != intents["right"]
        assert containers["left"] != containers["right"]
        assert requests["left"] != requests["right"]
        assert conversations["left"] != conversations["right"]
        assert native["left"]._key != native["right"]._key
        assert set(volumes["left"].values()).isdisjoint(volumes["right"].values())
        digests = {
            name: _probe(fixture, workers[name], name, peer, markers[name], completed=False)
            for name, peer in (("left", "right"), ("right", "left"))
        }
        assert digests["left"] != digests["right"]
        with httpx.Client(timeout=10, trust_env=False, follow_redirects=False) as client:
            for name, peer in (("left", "right"), ("right", "left")):
                own = client.get(
                    endpoints[name] + "api/conversations/" + conversations[name],
                    headers={"X-Session-API-Key": native[name]._key},
                )
                assert own.status_code == 200
                missing = client.get(
                    endpoints[name] + "api/conversations/" + conversations[peer],
                    headers={"X-Session-API-Key": native[name]._key},
                )
                assert missing.status_code == 404
                rejected = client.get(
                    endpoints[name] + "api/conversations/" + conversations[name],
                    headers={"X-Session-API-Key": native[peer]._key},
                )
                assert rejected.status_code == 401
        _submit(
            applications["right"],
            CommandKind.APPROVE,
            {
                "target_type": "action-set",
                "target_id": groups["left"],
            },
        )
        assert any(
            e.kind == EventKind.ERROR_RECORDED
            and e.payload.get("reason") == f"no matching pending action group: {groups['left']}"
            for e in services["right"].replay_events()
        )
        assert _pending(applications["right"], terminals["right"]) == groups["right"]
        assert (
            _probe(fixture, workers["right"], "right", "left", markers["right"], completed=False)
            == digests["right"]
        )
        _finish(applications["left"], groups["left"])
        assert (
            _probe(fixture, workers["left"], "left", "right", markers["left"], completed=True)
            == digests["left"]
        )
        assert (
            _probe(fixture, workers["right"], "right", "left", markers["right"], completed=False)
            == digests["right"]
        )
        _stop(applications["left"])
        _assert_revoked(services["left"])
        survivor = services["right"].worker_attempt()
        assert survivor is not None
        assert survivor.phase == "active"
        assert survivor.runtime == bindings["right"]
        assert services["right"].store.owns_writer
        assert (
            _probe(fixture, workers["right"], "right", "left", markers["right"], completed=False)
            == digests["right"]
        )
        _finish(applications["right"], groups["right"])
        assert (
            _probe(fixture, workers["right"], "right", "left", markers["right"], completed=True)
            == digests["right"]
        )
        _stop(applications["right"])
        _assert_revoked(services["right"])
        for name in names:
            observations = [o for o in fixture.provider.observations if o["route"] == markers[name]]
            assert len(observations) == 2
            assert all(o["authorized"] for o in observations)
            services[name].audit_log.verify()
        evidence: dict[str, Any] = {
            "drill_id": "native-worker-pair.v1",
            "gateway_id": fixture.identity,
            "attempts": intents,
            "confirmation_requests": requests,
            "worker_containers": containers,
            "workspace_storage": "separate container writable layers",
            "distinct_volume_sets": volumes,
            "own_inputs_and_artifacts_verified": True,
            "peer_secrets_and_artifacts_absent": True,
            "cross_worker_api_keys_rejected": True,
            "peer_conversations_unavailable": True,
            "cross_attempt_approval_rejected": True,
            "independent_revocation_verified": True,
            "both_workers_revoked_before_writer_release": True,
            "sensitive_data_ready": False,
        }
        (fixture.output / "evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
    finally:
        for application in applications.values():
            _stop(application)
