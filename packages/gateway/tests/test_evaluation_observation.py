# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Evaluation identity comes from the owned runtime, not a caller's labels."""

from concurrent.futures import ThreadPoolExecutor
from importlib.metadata import version
from pathlib import Path
from threading import Event

import pytest
from openhands.sdk import LLM
from openhands.sdk.security import ConfirmRisky
from pydantic import SecretStr

from heartwood.core_adapter import DeterministicAgentBackend, SessionService
from heartwood.gateway import ModelProfile, OpenHandsSdkBackend, ProjectContext, SessionGateway
from heartwood.schemas.evaluation import EvaluationRuntimeObservation
from heartwood.session import CommandKind, SessionCommand


def _owned_gateway(root: Path) -> tuple[SessionGateway, str]:
    gateway = SessionGateway(
        project=ProjectContext(root),
        service_factory=lambda path, session_id: SessionService.local_default(
            path, session_id=session_id, backend=DeterministicAgentBackend(), env={}
        ),
        env={},
    )
    session_id = gateway.create_session("Synthetic observation")["session_id"]
    with pytest.raises(ValueError, match="owned session"):
        gateway.bind_evaluation_observer(session_id=session_id)
    gateway.handle(
        SessionCommand(
            command_id="initial-pause",
            session_id=session_id,
            kind=CommandKind.PAUSE,
            created_at="2026-09-11T00:00:00Z",
        )
    )
    return gateway, session_id


def test_bound_runtime_observer_uses_owned_service_without_gateway_lock(tmp_path: Path) -> None:
    gateway, session_id = _owned_gateway(tmp_path)
    try:
        observe = gateway.bind_evaluation_observer(session_id=session_id)
        expected = gateway.evaluation_observation(session_id=session_id)
        with ThreadPoolExecutor(max_workers=1) as executor, gateway._state_lock:
            result = executor.submit(observe, session_id).result(timeout=5)
        assert result == expected
        with pytest.raises(ValueError, match="requested session"):
            observe("different-session")
        gateway.stop()
        with pytest.raises(ValueError, match="requested session"):
            observe(session_id)
        # A new owner does not renew a callback bound to the old service.
        gateway.handle(
            SessionCommand(
                command_id="renewed-pause",
                session_id=session_id,
                kind=CommandKind.PAUSE,
                created_at="2026-09-11T00:00:00Z",
            )
        )
        assert gateway.bind_evaluation_observer(session_id=session_id)(session_id) == expected
        with pytest.raises(ValueError, match="requested session"):
            observe(session_id)
    finally:
        gateway.stop()


def test_bound_runtime_observer_rechecks_ownership_after_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway, session_id = _owned_gateway(tmp_path)
    observe = gateway.bind_evaluation_observer(session_id=session_id)
    original = gateway._evaluation_observation
    entered, released = Event(), Event()

    def blocked(service: SessionService) -> EvaluationRuntimeObservation:
        result = original(service)
        entered.set()
        assert released.wait(timeout=10)
        return result

    monkeypatch.setattr(gateway, "_evaluation_observation", blocked)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(observe, session_id)
        try:
            assert entered.wait(timeout=5)
            gateway.stop()
        finally:
            released.set()
            gateway.stop()
        with pytest.raises(ValueError, match="ownership changed"):
            future.result(timeout=5)


def test_production_observation_needs_no_inference_and_omits_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected_completion(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Runtime observation must not request inference")

    monkeypatch.setattr(LLM, "completion", unexpected_completion)
    backend = OpenHandsSdkBackend(
        profile=ModelProfile(
            profile_id="test",
            model="openai/local-model",
            base_url="http://127.0.0.1:8765/v1",
            policy_endpoint="http://127.0.0.1:8765/v1/chat/completions",
            credential_kind="none",
            max_input_tokens=32768,
            max_output_tokens=4096,
        ),
        workspace=tmp_path,
        skills_dir=tmp_path / ".heartwood/skills",
        persistence_dir=tmp_path / ".heartwood/sessions/test/openhands",
        conversation_key="evaluation-observation-test",
        env={},
    )
    try:
        observation = backend.evaluation_observation(
            platform="generic", policy_fingerprint="a" * 64
        )
        assert observation.source == "production"
        assert observation.request_model == "openai/local-model"
        assert observation.openhands_version == version("openhands-sdk")
        assert observation.max_input_tokens == 32768
        assert observation.max_output_tokens == 4096
        llm = backend._get_conversation().state.agent.llm
        llm.api_key = SecretStr("synthetic-secret-do-not-export")
        llm.extra_headers = {"Authorization": "synthetic-header-do-not-export"}
        same = backend.evaluation_observation(platform="generic", policy_fingerprint="a" * 64)
        assert same == observation
        assert "synthetic-secret" not in same.model_dump_json()
        assert "synthetic-header" not in same.model_dump_json()
        llm.litellm_extra_body = {"chat_template_kwargs": {"enable_thinking": True}}
        changed = backend.evaluation_observation(platform="generic", policy_fingerprint="a" * 64)
        assert changed.model_options_fingerprint != observation.model_options_fingerprint
        assert "enable_thinking" not in changed.model_dump_json()
        llm.metrics.add_cost(0.01)
        assert (
            backend.evaluation_observation(platform="generic", policy_fingerprint="a" * 64)
            == changed
        )
        backend._get_conversation().set_confirmation_policy(ConfirmRisky())
        changed_policy = backend.evaluation_observation(
            platform="generic", policy_fingerprint="a" * 64
        )
        assert changed_policy.policy_fingerprint != changed.policy_fingerprint
    finally:
        backend.close()


def test_gateway_marks_deterministic_backend_without_model_identity(tmp_path: Path) -> None:
    def factory(root: Path, session_id: str) -> SessionService:
        return SessionService.local_default(
            root, session_id=session_id, backend=DeterministicAgentBackend(), env={}
        )

    gateway = SessionGateway(project=ProjectContext(tmp_path), service_factory=factory, env={})
    try:
        session_id = gateway.create_session("Synthetic observation")["session_id"]
        observation = gateway.evaluation_observation(session_id=session_id)
        assert observation.source == "deterministic"
        assert observation.request_model is None
        assert observation.openhands_version is None
        assert observation.model_options_fingerprint is None
        assert observation.platform == "generic"
        assert observation == gateway.evaluation_observation(session_id=session_id)
    finally:
        gateway.stop()
