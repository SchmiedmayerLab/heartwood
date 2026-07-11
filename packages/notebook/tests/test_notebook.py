# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Tests for notebook-facing session projections."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from heartwood.core_adapter import SessionResult
from heartwood.gateway import ModelProfile, SessionGateway
from heartwood.notebook import NotebookSession, build_widget_spec, jupyter_proxy_url, render_widgets
from heartwood.notebook._widgets import WidgetSpec
from heartwood.session import SessionCommand, SessionEvent


class _CountingGateway:
    def __init__(self) -> None:
        self.commands: list[SessionCommand] = []
        self.replay_calls = 0

    def replay_events(
        self,
        *,
        session_id: str,
        after_sequence: int | None = None,
    ) -> tuple[SessionEvent, ...]:
        _ = (session_id, after_sequence)
        self.replay_calls += 1
        return ()

    def handle(self, command: SessionCommand) -> SessionResult:
        self.commands.append(command)
        return SessionResult(events=())


def test_notebook_session_observes_gateway_events(tmp_path: Path) -> None:
    session = _deterministic_session(tmp_path, "notebook-session")

    detected = session.detect()
    run = session.run("inspect the synthetic workspace")
    pending = run.approval_controls[-1]
    approved = session.approve(tool_call_id=pending.target_id)
    exported = session.audit_export()

    assert detected.dataset_proposals[0].dataset_type == "omop-cdm"
    assert run.policy_status[-1].decision == "allow"
    assert run.chat[0].role == "user"
    assert run.chat[0].content == "inspect the synthetic workspace"
    assert run.chat[1].role == "assistant"
    assert pending.target_type == "tool-call"
    assert pending.decision is None
    assert approved.approval_controls[-1].decision == "approved"
    assert not any(control.target_type == "model-call" for control in run.approval_controls)
    assert exported.export_actions[-1].path.endswith("audit-export.jsonl")
    assert exported.event_count == len(session.gateway.replay_events(session_id="notebook-session"))


def test_notebook_session_coalesces_approval_controls(tmp_path: Path) -> None:
    session = _deterministic_session(tmp_path, "notebook-approvals")

    run = session.chat("inspect the workspace")

    keys = [(control.target_type, control.target_id) for control in run.approval_controls]
    assert len(keys) == len(set(keys))
    tool_control = next(
        control for control in run.approval_controls if control.target_type == "tool-call"
    )
    assert tool_control.label.startswith("Review")

    approved = session.deny(tool_call_id=tool_control.target_id)
    matching = [
        control
        for control in approved.approval_controls
        if (
            control.target_type == tool_control.target_type
            and control.target_id == tool_control.target_id
        )
    ]

    assert len(matching) == 1
    assert matching[0].decision == "denied"


def test_notebook_session_configures_non_secret_model_profiles(tmp_path: Path) -> None:
    session = _deterministic_session(tmp_path / "sessions", "notebook-models")
    profile = ModelProfile(
        profile_id="local",
        model="openai/local-model",
        base_url="http://127.0.0.1:8765/v1",
        policy_endpoint="http://127.0.0.1:8765/v1/chat/completions",
        credential_kind="none",
    )

    session.save_model_profile(profile)
    settings = session.select_model_profile("local")
    validation = session.validate_model_profile()
    artifacts = session.model_artifacts()
    policy_decision = cast(dict[str, object], validation["policy_decision"])
    artifact_items = cast(list[object], artifacts["artifacts"])
    artifact_ids = {cast(dict[str, object], item)["artifact_id"] for item in artifact_items}

    assert settings["active_profile"] == "local"
    assert session.model_settings()["active_profile"] == "local"
    assert validation["credential_status"] == "configured"
    assert policy_decision["decision"] == "allow"
    assert {
        "llama-cpp-stories260k-ci",
        "qwen25-7b-instruct-q4_k_m",
    }.issubset(artifact_ids)


def test_jupyter_proxy_url_uses_service_prefix() -> None:
    assert (
        jupyter_proxy_url(
            port=8767,
            env={"JUPYTERHUB_SERVICE_PREFIX": "/user/synthetic/"},
        )
        == "/user/synthetic/proxy/8767/"
    )


def test_notebook_session_tracks_command_sequence_without_duplicate_replay(
    tmp_path: Path,
) -> None:
    gateway = _CountingGateway()
    session = NotebookSession(
        workspace=tmp_path,
        session_id="notebook-counting",
        gateway=cast(SessionGateway, gateway),
    )

    assert gateway.replay_calls == 1
    session.chat("summarize")
    session.pause()

    assert gateway.replay_calls == 3
    assert [command.command_id for command in gateway.commands] == [
        "notebook-counting-chat-000000",
        "notebook-counting-pause-000001",
    ]


def test_notebook_pause_resume_updates_view_state(tmp_path: Path) -> None:
    session = NotebookSession(workspace=tmp_path, session_id="notebook-lifecycle")

    paused = session.pause()
    resumed = session.resume()

    assert paused.paused is True
    assert resumed.paused is False


def test_widget_spec_covers_expected_sections(tmp_path: Path) -> None:
    session = _deterministic_session(tmp_path, "notebook-widgets")
    session.detect()
    session.run()
    view_model = session.audit_export()

    sections = build_widget_spec(view_model)
    rendered = render_widgets(view_model)

    assert [section.title for section in sections] == [
        "Chat",
        "Activity",
        "Datasets",
        "Skills",
        "Approvals",
        "Policy",
        "Exports",
    ]
    assert sections[2].items == ("omop-cdm (0.95)",)
    assert isinstance(rendered, object)


def test_widget_rendering_falls_back_without_ipywidgets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = NotebookSession(workspace=tmp_path, session_id="notebook-fallback")
    view_model = session.detect()

    monkeypatch.setattr("heartwood.notebook._widgets._load_widgets", lambda: None)

    rendered = render_widgets(view_model)

    assert isinstance(rendered, tuple)
    assert all(isinstance(item, WidgetSpec) for item in rendered)


def _deterministic_session(workspace: Path, session_id: str) -> NotebookSession:
    gateway = SessionGateway(
        workspace=workspace,
        env={"HEARTWOOD_AGENT_BACKEND": "deterministic"},
    )
    return NotebookSession(workspace=workspace, session_id=session_id, gateway=gateway)
