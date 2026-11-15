# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Literal, cast
from unittest.mock import MagicMock

import pytest
from openhands.sdk.conversation import RemoteConversation
from openhands.sdk.conversation.request import StartConversationRequest
from openhands.sdk.event import StreamingDeltaEvent
from openhands.sdk.security import AlwaysConfirm
from openhands.sdk.skills import Skill
from pydantic import SecretStr

import heartwood.gateway._openhands_sdk as adapter
from heartwood.core_adapter.worker_attempts import WorkerAttemptError, WorkerAttemptLease
from heartwood.gateway._model_settings import ModelProfile
from heartwood.gateway._openhands_sdk import (
    OpenHandsSdkBackend,
    OpenHandsSdkError,
    WorkerConversationContext,
)
from heartwood.gateway._specialist_task import HeartwoodSpecialistToolSet
from heartwood.gateway._specialists import SpecialistCatalog, load_specialist_catalog
from heartwood.gateway._worker_events import AdmittedWorkerWorkspace


@pytest.fixture
def context(worker_attempt: Callable[[str], WorkerAttemptLease]) -> WorkerConversationContext:
    lease = worker_attempt("http://worker.test")
    # Isolate request construction from transport hooks, qualified separately with the native SDK.
    workspace = AdmittedWorkerWorkspace.model_construct(
        host="http://worker.test",
        working_dir="/workspace",
        attempt=lease,
        tool_modules=(),
    )
    return WorkerConversationContext(workspace, SecretStr("synthetic-inference-placeholder"))


def _backend(
    tmp_path: Path,
    context: WorkerConversationContext,
    *,
    specialist_catalog: SpecialistCatalog | None = None,
    structured_task_outcomes: bool = False,
    mode: Literal["always-confirm", "confirm-risky"] = "always-confirm",
) -> OpenHandsSdkBackend:
    profile = ModelProfile(
        profile_id="synthetic",
        model="openai/synthetic",
        base_url="https://model.test/v1",
        policy_endpoint="https://model.test/v1/chat/completions",
        credential_kind="environment",
        api_key_env="REAL_PROVIDER_KEY",
    )
    return OpenHandsSdkBackend(
        profile=profile,
        workspace=tmp_path / "project",
        skills_dir=tmp_path / "private-skills",
        persistence_dir=tmp_path / "private-runtime",
        conversation_key="synthetic",
        worker_context=lambda: context,
        env={"REAL_PROVIDER_KEY": "synthetic-real-secret"},
        specialist_catalog=specialist_catalog,
        structured_task_outcomes=structured_task_outcomes,
        action_confirmation_mode=mode,
    )


@pytest.mark.parametrize("mode", ["always-confirm", "confirm-risky"])
def test_remote_request_reuses_agent_contract_without_resolving_host_credentials(
    tmp_path: Path,
    context: WorkerConversationContext,
    monkeypatch: pytest.MonkeyPatch,
    mode: Literal["always-confirm", "confirm-risky"],
) -> None:
    requests: list[StartConversationRequest] = []
    callbacks: list[Callable[[object], None]] = []
    remote = cast(RemoteConversation, MagicMock(spec=RemoteConversation))

    def create(
        workspace: AdmittedWorkerWorkspace, request: StartConversationRequest, **kwargs: object
    ) -> RemoteConversation:
        assert workspace is context.workspace
        requests.append(request)
        callbacks.extend(cast(list[Callable[[object], None]], kwargs["callbacks"]))
        assert kwargs["visualizer"] is None
        return remote

    def reject_credential_lookup(*_args: object, **_kwargs: object) -> str:
        pytest.fail("Gateway provider credential was resolved")

    monkeypatch.setattr(RemoteConversation, "create", create)
    monkeypatch.setattr(ModelProfile, "resolve_api_key", reject_credential_lookup)
    backend = _backend(tmp_path, context, structured_task_outcomes=True, mode=mode)
    deltas: list[str] = []
    backend.bind_runtime(event_sink=lambda _events: None, token_sink=deltas.append)
    assert backend._get_conversation() is remote
    assert backend._get_conversation() is remote
    assert len(requests) == 1
    request = requests[0]
    serialized = request.model_dump_json(context={"expose_secrets": True})
    decoded = StartConversationRequest.model_validate_json(serialized)
    assert isinstance(decoded.confirmation_policy, AlwaysConfirm)
    assert "synthetic-inference-placeholder" in serialized
    assert "synthetic-real-secret" not in serialized
    assert str(tmp_path) not in serialized
    assert request.conversation_id == context.workspace.attempt.intent.conversation_id
    assert request.workspace.working_dir == "/workspace"
    assert request.autotitle is False
    assert request.max_iterations == 100
    assert request.stuck_detection is True
    assert request.agent.tool_concurrency_limit == 1
    tools = {tool.name: tool for tool in request.agent.tools}
    assert {
        "terminal",
        "heartwood_project_file_editor",
        "task_tracker",
        "FinishTool",
    } <= tools.keys()
    assert tools["heartwood_project_file_editor"].params == {"project_root": "/workspace"}
    assert request.agent.agent_context is not None
    assert request.agent.agent_context.load_user_skills is False
    assert request.agent.agent_context.load_project_skills is False
    assert request.agent.agent_context.load_public_skills is False
    assert request.agent.agent_context.load_memory is False
    callbacks[0](StreamingDeltaEvent(content="synthetic delta"))
    assert deltas == ["synthetic delta"]
    context.workspace.attempt.fence()
    with pytest.raises(WorkerAttemptError):
        callbacks[0](StreamingDeltaEvent(content="stale delta"))
    assert deltas == ["synthetic delta"]
    backend.close()


