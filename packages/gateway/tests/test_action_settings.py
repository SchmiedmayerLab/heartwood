# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from heartwood.core_adapter.action_review import REVIEW_MODES
from heartwood.gateway import (
    ACTION_MODE_OPTIONS,
    ActionSettingsError,
    ProjectConfigError,
    ProjectContext,
    SessionGateway,
    action_settings_from_mapping,
    action_state_label,
    display_safe_text,
)
from heartwood.gateway.worker_sessions import WorkerSessionPlan, WorkerSessionRequest
from heartwood.schemas import ActionReviewMode, PolicyProfile
from heartwood.session import CommandKind, EventKind, SessionCommand


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ([], "must be an object"),
        ({"review_mode": "review-all"}, "schema_version"),
        (
            {
                "schema_version": "heartwood.action-settings.v1",
                "review_mode": "review-all",
                "unexpected": True,
            },
            "unsupported fields",
        ),
        (
            {
                "schema_version": "heartwood.action-settings.v1",
                "review_mode": "never-confirm",
            },
            "unsupported action review mode",
        ),
    ],
)
def test_action_settings_reject_malformed_values(value: object, message: str) -> None:
    with pytest.raises(ActionSettingsError, match=message):
        action_settings_from_mapping(value)


def test_shared_action_presentation_handles_unknown_states_and_control_text() -> None:
    assert action_state_label("future-state") == "Future State"
    assert action_state_label("future-\u202estate") == r"Future \u202eState"
    assert display_safe_text("safe\nline", preserve_newlines=True) == "safe\nline"
    assert display_safe_text("unsafe\x1b\u202e") == r"unsafe\x1b\u202e"
    assert display_safe_text("\U000e0001") == r"\U000e0001"


def _policy(*modes: ActionReviewMode) -> PolicyProfile:
    return PolicyProfile(
        policy_id="deployment",
        platform_id="generic",
        allowed_action_review_modes=modes or REVIEW_MODES,
    )


def _unused_worker_plan(_request: WorkerSessionRequest) -> WorkerSessionPlan:
    raise AssertionError("these tests never launch a worker")


def _managed_gateway(
    tmp_path: Path,
    *,
    ceiling: PolicyProfile | None = None,
    workers: bool = False,
    backend_id: str = "auto",
) -> SessionGateway:
    """Build a manifest-style gateway whose state is outside the research directory."""
    root = tmp_path / "project"
    root.mkdir(exist_ok=True)
    identity = uuid4()
    project = ProjectContext(
        root, protected_state=tmp_path / "protected" / str(identity), project_id=identity
    )
    return SessionGateway(
        project=project,
        env={},
        deployment_policy=ceiling or _policy(),
        worker_plan_factory=_unused_worker_plan if workers else None,
        backend_id=backend_id,
    )


def _modes(gateway: SessionGateway) -> dict[str, bool]:
    return {item["mode"]: item["allowed"] for item in gateway.action_settings()["modes"]}


def _chat(session_id: str = "session-main", command_id: str = "chat") -> SessionCommand:
    return SessionCommand(
        command_id=command_id,
        session_id=session_id,
        kind=CommandKind.CHAT,
        actor_id="synthetic-user",
        created_at="2026-07-25T00:00:00Z",
        payload={"prompt": "inspect the synthetic project"},
    )


def test_every_interface_option_is_a_review_mode_in_strictness_order() -> None:
    assert tuple(option.mode for option in ACTION_MODE_OPTIONS) == REVIEW_MODES
    assert ACTION_MODE_OPTIONS[0].automatic == ()


def test_a_local_launch_reviews_everything_and_offers_only_review_changes(
    tmp_path: Path,
) -> None:
    gateway = SessionGateway(project=ProjectContext(tmp_path), env={})

    settings = gateway.action_settings()

    assert settings["review_mode"] == settings["default_mode"] == "review-all"
    assert settings["scope"] == "launch"
    assert _modes(gateway) == {
        "review-all": True,
        "review-changes": True,
        "review-outside-effects": False,
    }
    with pytest.raises(ActionSettingsError):
        gateway.select_action_review_mode("review-outside-effects")
    with pytest.raises(ActionSettingsError):
        gateway.select_action_review_mode("confirm-risky")
    assert gateway.action_settings()["review_mode"] == "review-all"
    assert gateway.select_action_review_mode("review-changes")["review_mode"] == "review-changes"


