# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from threading import Event, Thread, Timer
from time import monotonic, sleep
from typing import Literal, cast
from unittest.mock import Mock
from uuid import uuid4

import pytest
from pydantic import AnyHttpUrl, SecretStr

from heartwood.core_adapter import FileSessionStore, SessionResult, SessionService
from heartwood.core_adapter.external_actions import ExternalActionAdapter
from heartwood.core_adapter.worker_attempts import (
    WorkerAttemptError,
    WorkerAttemptIntent,
    WorkerAttemptLease,
    WorkerAttemptRecord,
    WorkerRevocationEvidence,
    WorkerRuntimeBinding,
    current_worker_attempt,
)
from heartwood.gateway import ProjectContext, SessionGateway
from heartwood.gateway._model_settings import ModelProfile, ModelSettings
from heartwood.gateway._openhands_sdk import OpenHandsSdkBackend, WorkerConversationContext
from heartwood.gateway._session_projection import project_session
from heartwood.gateway._worker_events import AdmittedWorkerWorkspace
from heartwood.gateway.worker_sessions import WorkerSessionPlan, WorkerSessionRequest
from heartwood.schemas import PolicyProfile
from heartwood.session import CommandKind, EventKind, SessionCommand
from heartwood.skills import SkillManifest


class Platform:
    def __init__(self) -> None:
        self.gateway: SessionGateway | None = None
        self.fail: Literal["prepare", "launch", "connect", "catalog", "lease", "revoke"] | None = (
            None
        )
        self.calls: list[str] = []
        self.lease: WorkerAttemptLease | None = None

    def prepare(self, request: WorkerSessionRequest) -> WorkerSessionPlan:
        assert self.gateway is not None
        service = self.gateway._services[request.session_id]
        assert request.policy == service.policy_profile
        self.calls.append("prepare")
        if self.fail == "prepare":
            raise RuntimeError("synthetic preparation failure")

        def launch(intent: WorkerAttemptIntent) -> WorkerRuntimeBinding:
            self.calls.append("launch")
            assert self.gateway is not None
            service = self.gateway._services[request.session_id]
            retained = service.worker_attempt()
            assert retained is not None
            assert retained.phase == "prepared"
            assert retained.intent == intent
            service.audit_log.verify()
            if self.fail == "launch":
                raise TimeoutError("synthetic uncertain creation")
            return WorkerRuntimeBinding(
                sandbox_id="synthetic-worker",
                generation="synthetic-generation",
                endpoint=AnyHttpUrl("http://worker.test"),
                image_digest=intent.image_digest,
                sandbox_policy_sha256=intent.sandbox_policy_sha256,
            )

        def connect(lease: WorkerAttemptLease) -> WorkerConversationContext:
            self.calls.append("connect")
            self.lease = lease
            if self.fail == "connect":
                raise RuntimeError("synthetic connection failure")
            workspace = AdmittedWorkerWorkspace.model_construct(
                host="http://worker.test", working_dir="/workspace", attempt=lease, tool_modules=()
            )
            if self.fail == "lease":
                workspace = workspace.model_copy(
                    update={
                        "attempt": WorkerAttemptLease(
                            WorkerAttemptRecord(
                                phase="active", intent=lease.intent, runtime=lease.runtime
                            ),
                            owns_writer=lambda: True,
                        )
                    }
                )
            return WorkerConversationContext(
                workspace,
                SecretStr("synthetic-placeholder"),
                specialist_catalog_sha256=(
                    "0" * 64 if self.fail == "catalog" else request.specialists.fingerprint
                ),
            )

        def revoke(record: WorkerAttemptRecord) -> WorkerRevocationEvidence:
            self.calls.append("revoke")
            assert record.phase == "fenced"
            assert self.lease is None or not self.lease.active
            if self.fail == "revoke":
                raise TimeoutError("synthetic uncertain revocation")
            return WorkerRevocationEvidence(
                attempt_id=record.intent.attempt_id,
                sandbox_id=record.runtime.sandbox_id if record.runtime else None,
                observer_id="synthetic-outside-observer",
                evidence_sha256="e" * 64,
                outcome="revoked" if record.runtime else "not-created",
            )

        return WorkerSessionPlan("sha256:" + "a" * 64, "b" * 64, "c" * 64, launch, connect, revoke)