def test_uncertain_create_fences_attempt_and_is_not_retried(
    tmp_path: Path, context: WorkerConversationContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []

    def create(*_args: object, **_kwargs: object) -> RemoteConversation:
        calls.append(1)
        raise TimeoutError("synthetic private response")

    monkeypatch.setattr(RemoteConversation, "create", create)
    backend = _backend(tmp_path, context)
    for _ in range(2):
        with pytest.raises(OpenHandsSdkError, match="outside reconciliation") as error:
            backend._get_conversation()
        assert "private response" not in str(error.value)
    assert calls == [1]
    assert not context.workspace.attempt.active
    backend.close()


def test_revoked_attempt_never_creates_a_conversation(
    tmp_path: Path, context: WorkerConversationContext
) -> None:
    context.workspace.attempt.fence()
    backend = _backend(tmp_path, context)
    with pytest.raises(WorkerAttemptError):
        backend._get_conversation()
    backend.close()


@pytest.mark.parametrize("placeholder", ["", "bad\nvalue", "x" * 4097])
def test_invalid_inference_placeholder_stops_before_creation(
    tmp_path: Path, context: WorkerConversationContext, placeholder: str
) -> None:
    backend = _backend(tmp_path, replace(context, inference_placeholder=SecretStr(placeholder)))
    with pytest.raises(OpenHandsSdkError, match="placeholder"):
        backend._get_conversation()
    backend.close()


def test_remote_specialists_require_qualified_image_catalog(
    tmp_path: Path, context: WorkerConversationContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = Path(__file__).resolve().parents[3]
    catalog = load_specialist_catalog(
        root / "agents/verified", root / "vendor/heartwood-skills/skills"
    )
    backend = _backend(tmp_path, context, specialist_catalog=catalog)
    with pytest.raises(OpenHandsSdkError, match="catalog"):
        backend._get_conversation()
    backend.close()
    matching = replace(context, specialist_catalog_sha256=catalog.fingerprint)
    backend = _backend(tmp_path, matching, specialist_catalog=catalog)
    requests: list[StartConversationRequest] = []
    remote = cast(RemoteConversation, MagicMock(spec=RemoteConversation))

    def create(
        _workspace: object, request: StartConversationRequest, **_kwargs: object
    ) -> RemoteConversation:
        requests.append(request)
        return remote

    monkeypatch.setattr(RemoteConversation, "create", create)
    backend._get_conversation()
    assert any(tool.name == HeartwoodSpecialistToolSet.name for tool in requests[0].agent.tools)
    assert requests[0].agent_definitions == []
    backend.close()


def test_gateway_parallel_executor_is_not_installed_on_remote_copy(
    tmp_path: Path, context: WorkerConversationContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote = cast(RemoteConversation, MagicMock(spec=RemoteConversation))
    monkeypatch.setattr(RemoteConversation, "create", lambda *_args, **_kwargs: remote)

    def reject(*_args: object) -> None:
        pytest.fail("Gateway executor installed on a remote agent copy")

    monkeypatch.setattr(adapter, "bind_review_executor", reject)
    backend = _backend(tmp_path, context)
    backend.bind_review_authorizer(lambda _events: None)
    assert backend._get_conversation() is remote
    backend.close()


def test_skill_host_paths_are_not_accepted(context: WorkerConversationContext) -> None:
    skill = Skill(
        name="synthetic",
        content="synthetic",
        source="/private/state/SKILL.md",
        is_agentskills_format=True,
    )
    with pytest.raises(OpenHandsSdkError, match="projection"):
        replace(context, skills=(skill,))


@pytest.mark.parametrize("mode", ["subscription", "identity", "aws-profile", "aws-region"])
def test_unsupported_credential_routes_fail_before_creation(
    tmp_path: Path, context: WorkerConversationContext, mode: str
) -> None:
    backend = _backend(tmp_path, context)
    if mode == "subscription":
        backend.profile = replace(backend.profile, auth_type="subscription")
    elif mode == "identity":
        backend.profile = replace(backend.profile, credential_kind="managed-identity")
    elif mode == "aws-profile":
        backend.profile = replace(backend.profile, aws_profile_name="outside-identity")
    else:
        backend.profile = replace(backend.profile, aws_region_name="us-east-1")
    with pytest.raises(OpenHandsSdkError, match="API-key route"):
        backend._get_conversation()
    backend.close()


def test_lease_revoked_during_creation_closes_client_and_refuses_admission(
    tmp_path: Path, context: WorkerConversationContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote = MagicMock(spec=RemoteConversation)

    def create(*_args: object, **_kwargs: object) -> RemoteConversation:
        context.workspace.attempt.fence()
        return cast(RemoteConversation, remote)

    monkeypatch.setattr(RemoteConversation, "create", create)
    backend = _backend(tmp_path, context)
    with pytest.raises(OpenHandsSdkError, match="outside reconciliation"):
        backend._get_conversation()
    remote.close.assert_called_once()
    assert not context.workspace.attempt.active
    backend.close()


def test_context_provider_failure_never_falls_back_to_local(
    tmp_path: Path, context: WorkerConversationContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail() -> WorkerConversationContext:
        raise OpenHandsSdkError("admitted worker unavailable")

    def reject(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Local fallback was used")

    backend = _backend(tmp_path, context)
    backend._worker_context = fail
    monkeypatch.setattr(adapter, "LocalConversation", reject)
    with pytest.raises(OpenHandsSdkError, match="admitted worker unavailable"):
        backend._get_conversation()
    backend.close()