def test_a_local_selection_is_never_written_to_or_read_from_the_research_directory(
    tmp_path: Path,
) -> None:
    project = ProjectContext(tmp_path)
    gateway = SessionGateway(project=project, env={})
    gateway.select_action_review_mode("review-changes")
    gateway.config_store.save(gateway.config_store.load())
    assert "review-changes" not in project.config_path.read_text().split("[policy]")[0]

    planted = project.config_path.read_text().replace(
        "[action]\n", '[action]\nreview_mode = "review-changes"\n'
    )
    assert planted != project.config_path.read_text()
    project.config_path.write_text(planted)
    reopened = SessionGateway(project=ProjectContext(tmp_path), env={}, backend_id="deterministic")
    try:
        assert reopened.action_settings()["review_mode"] == "review-all"
        service = reopened._service("session-main")
        assert service.action_review_mode() == "review-all"
    finally:
        reopened.stop()


def test_a_project_file_cannot_grant_sandbox_review_to_a_local_launch(tmp_path: Path) -> None:
    project = ProjectContext(tmp_path)
    gateway = SessionGateway(project=project, env={})
    config = gateway.config_store.load()
    gateway.config_store.save(
        replace(
            config,
            policy=config.policy.model_copy(update={"allowed_action_review_modes": REVIEW_MODES}),
        )
    )
    planted = project.config_path.read_text().replace(
        "[action]\n", '[action]\nreview_mode = "review-outside-effects"\n'
    )
    project.config_path.write_text(planted)

    reopened = SessionGateway(project=ProjectContext(tmp_path), env={}, backend_id="deterministic")
    try:
        assert reopened.action_settings()["review_mode"] == "review-all"
        assert _modes(reopened)["review-outside-effects"] is False
        with pytest.raises(ActionSettingsError):
            reopened.select_action_review_mode("review-outside-effects")
        assert reopened._service("session-main").action_review_mode() == "review-all"
    finally:
        reopened.stop()


def test_sandboxed_sessions_default_to_reviewing_outside_effects(tmp_path: Path) -> None:
    gateway = _managed_gateway(tmp_path, workers=True)

    settings = gateway.action_settings()

    assert settings["review_mode"] == settings["default_mode"] == "review-outside-effects"
    assert settings["scope"] == "project"
    assert all(_modes(gateway).values())
    assert gateway.select_action_review_mode("review-all")["review_mode"] == "review-all"
    assert gateway.config_store.load().action_settings.review_mode == "review-all"


def test_a_managed_launch_without_workers_cannot_select_sandbox_review(tmp_path: Path) -> None:
    gateway = _managed_gateway(tmp_path)

    assert gateway.action_settings()["review_mode"] == "review-all"
    assert _modes(gateway)["review-outside-effects"] is False
    with pytest.raises(ActionSettingsError):
        gateway.select_action_review_mode("review-outside-effects")
    gateway.select_action_review_mode("review-changes")
    assert gateway.config_store.load().action_settings.review_mode == "review-changes"


@pytest.mark.parametrize(
    ("ceiling", "default", "refused"),
    [
        (("review-all",), "review-all", ("review-changes", "review-outside-effects")),
        (("review-all", "review-changes"), "review-all", ("review-outside-effects",)),
    ],
)
def test_the_deployment_ceiling_limits_the_default_and_every_selection(
    tmp_path: Path,
    ceiling: tuple[ActionReviewMode, ...],
    default: str,
    refused: tuple[str, ...],
) -> None:
    gateway = _managed_gateway(tmp_path, ceiling=_policy(*ceiling), workers=True)

    assert gateway.action_settings()["review_mode"] == default
    for mode in refused:
        assert _modes(gateway)[mode] is False
        with pytest.raises(ActionSettingsError):
            gateway.select_action_review_mode(mode)
    assert gateway.action_settings()["review_mode"] == default