def configured_gateway(
    tmp_path: Path,
    platform: Platform,
    *,
    external_actions_factory: Callable[[str], ExternalActionAdapter] | None = None,
) -> SessionGateway:
    project_root = tmp_path / "project"
    project_root.mkdir()
    identity = uuid4()
    project = ProjectContext(
        project_root, protected_state=tmp_path / "protected" / str(identity), project_id=identity
    )
    profile = ModelProfile(
        profile_id="synthetic",
        model="openai/synthetic",
        base_url="http://127.0.0.1:9999/v1",
        policy_endpoint="http://127.0.0.1:9999/v1/chat/completions",
        credential_kind="none",
    )
    gateway = SessionGateway(
        project=project,
        worker_plan_factory=platform.prepare,
        env={},
        external_actions_factory=external_actions_factory,
    )
    platform.gateway = gateway
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


def test_gateway_launches_once_after_durable_intent_and_revokes_before_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    platform = Platform()
    gateway = configured_gateway(tmp_path, platform)

    def reject_credential_lookup(*_args: object, **_kwargs: object) -> dict[str, str]:
        pytest.fail("Worker backend resolved gateway credentials")

    monkeypatch.setattr(gateway, "_credential_environment", reject_credential_lookup)
    service = gateway._service("session-worker")
    backend = service.backend
    assert isinstance(backend, OpenHandsSdkBackend)
    assert backend.env == {}
    assert backend._worker_context is not None
    assert platform.calls == []
    context = backend._worker_context()
    assert backend._worker_context() is context
    assert platform.calls == ["prepare", "launch", "connect"]
    assert bool(service.store.owns_writer)
    gateway.stop()
    assert platform.calls == ["prepare", "launch", "connect", "revoke"]
    assert not context.workspace.attempt.active
    assert not bool(service.store.owns_writer)
    assert not (gateway.project.root / ".heartwood").exists()


