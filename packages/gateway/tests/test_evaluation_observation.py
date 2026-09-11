# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Evaluation identity comes from the owned runtime, not a caller's labels."""

from importlib.metadata import version
from pathlib import Path

import pytest
from openhands.sdk import LLM
from openhands.sdk.security import ConfirmRisky
from pydantic import SecretStr

from heartwood.core_adapter import DeterministicAgentBackend, SessionService
from heartwood.gateway import ModelProfile, OpenHandsSdkBackend, ProjectContext, SessionGateway


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
