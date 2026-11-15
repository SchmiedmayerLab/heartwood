# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Action-review settings shared by the gateway, CLI, and web UI."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import cast

from heartwood.core_adapter.action_review import REVIEW_MODES
from heartwood.schemas import ActionReviewMode

ACTION_MODE_SCOPE_DESCRIPTION = (
    "Shared by every Heartwood interface in this project and applied to the next action set."
)
ACTION_MODE_LAUNCH_SCOPE_DESCRIPTION = (
    "Applies until this Heartwood process exits. "
    "A project without a deployment manifest never stores automatic review."
)


class ActionSettingsError(ValueError):
    """Raised when action-review settings are malformed or disallowed."""


@dataclass(frozen=True, slots=True)
class ActionModeOption:
    """One review mode and its researcher-facing wording."""

    mode: ActionReviewMode
    label: str
    description: str
    automatic: tuple[str, ...]

    def safe_dict(self) -> dict[str, object]:
        """Return serializable non-secret metadata."""
        return asdict(self)


ACTION_MODE_OPTIONS: tuple[ActionModeOption, ...] = (
    ActionModeOption(
        mode="review-all",
        label="Review Every Action",
        description="You decide on every action before it runs.",
        automatic=(),
    ),
    ActionModeOption(
        mode="review-changes",
        label="Review Changes",
        description=(
            "Heartwood allows viewing project files and plan updates. "
            "You decide on every command, file change, and anything else."
        ),
        automatic=("read.file-view", "plan.task-tracker"),
    ),
    ActionModeOption(
        mode="review-outside-effects",
        label="Review Outside Effects",
        description=(
            "Commands and file changes that stay inside the isolated worker run without asking. "
            "You decide on anything that leaves it."
        ),
        automatic=("sandbox.contained",),
    ),
)


@dataclass(frozen=True, slots=True)
class ActionSettings:
    """Versioned researcher selection; None follows the default for the execution boundary."""

    schema_version: str = "heartwood.action-settings.v1"
    review_mode: ActionReviewMode | None = None

    def validate(self) -> None:
        """Validate the settings schema and selected mode."""
        if self.schema_version != "heartwood.action-settings.v1":
            msg = f"unsupported action settings schema: {self.schema_version}"
            raise ActionSettingsError(msg)
        if self.review_mode is not None and self.review_mode not in REVIEW_MODES:
            msg = f"unsupported action review mode: {self.review_mode}"
            raise ActionSettingsError(msg)

    def selecting(self, mode: str) -> ActionSettings:
        """Return settings with a validated mode selection."""
        updated = replace(self, review_mode=cast(ActionReviewMode, mode))
        updated.validate()
        return updated

    def safe_dict(self) -> dict[str, object]:
        """Return serializable non-secret settings."""
        return {key: value for key, value in asdict(self).items() if value is not None}


class LaunchActionSettingsStore:
    """Hold the selection in gateway memory when project state is in the research directory."""

    def __init__(self) -> None:
        self._settings = ActionSettings()

    def load(self) -> ActionSettings:
        """Return the selection made during this launch."""
        return self._settings

    def save(self, settings: ActionSettings) -> None:
        """Replace the selection for the rest of this launch."""
        settings.validate()
        self._settings = settings


def action_settings_from_mapping(value: object) -> ActionSettings:
    """Validate action settings parsed from project configuration."""
    if not isinstance(value, dict):
        msg = "action settings must be an object"
        raise ActionSettingsError(msg)
    unknown = sorted(set(value) - {"review_mode", "schema_version"})
    if unknown:
        msg = f"action settings contain unsupported fields: {', '.join(unknown)}"
        raise ActionSettingsError(msg)
    schema_version = value.get("schema_version")
    mode = value.get("review_mode")
    if not isinstance(schema_version, str) or not schema_version:
        msg = "schema_version must be a non-empty string"
        raise ActionSettingsError(msg)
    if mode is not None and not isinstance(mode, str):
        msg = "review_mode must be a string"
        raise ActionSettingsError(msg)
    settings = ActionSettings(
        schema_version=schema_version,
        review_mode=cast(ActionReviewMode | None, mode),
    )
    settings.validate()
    return settings