@pytest.mark.parametrize("failure", ["prepare", "launch", "connect", "catalog", "lease"])
def test_failed_worker_initialization_is_not_retried_or_replaced_with_local(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Literal["prepare", "launch", "connect", "catalog", "lease"],
) -> None:
    platform = Platform()
    platform.fail = failure
    gateway = configured_gateway(tmp_path, platform)

    def reject_local(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Local fallback was used")

    monkeypatch.setattr(OpenHandsSdkBackend, "_default_conversation_factory", reject_local)
    service = gateway._service("session-worker")
    backend = service.backend
    assert isinstance(backend, OpenHandsSdkBackend)
    try:
        with pytest.raises((RuntimeError, WorkerAttemptError)):
            backend._get_conversation()
        with pytest.raises(WorkerAttemptError, match="reconciliation"):
            backend._get_conversation()
        assert platform.calls.count("prepare") == 1
        assert platform.calls.count("launch") <= 1
        if failure == "prepare":
            assert service.worker_attempt() is None
        else:
            assert _settles(service, "revoked")
            assert platform.calls.count("revoke") == 1
    finally:
        platform.fail = None
        gateway.stop()
    assert platform.calls.count("revoke") == (0 if failure == "prepare" else 1)


def _settles(service: SessionService, phase: str) -> bool:
    deadline = monotonic() + 10
    while monotonic() < deadline:
        record = service.worker_attempt()
        if record is not None and record.phase == phase:
            return True
        sleep(0.01)
    return False


def test_fenced_worker_is_revoked_without_waiting_for_session_close(tmp_path: Path) -> None:
    platform = Platform()
    gateway = configured_gateway(tmp_path, platform)
    service = gateway._service("session-worker")
    backend = service.backend
    assert isinstance(backend, OpenHandsSdkBackend)
    assert backend._worker_context is not None
    lease = backend._worker_context().workspace.attempt
    started, release = Event(), Event()
    prepare = platform.prepare

    def revoke(record: WorkerAttemptRecord) -> WorkerRevocationEvidence:
        started.set()
        assert release.wait(timeout=10)
        return WorkerRevocationEvidence(
            attempt_id=record.intent.attempt_id,
            sandbox_id="synthetic-worker",
            observer_id="synthetic-outside-observer",
            evidence_sha256="e" * 64,
            outcome="revoked",
        )

    worker = gateway._worker_sessions[id(service)]
    assert worker._plan is not None
    worker._plan = replace(worker._plan, revoke=revoke)
    try:
        lease.fence()
        lease.fence()
        # The caller that fenced has returned while the control plane is still being asked.
        assert started.wait(timeout=10)
        fenced = service.worker_attempt()
        assert fenced is not None
        assert fenced.phase == "fenced"
        release.set()
        assert _settles(service, "revoked")
        assert platform.calls == ["prepare", "launch", "connect"]
        assert prepare is not None
    finally:
        release.set()
        gateway.stop()
    assert platform.calls == ["prepare", "launch", "connect"]
    assert not service.store.owns_writer


def test_failed_prompt_revocation_leaves_the_attempt_fenced_for_close(tmp_path: Path) -> None:
    platform = Platform()
    gateway = configured_gateway(tmp_path, platform)
    service = gateway._service("session-worker")
    backend = service.backend
    assert isinstance(backend, OpenHandsSdkBackend)
    assert backend._worker_context is not None
    lease = backend._worker_context().workspace.attempt
    platform.fail = "revoke"
    lease.fence()
    deadline = monotonic() + 10
    while platform.calls.count("revoke") < 1 and monotonic() < deadline:
        sleep(0.01)
    assert _settles(service, "fenced")
    with pytest.raises(ExceptionGroup, match="unable to close"):
        gateway.stop()
    assert platform.calls.count("revoke") == 2
    assert bool(service.store.owns_writer)
    platform.fail = None
    gateway.stop()
    assert platform.calls.count("revoke") == 3
    assert _settles(service, "revoked")


def test_failed_revocation_retains_service_and_writer_until_retry(tmp_path: Path) -> None:
    platform = Platform()
    gateway = configured_gateway(tmp_path, platform)
    service = gateway._service("session-worker")
    backend = service.backend
    assert isinstance(backend, OpenHandsSdkBackend)
    assert backend._worker_context is not None
    context = backend._worker_context()
    platform.fail = "revoke"
    with pytest.raises(ExceptionGroup, match="unable to close"):
        gateway.stop()
    assert gateway._services["session-worker"] is service
    assert bool(service.store.owns_writer)
    assert not context.workspace.attempt.active
    platform.fail = None
    gateway.stop()
    assert not bool(service.store.owns_writer)
    assert platform.calls.count("launch") == 1
    assert platform.calls.count("revoke") == 2


def test_managed_workers_reject_local_state(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="protected state"):
        SessionGateway(project=ProjectContext(tmp_path), worker_plan_factory=Platform().prepare)


def test_managed_workers_reject_local_backend_override(tmp_path: Path) -> None:
    identity = uuid4()
    workspace = tmp_path / "project"
    workspace.mkdir()
    project = ProjectContext(
        workspace, protected_state=tmp_path / "protected" / str(identity), project_id=identity
    )
    with pytest.raises(ValueError, match="native gateway backend"):
        SessionGateway(
            project=project, worker_plan_factory=Platform().prepare, backend_id="deterministic"
        )


def test_failed_revocation_prevents_configuration_replacement(tmp_path: Path) -> None:
    platform = Platform()
    gateway = configured_gateway(tmp_path, platform)
    service = gateway._service("session-worker")
    backend = service.backend
    assert isinstance(backend, OpenHandsSdkBackend)
    assert backend._worker_context is not None
    context = backend._worker_context()
    config = gateway.config_store.load()
    gateway.config_store.save(
        replace(config, policy=config.policy.model_copy(update={"policy_id": "updated"}))
    )
    platform.fail = "revoke"
    with pytest.raises(WorkerAttemptError, match="revocation"):
        gateway._service("session-worker")
    assert gateway._services["session-worker"] is service
    assert bool(service.store.owns_writer)
    assert not context.workspace.attempt.active
    assert platform.calls.count("launch") == 1
    platform.fail = None
    replacement = gateway._service("session-worker")
    assert replacement is not service
    assert not bool(service.store.owns_writer)
    assert platform.calls.count("launch") == 1
    gateway.stop()


def test_managed_workers_reject_injected_session_factory(tmp_path: Path) -> None:
    platform = Platform()
    gateway = configured_gateway(tmp_path, platform)
    try:
        with pytest.raises(ValueError, match="native gateway backend"):
            SessionGateway(
                project=gateway.project,
                worker_plan_factory=platform.prepare,
                service_factory=lambda _root, _session: pytest.fail("Factory was called"),
            )
    finally:
        gateway.stop()


def test_worker_model_preflight_does_not_resolve_gateway_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    platform = Platform()
    gateway = configured_gateway(tmp_path, platform)
    service = gateway._service("session-worker")
    backend = service.backend
    assert isinstance(backend, OpenHandsSdkBackend)
    backend.profile = replace(
        backend.profile, credential_kind="environment", api_key_env="SYNTHETIC_PROVIDER_KEY"
    )

    def reject_lookup(*_args: object, **_kwargs: object) -> str:
        pytest.fail("Worker model preflight resolved a gateway credential")

    monkeypatch.setattr(ModelProfile, "resolve_api_key", reject_lookup)
    try:
        assert backend.configuration_error is None
        assert backend.credential_reference == "SYNTHETIC_PROVIDER_KEY"
        assert platform.calls == []
    finally:
        gateway.stop()


@pytest.mark.parametrize("denied", ["endpoint", "credential", "tier", "identity-route"])
def test_model_admission_precedes_all_platform_callbacks(tmp_path: Path, denied: str) -> None:
    platform = Platform()
    gateway = configured_gateway(tmp_path, platform)
    config = gateway.config_store.load()
    profile = config.model_settings.profile()
    policy = config.policy
    if denied == "endpoint":
        policy = policy.model_copy(update={"allowed_model_endpoints": ()})
    elif denied == "tier":
        policy = policy.model_copy(update={"allowed_capability_tiers": ("experimental",)})
    elif denied == "credential":
        profile = replace(profile, credential_kind="environment", api_key_env="UNAPPROVED_KEY")
    else:
        profile = replace(profile, aws_region_name="us-east-1")
    gateway.config_store.save(
        replace(
            config,
            policy=policy,
            model_settings=replace(config.model_settings, profiles=(profile,)),
        )
    )
    service = gateway._service("session-worker")
    backend = service.backend
    assert isinstance(backend, OpenHandsSdkBackend)
    assert backend._worker_context is not None
    try:
        with pytest.raises(WorkerAttemptError):
            backend._worker_context()
        assert platform.calls == []
        assert service.worker_attempt() is None
    finally:
        gateway.stop()


def test_preparation_is_released_when_journal_rejects_launch_intent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    platform = Platform()
    original = platform.prepare
    released: list[bool] = []

    def prepare(request: WorkerSessionRequest) -> WorkerSessionPlan:
        return replace(original(request), release_preparation=lambda: released.append(True))

    monkeypatch.setattr(platform, "prepare", prepare)
    gateway = configured_gateway(tmp_path, platform)
    service = gateway._service("session-worker")
    backend = service.backend
    assert isinstance(backend, OpenHandsSdkBackend)
    assert backend._worker_context is not None

    def reject_intent(*_args: object, **_kwargs: object) -> WorkerAttemptLease:
        raise WorkerAttemptError("synthetic journal failure")

    monkeypatch.setattr(service, "launch_worker", reject_intent)
    with pytest.raises(WorkerAttemptError, match="journal"):
        backend._worker_context()
    assert service.worker_attempt() is None
    assert platform.calls == ["prepare"]
    gateway.stop()
    assert released == [True]
    assert not service.store.owns_writer


def test_failed_revocation_retains_preparation_for_reconciliation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    platform = Platform()
    original = platform.prepare
    released: list[bool] = []

    def prepare(request: WorkerSessionRequest) -> WorkerSessionPlan:
        return replace(original(request), release_preparation=lambda: released.append(True))

    monkeypatch.setattr(platform, "prepare", prepare)
    gateway = configured_gateway(tmp_path, platform)
    service = gateway._service("session-worker")
    backend = service.backend
    assert isinstance(backend, OpenHandsSdkBackend)
    assert backend._worker_context is not None
    backend._worker_context()
    platform.fail = "revoke"
    with pytest.raises(ExceptionGroup):
        gateway.stop()
    assert released == []
    assert service.store.owns_writer
    platform.fail = None
    gateway.stop()
    assert released == [True]
    assert not service.store.owns_writer


def test_gateway_binds_outside_adapter_once_per_owned_session(tmp_path: Path) -> None:
    adapter = Mock()
    factory = Mock(return_value=adapter)
    gateway = configured_gateway(tmp_path, Platform(), external_actions_factory=factory)
    try:
        service = gateway._service("outside")
        assert service._external_actions is adapter
        assert gateway._service("outside") is service
        factory.assert_called_once_with("outside")
    finally:
        gateway.stop()


def test_outside_services_require_managed_worker_ownership(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Outside services require"):
        SessionGateway(
            project=ProjectContext(tmp_path),
            env={},
            external_actions_factory=lambda _: cast(ExternalActionAdapter, Mock()),
        )


class Reconciler:
    def __init__(self) -> None:
        self.fail = False
        self.records: list[WorkerAttemptRecord] = []

    def __call__(self, record: WorkerAttemptRecord) -> WorkerRevocationEvidence:
        self.records.append(record)
        if self.fail:
            raise TimeoutError("synthetic control plane outage")
        return WorkerRevocationEvidence(
            attempt_id=record.intent.attempt_id,
            sandbox_id=record.runtime.sandbox_id if record.runtime else None,
            observer_id="synthetic-outside-observer",
            evidence_sha256="e" * 64,
            outcome="revoked" if record.runtime else "not-created",
        )


def _end_without_revocation(gateway: SessionGateway) -> None:
    """Stand in for a gateway process that ended: its ownership lapses, nothing is revoked."""
    for service in gateway._services.values():
        service.store.release_writer()
    gateway._project_activity.close()


def _successor(
    gateway: SessionGateway, reconciler: Reconciler | None, platform: Platform | None = None
) -> SessionGateway:
    platform = platform or Platform()
    platform.gateway = SessionGateway(
        project=gateway.project,
        worker_plan_factory=platform.prepare,
        worker_reconciler=reconciler,
        env={},
    )
    return platform.gateway


def _retained(gateway: SessionGateway) -> WorkerAttemptRecord:
    record = current_worker_attempt(
        FileSessionStore(gateway.sessions_root, "session-worker").replay_events()
    )
    assert record is not None
    return record


@pytest.mark.parametrize("left", ["active", "fenced"])
def test_new_gateway_reconciles_attempt_left_by_an_ended_process(
    tmp_path: Path, left: Literal["active", "fenced"]
) -> None:
    platform = Platform()
    gateway = configured_gateway(tmp_path, platform)
    service = gateway._service("session-worker")
    backend = service.backend
    assert isinstance(backend, OpenHandsSdkBackend)
    assert backend._worker_context is not None
    lease = backend._worker_context().workspace.attempt
    if left == "fenced":
        platform.fail = "revoke"
        lease.fence()
        assert _settles(service, "fenced")
        gateway._worker_sessions[id(service)]._revocation.join(timeout=10)  # type: ignore[union-attr]
        assert platform.calls.count("revoke") == 1
    orphan = service.worker_attempt()
    assert orphan is not None
    assert orphan.phase == left
    _end_without_revocation(gateway)

    reconciler = Reconciler()
    relaunch = Platform()
    successor = _successor(gateway, reconciler, relaunch)
    try:
        assert successor.reconcile_workers() == ()
        assert [record.phase for record in reconciler.records] == ["fenced"]
        assert reconciler.records[0].intent == orphan.intent
        assert reconciler.records[0].runtime == orphan.runtime
        assert successor._services == {}
        revoked = _retained(successor)
        assert revoked.phase == "revoked"
        assert revoked.revocation is not None
        assert revoked.revocation.attempt_id == orphan.intent.attempt_id
        assert successor.reconcile_workers() == ()
        assert len(reconciler.records) == 1
        assert relaunch.calls == []

        restored = successor._service("session-worker")
        restored.audit_log.verify()
        restored_backend = restored.backend
        assert isinstance(restored_backend, OpenHandsSdkBackend)
        assert restored_backend._worker_context is not None
        context = restored_backend._worker_context()
        assert relaunch.calls == ["prepare", "launch", "connect"]
        replacement = context.workspace.attempt
        assert replacement.active
        assert replacement.intent.attempt_id != orphan.intent.attempt_id
        assert replacement.intent.conversation_id == orphan.intent.conversation_id
        assert restored.worker_attempt() == WorkerAttemptRecord(
            phase="active", intent=replacement.intent, runtime=replacement.runtime
        )
        notices = [
            message
            for message in project_session(
                restored.replay_events(), session_id="session-worker"
            ).conversation
            if "new worker" in message.content
        ]
        assert (len(notices), notices[0].role) == (1, "trace")
        assert "does not remember" in (notices[0].detail or "")
    finally:
        successor.stop()
    assert relaunch.calls == ["prepare", "launch", "connect", "revoke"]
    assert _retained(successor).phase == "revoked"


def test_unobserved_reconciliation_keeps_the_attempt_fenced_until_retry(tmp_path: Path) -> None:
    gateway = configured_gateway(tmp_path, Platform())
    backend = gateway._service("session-worker").backend
    assert isinstance(backend, OpenHandsSdkBackend)
    assert backend._worker_context is not None
    backend._worker_context()
    _end_without_revocation(gateway)

    reconciler = Reconciler()
    reconciler.fail = True
    successor = _successor(gateway, reconciler)
    try:
        assert successor.reconcile_workers() == ("session-worker",)
        assert _retained(successor).phase == "fenced"
        blocked = Platform()
        blocked.gateway = successor
        successor._worker_plan_factory = blocked.prepare
        backend = successor._service("session-worker").backend
        assert isinstance(backend, OpenHandsSdkBackend)
        assert backend._worker_context is not None
        with pytest.raises(WorkerAttemptError, match="has not been revoked"):
            backend._worker_context()
        assert blocked.calls == ["prepare"]
        reconciler.fail = False
    finally:
        successor.stop()
    assert _retained(successor).phase == "fenced"
    retry = _successor(gateway, reconciler)
    try:
        assert retry.reconcile_workers() == ()
        assert _retained(retry).phase == "revoked"
    finally:
        retry.stop()


def test_cleanly_stopped_session_launches_a_replacement_in_the_next_gateway(
    tmp_path: Path,
) -> None:
    platform = Platform()
    gateway = configured_gateway(tmp_path, platform)
    backend = gateway._service("session-worker").backend
    assert isinstance(backend, OpenHandsSdkBackend)
    assert backend._worker_context is not None
    first = backend._worker_context().workspace.attempt
    assert (
        project_session(
            gateway._service("session-worker").replay_events(), session_id="session-worker"
        ).conversation
        == ()
    )
    gateway.stop()
    relaunch = Platform()
    successor = _successor(gateway, None, relaunch)
    try:
        assert successor.session_projection(session_id="session-worker").conversation == ()
        assert successor._services == {}
        assert relaunch.calls == []
        restored_backend = successor._service("session-worker").backend
        assert isinstance(restored_backend, OpenHandsSdkBackend)
        assert restored_backend._worker_context is not None
        second = restored_backend._worker_context().workspace.attempt
        assert not first.active
        assert second.active
        assert second.intent.attempt_id != first.intent.attempt_id
        assert relaunch.calls == ["prepare", "launch", "connect"]
    finally:
        successor.stop()


def test_reconciliation_leaves_this_gateways_own_worker_running(tmp_path: Path) -> None:
    platform = Platform()
    reconciler = Reconciler()
    gateway = configured_gateway(tmp_path, platform)
    gateway._worker_reconciler = reconciler
    service = gateway._service("session-worker")
    backend = service.backend
    assert isinstance(backend, OpenHandsSdkBackend)
    assert backend._worker_context is not None
    context = backend._worker_context()
    try:
        assert gateway.reconcile_workers() == ()
        assert reconciler.records == []
        assert context.workspace.attempt.active
    finally:
        gateway.stop()


def test_gateway_without_a_reconciler_leaves_retained_attempts_untouched(tmp_path: Path) -> None:
    gateway = configured_gateway(tmp_path, Platform())
    backend = gateway._service("session-worker").backend
    assert isinstance(backend, OpenHandsSdkBackend)
    assert backend._worker_context is not None
    backend._worker_context()
    _end_without_revocation(gateway)
    successor = _successor(gateway, None)
    try:
        assert successor.reconcile_workers() == ()
        assert successor._services == {}
    finally:
        successor.stop()
    with pytest.raises(ValueError, match="requires managed workers"):
        SessionGateway(
            project=ProjectContext(tmp_path / "project"), env={}, worker_reconciler=Reconciler()
        )


def test_sandbox_review_applies_only_while_the_worker_attempt_is_admitted(tmp_path: Path) -> None:
    platform = Platform()
    gateway = configured_gateway(tmp_path, platform)
    service = gateway._service("session-worker")
    backend = service.backend
    assert isinstance(backend, OpenHandsSdkBackend)
    assert backend._worker_context is not None
    assert gateway.action_settings()["review_mode"] == "review-outside-effects"
    assert service.action_review_mode() == "review-all"

    backend._worker_context()
    assert service.action_review_mode() == "review-outside-effects"

    gateway.select_action_review_mode("review-all")
    assert service.action_review_mode() == "review-all"
    gateway.select_action_review_mode("review-outside-effects")
    assert service.action_review_mode() == "review-outside-effects"

    assert platform.lease is not None
    platform.lease.fence()
    assert service.action_review_mode() == "review-all"
    gateway.stop()


def test_gateway_without_workers_never_runs_a_revoked_worker_session_on_the_host(
    tmp_path: Path,
) -> None:
    platform = Platform()
    gateway = configured_gateway(tmp_path, platform)
    backend = gateway._service("session-worker").backend
    assert isinstance(backend, OpenHandsSdkBackend)
    assert backend._worker_context is not None
    backend._worker_context()
    gateway.stop()
    assert _retained(gateway).phase == "revoked"

    host = SessionGateway(project=gateway.project, env={})
    try:
        service = host._service("session-worker")
        host_backend = service.backend
        assert isinstance(host_backend, OpenHandsSdkBackend)
        assert host_backend._worker_context is None
        assert service.action_review_mode() == "review-all"

        result = host.handle(
            SessionCommand(
                command_id="chat-on-host",
                session_id="session-worker",
                kind=CommandKind.CHAT,
                created_at="2026-01-01T00:00:00Z",
                payload={"prompt": "Continue the synthetic analysis."},
            )
        )

        assert host_backend._conversation is None
        assert result.events[-1].kind == EventKind.ERROR_RECORDED
        assert not any(event.kind == EventKind.USER_MESSAGE_RECORDED for event in result.events)
        projection = host.session_projection(session_id="session-worker")
        assert projection.pending_approval is None
        assert _retained(gateway).phase == "revoked"
    finally:
        host.stop()

    # A later gateway that launches workers still replaces the revoked attempt.
    relaunch = Platform()
    successor = _successor(gateway, None, relaunch)
    try:
        restored = successor._service("session-worker")
        assert restored.action_review_mode() == "review-all"
        restored_backend = restored.backend
        assert isinstance(restored_backend, OpenHandsSdkBackend)
        assert restored_backend._worker_context is not None
        restored_backend._worker_context()
        assert restored.action_review_mode() == "review-outside-effects"
    finally:
        successor.stop()


def test_connection_failure_after_prompt_revocation_keeps_its_own_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    platform = Platform()
    original = platform.prepare
    gateway = configured_gateway(tmp_path, platform)
    service = gateway._service("session-worker")

    def prepare(request: WorkerSessionRequest) -> WorkerSessionPlan:
        plan = original(request)

        def connect(lease: WorkerAttemptLease) -> WorkerConversationContext:
            # A transport thread fences while connection is still being established.
            lease.fence()
            assert _settles(service, "revoked")
            raise RuntimeError("synthetic connection failure")

        return replace(plan, connect=connect)

    monkeypatch.setattr(platform, "prepare", prepare)
    gateway._worker_plan_factory = prepare
    gateway._worker_sessions[id(service)]._prepare = prepare
    backend = service.backend
    assert isinstance(backend, OpenHandsSdkBackend)
    assert backend._worker_context is not None
    try:
        with pytest.raises(RuntimeError, match="synthetic connection failure"):
            backend._worker_context()
        record = service.worker_attempt()
        assert record is not None
        assert record.phase == "revoked"
    finally:
        gateway.stop()
    assert platform.calls.count("revoke") == 1


@pytest.mark.parametrize("revocation", ["finishes", "stalls"])
def test_close_waits_a_bounded_time_for_prompt_revocation_and_holds_none_of_its_locks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, revocation: str
) -> None:
    monkeypatch.setattr("heartwood.gateway.worker_sessions._CLOSE_WAIT_SECONDS", 1.0)
    platform = Platform()
    gateway = configured_gateway(tmp_path, platform)
    service = gateway._service("session-worker")
    backend = service.backend
    assert isinstance(backend, OpenHandsSdkBackend)
    assert backend._worker_context is not None
    lease = backend._worker_context().workspace.attempt
    started, release = Event(), Event()
    worker = gateway._worker_sessions[id(service)]
    assert worker._plan is not None
    revoke = worker._plan.revoke

    def stalled(record: WorkerAttemptRecord) -> WorkerRevocationEvidence:
        started.set()
        assert release.wait(timeout=10)
        return revoke(record)

    worker._plan = replace(worker._plan, revoke=stalled)
    try:
        lease.fence()
        assert started.wait(timeout=10)
        if revocation == "finishes":
            # The revocation needs the session's locks while the close waits under the gateway's.
            Timer(0.2, release.set).start()
        began = monotonic()
        if revocation == "stalls":
            with pytest.raises(ExceptionGroup, match="unable to close"):
                gateway.stop()
            assert monotonic() - began < 5
            assert bool(service.store.owns_writer)
            release.set()
            assert _settles(service, "revoked")
        gateway.stop()
        assert monotonic() - began < 5
    finally:
        release.set()
        gateway.stop()
    assert _settles(service, "revoked")
    assert not service.store.owns_writer


def test_close_during_launch_returns_at_once_and_the_launch_revokes_and_closes(
    tmp_path: Path,
) -> None:
    platform = Platform()
    gateway = configured_gateway(tmp_path, platform)
    launching, release = Event(), Event()
    prepare = platform.prepare

    def slow_prepare(request: WorkerSessionRequest) -> WorkerSessionPlan:
        plan = prepare(request)

        def slow_launch(intent: WorkerAttemptIntent) -> WorkerRuntimeBinding:
            launching.set()
            assert release.wait(timeout=10)
            return plan.launch(intent)

        return replace(plan, launch=slow_launch)

    gateway._worker_plan_factory = slow_prepare
    service = gateway._service("session-worker")
    backend = service.backend
    assert isinstance(backend, OpenHandsSdkBackend)
    assert backend._worker_context is not None
    context = backend._worker_context
    worker = gateway._worker_sessions[id(service)]
    failures: list[BaseException] = []

    def open_context() -> None:
        try:
            context()
        except BaseException as error:
            failures.append(error)

    opening = Thread(target=open_context, daemon=True)
    opening.start()
    try:
        assert launching.wait(timeout=10)
        began = monotonic()
        # The close does not wait for the control plane; the launch finishes it.
        assert not worker.close()
        assert monotonic() - began < 1
        assert worker.closing
        assert not worker.wait_closed(0.1)
        assert bool(service.store.owns_writer)
        release.set()
        assert worker.wait_closed(10)
        opening.join(timeout=10)
        assert not opening.is_alive()
        assert isinstance(failures[0], WorkerAttemptError)
        assert "connect" not in platform.calls
        assert platform.calls.count("revoke") == 1
        assert _settles(service, "revoked")
        assert not service.store.owns_writer
        assert worker.close()
    finally:
        release.set()
        gateway.stop()
    assert platform.calls.count("revoke") == 1


def _chat(command_id: str, session_id: str = "session-worker") -> SessionCommand:
    return SessionCommand(
        command_id=command_id,
        session_id=session_id,
        kind=CommandKind.CHAT,
        created_at="2026-01-01T00:00:00Z",
        payload={"prompt": "Continue the synthetic analysis."},
    )


def _local_skill(gateway: SessionGateway) -> Path:
    source = gateway.project.root / "skills" / "community-summary"
    bundled = Path(__file__).resolve().parents[3] / "vendor/heartwood-skills/skills"
    shutil.copytree(bundled / "aggregate-export", source)
    skill_file = source / "SKILL.md"
    skill_file.write_text(
        skill_file.read_text(encoding="utf-8")
        .replace("name: aggregate-export", "name: community-summary")
        .replace(
            'heartwood.id: "heartwood.research.aggregate-export"',
            'heartwood.id: "example.community-summary"',
        ),
        encoding="utf-8",
    )
    return source


@pytest.mark.parametrize("change", ["model", "skill"])
def test_settings_change_during_a_stalled_launch_revokes_it_and_relaunches_with_the_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    monkeypatch.setattr("heartwood.gateway.worker_sessions._CLOSE_WAIT_SECONDS", 5.0)
    platform = Platform()
    gateway = configured_gateway(tmp_path, platform)
    launching, release = Event(), Event()
    prepare = platform.prepare
    launched: list[tuple[SessionService, WorkerSessionRequest, tuple[SkillManifest, ...]]] = []

    def slow_prepare(request: WorkerSessionRequest) -> WorkerSessionPlan:
        launched.append(
            (
                gateway._services[request.session_id],
                request,
                request.skills.active_skill_manifests(),
            )
        )
        plan = prepare(request)
        if len(launched) > 1:
            return plan

        def slow_launch(intent: WorkerAttemptIntent) -> WorkerRuntimeBinding:
            launching.set()
            assert release.wait(timeout=10)
            return plan.launch(intent)

        return replace(plan, launch=slow_launch)

    gateway._worker_plan_factory = slow_prepare
    source = _local_skill(gateway) if change == "skill" else None
    outcomes: list[object] = []

    def chat() -> None:
        try:
            outcomes.append(gateway.handle(_chat("chat-during-launch")))
        except BaseException as error:
            outcomes.append(error)

    command = Thread(target=chat, daemon=True)
    command.start()
    try:
        assert launching.wait(timeout=10)
        began = monotonic()
        if source is None:
            profile = gateway.settings_store.load().profile()
            gateway.save_model_profile(replace(profile, model="openai/synthetic-updated"))
            assert gateway.settings_store.load().profile().model == "openai/synthetic-updated"
        else:
            inspected = gateway.inspect_local_skill(source)
            settings = gateway.install_local_skill(
                source, expected_tree_sha256=inspected["tree_sha256"], approved=True
            )
            assert any(skill["name"] == "community-summary" for skill in settings["skills"])
        # The saved change is reported without waiting for the launch under the state lock.
        assert monotonic() - began < 2
        assert gateway.session_projection(session_id="session-other").session_id == (
            "session-other"
        )
        # The session whose close waits for its launch is read from its journal.
        assert "session-worker" in gateway._releasing
        gateway.session_projection(session_id="session-worker")
        assert monotonic() - began < 2
        release.set()
        command.join(timeout=10)
        assert not command.is_alive()
        first = launched[0][0]
        # The launch that finished after the change connects nothing and is revoked.
        assert _settles(first, "revoked")
        deadline = monotonic() + 10
        while first.store.owns_writer and monotonic() < deadline:
            sleep(0.01)
        assert not first.store.owns_writer
        # The command that waited for the launch reached no worker. It was decided again
        # with the new service, or it was refused and the next command relaunches.
        refused = outcomes[0]
        assert isinstance(refused, SessionResult)
        assert refused.events[-1].kind == EventKind.ERROR_RECORDED
        if len(launched) == 1:
            gateway.handle(_chat("chat-after-launch"))
        assert len(launched) == 2
        replacement, request, skills = launched[1]
        assert replacement is not first
        if source is None:
            assert request.profile.model == "openai/synthetic-updated"
        else:
            assert len(skills) == len(launched[0][2]) + 1
        assert platform.calls[:2] == ["prepare", "launch"]
        assert platform.calls.count("connect") == 1
    finally:
        release.set()
        gateway.stop()
