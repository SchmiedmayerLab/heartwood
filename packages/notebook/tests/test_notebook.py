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
from heartwood.gateway import SessionGateway
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
    session = NotebookSession(workspace=tmp_path, session_id="notebook-session")

    detected = session.detect()
    approved = session.approve(
        target_type="model-call",
        target_id="decision-synthetic-model-call",
    )
    run = session.run(endpoint="https://model.local.invalid/v1/chat/completions")
    exported = session.audit_export()

    assert detected.dataset_proposals[0].dataset_type == "omop-cdm"
    assert approved.approval_controls[-1].decision == "approved"
    assert run.policy_status[-1].decision == "allow"
    assert run.skill_proposals[-1].status == "proposed"
    assert any(control.target_type == "model-call" for control in run.approval_controls)
    assert exported.export_actions[-1].path.endswith("audit-export.jsonl")
    assert exported.event_count == len(session.gateway.replay_events(session_id="notebook-session"))


def test_notebook_session_projects_provider_route_metadata(tmp_path: Path) -> None:
    provider_config = tmp_path / "provider-routes.toml"
    provider_config.write_text(
        "\n".join(
            (
                'schema_version = "heartwood.provider-config.v1"',
                "",
                "[[routes]]",
                'route_id = "local-loopback"',
                'provider = "openai-compatible"',
                'endpoint = "http://127.0.0.1:8765/v1/chat/completions"',
                'model = "heartwood-local-runtime"',
                'capability_tier = "supervised"',
                'auth = "none"',
                "",
            )
        ),
        encoding="utf-8",
    )
    session = NotebookSession(workspace=tmp_path / "sessions", session_id="notebook-provider")

    view_model = session.run(
        provider_config_path=provider_config,
        provider_route_id="local-loopback",
    )

    assert view_model.policy_status[-1].route_id == "local-loopback"
    assert view_model.policy_status[-1].provider == "openai-compatible"


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
    session = NotebookSession(workspace=tmp_path, session_id="notebook-widgets")
    session.detect()
    session.run(endpoint="https://public.example.invalid/v1/chat/completions")
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
