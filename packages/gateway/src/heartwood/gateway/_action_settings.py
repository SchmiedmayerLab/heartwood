# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Action-confirmation settings shared by the gateway, CLI, and web UI."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import cast

from heartwood.schemas import ActionConfirmationMode

ACTION_MODE_SCOPE_DESCRIPTION = (
    "Shared by every Heartwood interface in this project and applied to future action sets."
)


class ActionSettingsError(ValueError):
    """Raised when action-confirmation settings are malformed or disallowed."""


@dataclass(frozen=True, slots=True)
class ActionModeOption:
    """One stable OpenHands confirmation mode and its researcher-facing label."""

    mode: ActionConfirmationMode
    command_value: str
    label: str
    description: str
    automatic_risks: tuple[str, ...]
    reviewed_risks: tuple[str, ...]
    recommended: bool = False

    def safe_dict(self) -> dict[str, object]:
        """Return serializable non-secret metadata."""
        return asdict(self)


ACTION_MODE_OPTIONS: tuple[ActionModeOption, ...] = (
    ActionModeOption(
        mode="always-confirm",
        command_value="ask-every-time",
        label="Review Every Action",
        description=(
            "Heartwood pauses before every proposed action set so you can inspect it "
            "before anything runs."
        ),
        automatic_risks=(),
        reviewed_risks=("low", "medium", "high", "unknown"),
        recommended=True,
    ),
    ActionModeOption(
        mode="confirm-risky",
        command_value="auto-approve-low-risk",
        label="Low-Risk Automation",
        description=(
            "An action set continues automatically only when every action is low risk. "
            "Any medium-, high-, or unclassified-risk action pauses the complete set for review."
        ),
        automatic_risks=("low",),
        reviewed_risks=("medium", "high", "unknown"),
    ),
)

_CONFIRMATION_MODES = frozenset(option.mode for option in ACTION_MODE_OPTIONS)


@dataclass(frozen=True, slots=True)
class ActionSettings:
    """Versioned selection of the OpenHands action-confirmation mode."""

    schema_version: str = "heartwood.action-settings.v1"
    confirmation_mode: ActionConfirmationMode = "always-confirm"

    def validate(self) -> None:
        """Validate the settings schema and selected mode."""
        if self.schema_version != "heartwood.action-settings.v1":
            msg = f"unsupported action settings schema: {self.schema_version}"
            raise ActionSettingsError(msg)
        if self.confirmation_mode not in _CONFIRMATION_MODES:
            msg = f"unsupported action confirmation mode: {self.confirmation_mode}"
            raise ActionSettingsError(msg)

    def selecting(self, mode: str) -> ActionSettings:
        """Return settings with a validated mode selection."""
        if mode not in _CONFIRMATION_MODES:
            msg = f"unsupported action confirmation mode: {mode}"
            raise ActionSettingsError(msg)
        updated = replace(self, confirmation_mode=mode)
        updated.validate()
        return updated

    def safe_dict(self) -> dict[str, object]:
        """Return serializable non-secret settings."""
        return asdict(self)


def action_settings_from_mapping(value: object) -> ActionSettings:
    """Validate action settings parsed from JSON."""
    if not isinstance(value, dict):
        msg = "action settings must be an object"
        raise ActionSettingsError(msg)
    unknown = sorted(set(value) - {"confirmation_mode", "schema_version"})
    if unknown:
        msg = f"action settings contain unsupported fields: {', '.join(unknown)}"
        raise ActionSettingsError(msg)
    schema_version = value.get("schema_version")
    mode = value.get("confirmation_mode")
    if not isinstance(schema_version, str) or not schema_version:
        msg = "schema_version must be a non-empty string"
        raise ActionSettingsError(msg)
    if not isinstance(mode, str) or not mode:
        msg = "confirmation_mode must be a non-empty string"
        raise ActionSettingsError(msg)
    settings = ActionSettings(
        schema_version=schema_version,
        confirmation_mode=cast(ActionConfirmationMode, mode),
    )
    settings.validate()
    return settings
