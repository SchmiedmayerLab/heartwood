# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Literal
from uuid import uuid4

import pytest
from pydantic import AnyHttpUrl, SecretStr

from heartwood.core_adapter.worker_attempts import (
    WorkerAttemptError,
    WorkerAttemptIntent,
    WorkerAttemptLease,
    WorkerAttemptRecord,
    WorkerRevocationEvidence,
    WorkerRuntimeBinding,
)
from heartwood.gateway import ProjectContext, SessionGateway
from heartwood.gateway._model_settings import ModelProfile, ModelSettings
from heartwood.gateway._openhands_sdk import OpenHandsSdkBackend, WorkerConversationContext
from heartwood.gateway._worker_events import AdmittedWorkerWorkspace
from heartwood.gateway.worker_sessions import WorkerSessionPlan, WorkerSessionRequest
from heartwood.schemas import PolicyProfile


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
        assert request.action_confirmation_mode == service.backend.action_confirmation_mode
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


def configured_gateway(tmp_path: Path, platform: Platform) -> SessionGateway:
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
    gateway = SessionGateway(project=project, worker_plan_factory=platform.prepare, env={})
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
        record = service.worker_attempt()
        assert (
            record is None
            if failure == "prepare"
            else record is not None and record.phase == "fenced"
        )
    finally:
        platform.fail = None
        gateway.stop()


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
