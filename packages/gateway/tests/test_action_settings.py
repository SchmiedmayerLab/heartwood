# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

from pathlib import Path

import pytest

from heartwood.gateway import (
    ActionSettingsError,
    ProjectConfig,
    ProjectConfigStore,
    ProjectContext,
    SessionGateway,
    action_settings_from_mapping,
)
from heartwood.schemas import PolicyProfile
from heartwood.session import CommandKind, EventKind, SessionCommand


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ([], "must be an object"),
        ({"confirmation_mode": "always-confirm"}, "schema_version"),
        (
            {
                "schema_version": "heartwood.action-settings.v1",
                "confirmation_mode": "always-confirm",
                "unexpected": True,
            },
            "unsupported fields",
        ),
        (
            {
                "schema_version": "heartwood.action-settings.v1",
                "confirmation_mode": "never-confirm",
            },
            "unsupported action confirmation mode",
        ),
    ],
)
def test_action_settings_reject_malformed_values(value: object, message: str) -> None:
    with pytest.raises(ActionSettingsError, match=message):
        action_settings_from_mapping(value)


def test_gateway_exposes_only_the_two_supported_modes_and_persists_selection(
    tmp_path: Path,
) -> None:
    gateway = SessionGateway(project=ProjectContext(tmp_path), env={})

    initial = gateway.action_settings()
    selected = gateway.select_action_confirmation_mode("confirm-risky")

    assert initial["confirmation_mode"] == "always-confirm"
    modes = initial["modes"]
    assert isinstance(modes, list)
    assert all(isinstance(item, dict) for item in modes)
    assert [item["mode"] for item in modes] == [
        "always-confirm",
        "confirm-risky",
    ]
    scope_description = initial["scope_description"]
    assert isinstance(scope_description, str)
    assert scope_description.startswith("Shared by every Heartwood interface")
    assert modes[0] == {
        "allowed": True,
        "automatic_risks": [],
        "command_value": "ask-every-time",
        "description": (
            "Heartwood pauses before every proposed action set so you can inspect it "
            "before anything runs."
        ),
        "label": "Review Every Action",
        "mode": "always-confirm",
        "recommended": True,
        "reviewed_risks": ["low", "medium", "high", "unknown"],
        "unavailable_reason": None,
    }
    assert initial["presentation"] == {
        "other_tool_label_template": "{tool_name} Action",
        "risk_labels": {
            "high": "High Risk",
            "low": "Low Risk",
            "medium": "Medium Risk",
            "unknown": "Not Classified",
        },
        "tool_labels": {
            "file_editor": "File Change",
            "terminal": "Terminal Command",
        },
        "unknown_risk_label": "Not Classified",
        "unknown_tool_label": "Tool Action",
    }
    assert selected["confirmation_mode"] == "confirm-risky"
    assert gateway.action_settings()["confirmation_mode"] == "confirm-risky"


def test_gateway_rejects_confirmation_mode_changes_while_work_is_active(
    tmp_path: Path,
) -> None:
    gateway = SessionGateway(
        project=ProjectContext(tmp_path),
        env={},
        backend_id="deterministic",
    )
    gateway.handle(
        SessionCommand(
            command_id="chat",
            session_id="session-main",
            kind=CommandKind.CHAT,
            actor_id="test-user",
            created_at="2026-07-25T00:00:00Z",
            payload={"prompt": "Propose one action"},
        )
    )

    settings = gateway.action_settings()
    with pytest.raises(ActionSettingsError, match="while a session is active"):
        gateway.select_action_confirmation_mode("confirm-risky")

    assert settings["change_allowed"] is False
    assert gateway.action_settings()["confirmation_mode"] == "always-confirm"
    assert "session-main" in gateway._services


def test_gateway_rejects_confirmation_mode_blocked_by_deployment_policy(
    tmp_path: Path,
) -> None:
    project = ProjectContext(tmp_path)
    ProjectConfigStore(
        project,
        ProjectConfig(
            platform_id="generic",
            policy=PolicyProfile(policy_id="managed", platform_id="generic"),
        ),
    ).save(
        ProjectConfig(
            platform_id="generic",
            policy=PolicyProfile(policy_id="managed", platform_id="generic"),
        )
    )
    gateway = SessionGateway(project=project, env={})

    modes = gateway.action_settings()["modes"]
    assert isinstance(modes, list)
    restricted = next(item for item in modes if item["mode"] == "confirm-risky")
    assert restricted["allowed"] is False
    assert restricted["unavailable_reason"] == ("Unavailable under the active platform policy.")

    with pytest.raises(ActionSettingsError, match="not allowed by platform policy"):
        gateway.select_action_confirmation_mode("confirm-risky")


def test_cached_service_reloads_action_mode_changed_by_another_gateway(
    tmp_path: Path,
) -> None:
    project = ProjectContext(tmp_path)
    reader = SessionGateway(project=project, env={}, backend_id="deterministic")
    writer = SessionGateway(project=project, env={}, backend_id="deterministic")
    try:
        writer.select_action_confirmation_mode("confirm-risky")

        first = reader.handle(_chat_command("first"))
        first_kinds = {str(event.kind) for event in first.events}
        assert EventKind.TOOL_EXECUTION_RECORDED.value in first_kinds
        assert EventKind.CONFIRMATION_REQUESTED.value not in first_kinds

        writer.select_action_confirmation_mode("always-confirm")

        second = reader.handle(_chat_command("second"))
        second_kinds = {str(event.kind) for event in second.events}
        assert EventKind.CONFIRMATION_REQUESTED.value in second_kinds
        assert EventKind.TOOL_EXECUTION_RECORDED.value not in second_kinds
    finally:
        reader.stop()
        writer.stop()


def _chat_command(command_id: str) -> SessionCommand:
    return SessionCommand(
        command_id=command_id,
        session_id="shared-session",
        kind=CommandKind.CHAT,
        actor_id="synthetic-user",
        created_at="2026-07-25T00:00:00Z",
        payload={"prompt": "inspect the synthetic project"},
    )
