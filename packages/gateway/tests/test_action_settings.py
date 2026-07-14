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
    assert selected["confirmation_mode"] == "confirm-risky"
    assert gateway.action_settings()["confirmation_mode"] == "confirm-risky"


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

    with pytest.raises(ActionSettingsError, match="not allowed by platform policy"):
        gateway.select_action_confirmation_mode("confirm-risky")
