# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import json
from pathlib import Path

import pytest

from heartwood.gateway import (
    ActionSettings,
    ActionSettingsError,
    ActionSettingsStore,
    SessionGateway,
    action_settings_from_mapping,
    action_settings_path,
)
from heartwood.schemas import PolicyProfile


def test_action_settings_default_to_confirmation_for_every_action(tmp_path: Path) -> None:
    store = ActionSettingsStore(tmp_path / "actions.json")

    assert store.load().confirmation_mode == "always-confirm"


def test_action_settings_round_trip_risk_based_selection(tmp_path: Path) -> None:
    path = tmp_path / "actions.json"
    store = ActionSettingsStore(path)
    settings = ActionSettings(confirmation_mode="confirm-risky")

    store.save(settings)

    assert store.load() == settings
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "confirmation_mode": "confirm-risky",
        "schema_version": "heartwood.action-settings.v1",
    }


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


def test_action_settings_path_supports_deployment_override(tmp_path: Path) -> None:
    configured = tmp_path / "deployment-actions.json"

    assert action_settings_path(tmp_path / "sessions", {}) == tmp_path / "actions.json"
    assert (
        action_settings_path(tmp_path / "sessions", {"HEARTWOOD_ACTION_SETTINGS": str(configured)})
        == configured
    )


def test_gateway_exposes_only_the_two_supported_modes_and_persists_selection(
    tmp_path: Path,
) -> None:
    gateway = SessionGateway(workspace=tmp_path / "sessions", env={})

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
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(
        PolicyProfile(policy_id="managed", platform_id="managed").model_dump_json(),
        encoding="utf-8",
    )
    gateway = SessionGateway(
        workspace=tmp_path / "sessions",
        env={"HEARTWOOD_POLICY_PROFILE": str(policy_path)},
    )

    with pytest.raises(ActionSettingsError, match="not allowed by platform policy"):
        gateway.select_action_confirmation_mode("confirm-risky")