def test_a_project_cannot_store_a_policy_or_selection_looser_than_the_ceiling(
    tmp_path: Path,
) -> None:
    gateway = _managed_gateway(tmp_path, ceiling=_policy("review-all"))
    config = gateway.config_store.load()
    looser = config.policy.model_copy(update={"allowed_action_review_modes": REVIEW_MODES})

    with pytest.raises(ProjectConfigError):
        gateway.config_store.save(replace(config, policy=looser))
    with pytest.raises(ProjectConfigError):
        gateway.config_store.save(
            replace(config, action_settings=config.action_settings.selecting("review-changes"))
        )


def test_review_tightens_at_any_time_and_loosens_only_while_idle(tmp_path: Path) -> None:
    gateway = SessionGateway(project=ProjectContext(tmp_path), env={}, backend_id="deterministic")
    try:
        gateway.select_action_review_mode("review-changes")
        result = gateway.handle(_chat())
        assert EventKind.CONFIRMATION_REQUESTED in {event.kind for event in result.events}
        service = gateway._services["session-main"]

        settings = gateway.action_settings()
        assert settings["change_allowed"] is False
        tightened = gateway.select_action_review_mode("review-all")
        assert tightened["review_mode"] == "review-all"
        assert service.action_review_mode() == "review-all"
        with pytest.raises(ActionSettingsError):
            gateway.select_action_review_mode("review-changes")

        assert gateway.action_settings()["review_mode"] == "review-all"
        assert gateway._services["session-main"] is service
    finally:
        gateway.stop()


def test_an_unclassified_action_waits_for_the_researcher_in_every_selectable_mode(
    tmp_path: Path,
) -> None:
    gateway = SessionGateway(project=ProjectContext(tmp_path), env={}, backend_id="deterministic")
    try:
        gateway.select_action_review_mode("review-changes")
        result = gateway.handle(_chat())
        kinds = {event.kind for event in result.events}
        assert EventKind.CONFIRMATION_REQUESTED in kinds
        assert EventKind.TOOL_EXECUTION_RECORDED not in kinds
        projection = gateway.session_projection(session_id="session-main")
        assert projection.pending_approval is not None
        assert all(action.automatic_rule is None for action in projection.pending_approval.actions)
    finally:
        gateway.stop()


def test_the_manifest_ceiling_replaces_any_list_kept_in_project_state(tmp_path: Path) -> None:
    """A migrated or edited project policy neither narrows nor widens the operator's ceiling."""
    gateway = _managed_gateway(tmp_path, workers=True)
    project = gateway.project
    gateway.config_store.save(gateway.config_store.load())
    stored = project.config_path.read_text()
    listed = '"review-all",\n    "review-changes",\n    "review-outside-effects",\n'
    assert listed in stored

    project.config_path.write_text(stored.replace(listed, '"review-all",\n'))
    migrated = SessionGateway(
        project=project,
        env={},
        deployment_policy=_policy(),
        worker_plan_factory=_unused_worker_plan,
    )
    assert all(_modes(migrated).values())
    assert migrated.action_settings()["review_mode"] == "review-outside-effects"

    project.config_path.write_text(stored)
    narrowed = SessionGateway(
        project=project,
        env={},
        deployment_policy=_policy("review-all"),
        worker_plan_factory=_unused_worker_plan,
    )
    assert _modes(narrowed) == {
        "review-all": True,
        "review-changes": False,
        "review-outside-effects": False,
    }
    assert narrowed.action_settings()["review_mode"] == "review-all"


def test_a_stored_selection_the_manifest_no_longer_permits_stops_the_project(
    tmp_path: Path,
) -> None:
    gateway = _managed_gateway(tmp_path)
    gateway.select_action_review_mode("review-changes")

    with pytest.raises(ProjectConfigError):
        SessionGateway(
            project=gateway.project, env={}, deployment_policy=_policy("review-all")
        ).config_store.load()


def test_a_launch_selection_obeys_every_limit_and_leaves_the_project_untouched(
    tmp_path: Path,
) -> None:
    project = ProjectContext(tmp_path)
    gateway = SessionGateway(project=project, env={}, backend_id="deterministic")
    try:
        assert gateway.select_action_review_mode("review-changes")["review_mode"] == (
            "review-changes"
        )
        assert not project.state_root.exists()
        assert gateway._service("session-main").action_review_mode() == "review-changes"
        with pytest.raises(ActionSettingsError):
            gateway.select_action_review_mode("review-outside-effects")
        assert gateway.action_settings()["review_mode"] == "review-changes"
    finally:
        gateway.stop()
