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
from heartwood.gateway import ModelProfile, ProjectContext, SessionGateway
from heartwood.notebook import (
    NotebookSession,
    build_view_model,
    build_widget_spec,
    jupyter_proxy_url,
    render_widgets,
)
from heartwood.notebook._widgets import WidgetSpec
from heartwood.session import EventKind, SessionCommand, SessionEvent


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


class _ModelGateway(_CountingGateway):
    def __init__(self) -> None:
        super().__init__()
        self.inspected: tuple[str, str | None] | None = None
        self.downloaded: tuple[str, str | None] | None = None
        self.discovered: tuple[str, bool] | None = None

    def discover_models(
        self,
        connection_id: str,
        *,
        token: str | None = None,
        base_url: str | None = None,
        refresh: bool = False,
    ) -> dict[str, object]:
        assert token is None
        assert base_url is None
        self.discovered = (connection_id, refresh)
        return {"connection_id": connection_id, "models": []}

    def inspect_model_repository(
        self, repository: str, *, revision: str | None = None
    ) -> dict[str, object]:
        self.inspected = (repository, revision)
        return {"model": {"source_repository": repository}, "selection_reason": "automatic"}

    def download_custom_local_model(
        self, repository: str, *, revision: str | None = None
    ) -> dict[str, object]:
        self.downloaded = (repository, revision)
        return {"model_id": "hf-model", "status": "downloading"}


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


def test_notebook_session_adopts_and_validates_an_injected_gateway_project(
    tmp_path: Path,
) -> None:
    gateway_root = tmp_path / "gateway-project"
    other_root = tmp_path / "other-project"
    gateway_root.mkdir()
    other_root.mkdir()
    gateway = SessionGateway(
        project=ProjectContext(gateway_root),
        env={},
        backend_id="deterministic",
    )

    adopted = NotebookSession(gateway=gateway)

    assert adopted.project.root == gateway_root
    with pytest.raises(ValueError, match="must match the injected gateway project"):
        NotebookSession(project=ProjectContext(other_root), gateway=gateway)


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


def test_notebook_groups_every_pending_member_under_one_action_set() -> None:
    events = tuple(
        SessionEvent(
            event_id=f"event-{index}",
            session_id="notebook-batch",
            sequence=index,
            kind=EventKind.CONFIRMATION_REQUESTED,
            occurred_at="2026-07-13T00:00:00Z",
            payload={
                "request": {
                    "request_id": f"request-{index}",
                    "tool_call_id": f"tool-{index}",
                    "tool_name": tool_name,
                    "risk": risk,
                    "summary": summary,
                    "arguments": (
                        {"command": "python run.py --output cohort-summary.json"}
                        if tool_name == "terminal"
                        else {}
                    ),
                }
            },
        )
        for index, (tool_name, risk, summary) in enumerate(
            (
                ("terminal", "medium", "Run the synthetic cohort command"),
                ("file_editor", "unknown", "Write the aggregate result"),
            ),
            1,
        )
    )

    view_model = build_view_model(events)
    pending = view_model.approval_controls
    approval_items = next(
        section.items for section in build_widget_spec(view_model) if section.title == "Approvals"
    )

    assert len(pending) == 1
    assert pending[0].target_id == "tool-1"
    assert [action.target_id for action in pending[0].actions] == ["tool-1", "tool-2"]
    assert approval_items == (
        "Review complete action set (2 actions): pending",
        (
            "1. Run the synthetic cohort command (terminal, medium risk)\n"
            "Arguments:\n{\n"
            '  "command": "python run.py --output cohort-summary.json"\n'
            "}"
        ),
        "2. Write the aggregate result (file_editor, unknown risk)",
    )


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


def test_notebook_reuses_gateway_model_inspection_and_download_contract(tmp_path: Path) -> None:
    gateway = _ModelGateway()
    session = NotebookSession(
        project=ProjectContext(tmp_path),
        session_id="notebook-model-download",
        gateway=cast(SessionGateway, gateway),
    )

    plan = session.inspect_model_repository("example/model", revision="main")
    download = session.download_custom_local_model("example/model", revision="1" * 40)
    discovered = session.discover_models("local", refresh=True)

    assert gateway.inspected == ("example/model", "main")
    assert gateway.downloaded == ("example/model", "1" * 40)
    assert gateway.discovered == ("local", True)
    assert discovered["connection_id"] == "local"
    assert cast(dict[str, object], plan["model"])["source_repository"] == "example/model"
    assert download["status"] == "downloading"


def test_notebook_observes_shared_project_setup_and_action_settings(tmp_path: Path) -> None:
    project = ProjectContext(tmp_path)
    session = NotebookSession(
        project=project,
        session_id="notebook-shared-state",
        gateway=SessionGateway(project=project, env={}, backend_id="deterministic"),
    )

    configured = session.configure_model_source("local")
    action_settings = session.select_action_confirmation_mode("confirm-risky")

    reopened = NotebookSession(
        project=project,
        session_id="notebook-shared-state",
        gateway=SessionGateway(project=project, env={}, backend_id="deterministic"),
    )
    assert configured["model_source"] == "local"
    assert action_settings["confirmation_mode"] == "confirm-risky"
    assert reopened.model_settings()["model_source"] == "local"
    assert reopened.action_settings()["confirmation_mode"] == "confirm-risky"
    assert reopened.project_readiness()["project_root"] == str(tmp_path)


def test_jupyter_proxy_url_uses_service_prefix() -> None:
    assert (
        jupyter_proxy_url(
            port=8767,
            env={"JUPYTERHUB_SERVICE_PREFIX": "/user/synthetic/"},
        )
        == "/user/synthetic/proxy/8767/"
    )


def test_jupyter_proxy_url_uses_terra_leonardo_route() -> None:
    assert (
        jupyter_proxy_url(
            port=8767,
            env={
                "GOOGLE_PROJECT": "terra-project",
                "CLUSTER_NAME": "saturn-runtime",
            },
        )
        == "/proxy/terra-project/saturn-runtime/jupyter/proxy/8767/"
    )


def test_jupyter_proxy_url_requires_complete_terra_route() -> None:
    assert (
        jupyter_proxy_url(
            port=8767,
            env={"GOOGLE_PROJECT": "terra-project"},
        )
        == "/proxy/8767/"
    )


def test_notebook_session_tracks_command_sequence_without_duplicate_replay(
    tmp_path: Path,
) -> None:
    gateway = _CountingGateway()
    session = NotebookSession(
        project=ProjectContext(tmp_path),
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
    session = NotebookSession(project=ProjectContext(tmp_path), session_id="notebook-lifecycle")

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
    session = NotebookSession(project=ProjectContext(tmp_path), session_id="notebook-fallback")
    view_model = session.detect()

    monkeypatch.setattr("heartwood.notebook._widgets._load_widgets", lambda: None)

    rendered = render_widgets(view_model)

    assert isinstance(rendered, tuple)
    assert all(isinstance(item, WidgetSpec) for item in rendered)


def _deterministic_session(workspace: Path, session_id: str) -> NotebookSession:
    workspace.mkdir(parents=True, exist_ok=True)
    project = ProjectContext(workspace)
    gateway = SessionGateway(
        project=project,
        env={},
        backend_id="deterministic",
    )
    return NotebookSession(project=project, session_id=session_id, gateway=gateway)
