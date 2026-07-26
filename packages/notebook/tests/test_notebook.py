# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Tests for notebook-facing session projections."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from heartwood.adapters.platform import GenericPlatformAdapter
from heartwood.core_adapter import (
    BackendLifecycle,
    BackendLifecycleEvent,
    SessionResult,
)
from heartwood.gateway import (
    CredentialStore,
    ModelCatalogService,
    ModelProfile,
    ProjectContext,
    ProjectionApprovalAction,
    ProjectionApprovalGroup,
    ProjectionLifecycleState,
    ProjectionMessage,
    ProjectionSubagent,
    ProjectionTask,
    ProjectionUsage,
    ProviderModel,
    RestGateway,
    RestRequest,
    SessionGateway,
    SessionLifecycle,
    SessionProjection,
)
from heartwood.notebook import (
    NotebookSession,
    build_view_model,
    build_widget_spec,
    render_widgets,
)
from heartwood.notebook._widgets import WidgetSpec
from heartwood.session import SessionCommand


class _CountingGateway:
    def __init__(self) -> None:
        self.commands: list[SessionCommand] = []
        self.projection_calls = 0
        self.stopped = False
        self.projection = SessionProjection(
            session_id="notebook-counting",
            event_count=0,
            revision=-1,
        )

    def session_projection(self, *, session_id: str) -> SessionProjection:
        self.projection_calls += 1
        return self.projection.model_copy(update={"session_id": session_id})

    def handle(self, command: SessionCommand) -> SessionResult:
        self.commands.append(command)
        return SessionResult(events=())

    def stop(self) -> None:
        self.stopped = True


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
        remember: bool = False,
    ) -> dict[str, object]:
        assert token is None
        assert base_url is None
        assert remember is False
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

    turn = session.chat("inspect the synthetic workspace")
    pending = turn.pending_approval
    assert pending is not None
    approved = session.approve(group_id=pending.group_id)
    exported = session.audit_export()

    assert turn.context.model_decision == "allow"
    assert turn.conversation[0].role == "user"
    assert turn.conversation[0].content == "inspect the synthetic workspace"
    assert any(message.role == "agent" for message in turn.conversation)
    assert pending.decision_scope == "all"
    assert pending.decision is None
    assert approved.pending_approval is None
    assert exported["filename"] == "notebook-session-audit.jsonl"
    assert '"event_type":"audit.export.recorded"' in str(exported["content"])


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


def test_notebook_session_uses_one_atomic_approval_group(tmp_path: Path) -> None:
    session = _deterministic_session(tmp_path, "notebook-approvals")

    run = session.chat("inspect the workspace")
    approval = run.pending_approval
    assert approval is not None

    denied = session.deny(group_id=approval.group_id)

    assert approval.decision_scope == "all"
    assert denied.pending_approval is None


def test_notebook_groups_every_pending_member_under_one_action_set() -> None:
    projection = SessionProjection(
        session_id="notebook-batch",
        event_count=4,
        revision=3,
        pending_approval=ProjectionApprovalGroup(
            group_id="action-set-synthetic",
            actions=(
                ProjectionApprovalAction(
                    target_id="tool-1",
                    tool_name="terminal",
                    risk="medium",
                    summary="Run the synthetic cohort command",
                    arguments={"command": "python run.py --output cohort-summary.json"},
                ),
                ProjectionApprovalAction(
                    target_id="tool-2",
                    tool_name="file_editor",
                    risk="unknown",
                    summary="Write the aggregate result",
                ),
            ),
        ),
    )

    view_model = build_view_model(projection)
    pending = view_model.pending_approval
    assert pending is not None
    approval_items = next(
        section.items for section in build_widget_spec(view_model) if section.title == "Approval"
    )

    assert pending.group_id == "action-set-synthetic"
    assert [action.target_id for action in pending.actions] == ["tool-1", "tool-2"]
    assert approval_items == (
        "Review action set action-set-synthetic (2 actions): pending",
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
        profile_id="custom-loopback",
        model="openai/custom-model",
        base_url="http://127.0.0.1:8765/v1",
        policy_endpoint="http://127.0.0.1:8765/v1/chat/completions",
        credential_kind="none",
    )

    session.save_model_profile(profile)
    settings = session.select_model_profile("custom-loopback")
    validation = session.validate_model_profile()
    artifacts = session.model_artifacts()
    policy_decision = cast(dict[str, object], validation["policy_decision"])
    artifact_items = cast(list[object], artifacts["artifacts"])
    artifact_ids = {cast(dict[str, object], item)["artifact_id"] for item in artifact_items}

    assert settings["active_profile"] == "custom-loopback"
    assert session.model_settings()["active_profile"] == "custom-loopback"
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
    discovered = session.discover_models("heartwood", refresh=True)

    assert gateway.inspected == ("example/model", "main")
    assert gateway.downloaded == ("example/model", "1" * 40)
    assert gateway.discovered == ("heartwood", True)
    assert discovered["connection_id"] == "heartwood"
    assert cast(dict[str, object], plan["model"])["source_repository"] == "example/model"
    assert download["status"] == "downloading"


def test_notebook_completes_hosted_model_and_credential_workflow(tmp_path: Path) -> None:
    class FakeKeyring:
        priority = 1.0

        def __init__(self) -> None:
            self.values: dict[tuple[str, str], str] = {}

        def get_password(self, service: str, username: str) -> str | None:
            return self.values.get((service, username))

        def set_password(self, service: str, username: str, password: str) -> None:
            self.values[(service, username)] = password

        def delete_password(self, service: str, username: str) -> None:
            self.values.pop((service, username), None)

    project = ProjectContext(tmp_path)
    keyring = FakeKeyring()
    credential_store = CredentialStore(
        project_root=project.root,
        capabilities=GenericPlatformAdapter().capabilities(),
        env={},
        keyring_backend=keyring,
    )
    catalog = ModelCatalogService(
        openai_lister=lambda _connection, _token: (ProviderModel("gpt-synthetic"),),
        compatibility=lambda _connection, _model: (
            "available",
            "verified",
            32_768,
            True,
        ),
    )
    gateway = SessionGateway(
        project=project,
        env={},
        backend_id="deterministic",
        credential_store=credential_store,
        model_catalog_service=catalog,
    )
    session = NotebookSession(project=project, gateway=gateway)

    session.configure_model_source("openai")
    discovered = session.discover_models(
        "openai",
        token="synthetic-secret",
        refresh=True,
        remember=True,
    )
    connected = session.connect_model("openai", "gpt-synthetic", remember=True)
    credential = cast(list[dict[str, object]], session.credential_settings()["bindings"])

    assert cast(list[object], discovered["models"])
    assert connected["active_profile"] == "openai"
    assert (
        next(item for item in credential if item["binding_id"] == "OPENAI_API_KEY")["source"]
        == "process"
    )
    forgotten = session.forget_credential("openai")
    assert all(
        item["configured"] is False
        for item in cast(list[dict[str, object]], forgotten["bindings"])
        if item["binding_id"] == "OPENAI_API_KEY"
    )


def test_notebook_imports_a_local_model_and_releases_gateway(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    source = tmp_path / "model.gguf"
    source.write_bytes(b"GGUFsynthetic-model")
    gateway = SessionGateway(
        project=ProjectContext(project_root),
        env={},
        backend_id="deterministic",
    )

    with NotebookSession(gateway=gateway) as session:
        session.configure_model_source("heartwood")
        imported = session.import_local_model(
            source,
            source_repository="example/research-model-gguf",
            source_revision="a" * 40,
            license_posture="Apache-2.0",
        )

    imported_path = Path(cast(str, imported["path"]))
    assert imported_path.is_file()
    assert gateway.config_store.load().local_model is not None


def test_notebook_observes_shared_project_setup_and_action_settings(tmp_path: Path) -> None:
    project = ProjectContext(tmp_path)
    session = NotebookSession(
        project=project,
        session_id="notebook-shared-state",
        gateway=SessionGateway(project=project, env={}, backend_id="deterministic"),
    )

    configured = session.configure_model_source("heartwood")
    action_settings = session.select_action_confirmation_mode("confirm-risky")

    reopened = NotebookSession(
        project=project,
        session_id="notebook-shared-state",
        gateway=SessionGateway(project=project, env={}, backend_id="deterministic"),
    )
    assert configured["model_source"] == "heartwood"
    assert action_settings["confirmation_mode"] == "confirm-risky"
    assert reopened.model_settings()["model_source"] == "heartwood"
    assert reopened.action_settings()["confirmation_mode"] == "confirm-risky"
    assert reopened.project_readiness()["project_root"] == str(tmp_path)


def test_notebook_and_browser_transport_share_gateway_setup_projections(tmp_path: Path) -> None:
    project = ProjectContext(tmp_path)
    gateway = SessionGateway(project=project, env={}, backend_id="deterministic")
    notebook = NotebookSession(project=project, gateway=gateway)
    rest = RestGateway(gateway)

    projections = (
        (notebook.model_settings(), gateway.model_settings(), "/settings/models"),
        (notebook.action_settings(), gateway.action_settings(), "/settings/actions"),
        (notebook.project_readiness(), gateway.project_readiness(), "/project/readiness"),
        (
            notebook.platform_capabilities(),
            gateway.platform_capabilities(),
            "/project/capabilities",
        ),
        (
            notebook.startup_plan(),
            gateway.startup_plan(interface="notebook"),
            "/project/startup?interface=notebook",
        ),
    )

    for notebook_value, gateway_value, path in projections:
        response = rest.handle(RestRequest(method="GET", path=path))
        assert response.status_code == 200
        assert notebook_value == gateway_value
        assert json.loads(json.dumps(gateway_value)) == response.body


def test_notebook_and_browser_transport_share_the_exact_session_projection(
    tmp_path: Path,
) -> None:
    project = ProjectContext(tmp_path)
    gateway = SessionGateway(project=project, env={}, backend_id="deterministic")
    notebook = NotebookSession(
        project=project,
        session_id="notebook-browser-parity",
        gateway=gateway,
    )
    rest = RestGateway(gateway)

    notebook.chat("inspect the synthetic workspace")
    notebook_projection = notebook.replay().projection
    response = rest.handle(
        RestRequest(
            method="GET",
            path="/sessions/notebook-browser-parity/projection",
        )
    )

    assert response.status_code == 200
    assert response.body == notebook_projection.safe_dict()


def test_notebook_view_model_preserves_the_complete_gateway_projection() -> None:
    projection = SessionProjection(
        session_id="notebook-complete-projection",
        event_count=8,
        revision=7,
        conversation=(
            ProjectionMessage(
                id="message-1",
                sequence=1,
                role="agent",
                label="Agent",
                content="I am checking the cohort.",
            ),
        ),
        lifecycle=ProjectionLifecycleState(
            status=SessionLifecycle.RUNNING,
            can_pause=True,
            can_steer=True,
        ),
        task_plan=(
            ProjectionTask(
                title="Validate the synthetic cohort",
                status="in-progress",
            ),
        ),
        usage=ProjectionUsage(
            usage_id="total",
            model_name="synthetic-model",
            call_count=2,
            prompt_tokens=128,
            completion_tokens=32,
        ),
        subagents=(
            ProjectionSubagent(
                invocation_id="task-call-1",
                task_id="task-1",
                agent_name="research-planner",
                status="running",
                parent_session_id="notebook-complete-projection",
                parent_action_id="action-1",
            ),
        ),
        streaming_text="Checking column types",
        available_commands=("chat", "pause"),
    )

    view_model = build_view_model(projection)
    sections = {section.title: section.items for section in build_widget_spec(view_model)}

    assert view_model.projection is projection
    assert view_model.lifecycle is projection.lifecycle
    assert view_model.task_plan is projection.task_plan
    assert view_model.usage is projection.usage
    assert view_model.subagents is projection.subagents
    assert view_model.streaming_text == "Checking column types"
    assert sections["Conversation"][-1] == "Agent (working): Checking column types"
    assert sections["Tasks"] == ("in-progress: Validate the synthetic cohort",)
    assert sections["Specialists"] == ("research-planner: running (task-call-1)",)
    assert sections["Runtime"][-1] == (
        "Usage: 128 input, 32 output tokens across 2 calls (synthetic-model)"
    )


def test_notebook_initialization_does_not_advertise_a_terra_web_route(
    tmp_path: Path,
) -> None:
    gateway = SessionGateway(
        project=ProjectContext(tmp_path),
        env={
            "HEARTWOOD_PLATFORM": "terra",
            "JUPYTERHUB_SERVICE_PREFIX": "/user/synthetic/",
        },
        backend_id="deterministic",
    )
    notebook = NotebookSession(gateway=gateway)

    initialized = notebook.initialize_project()

    assert initialized["interface"] == "notebook"
    assert initialized["access_url"] is None
    assert notebook.browser_url(port=9000) is None


def test_notebook_session_uses_unique_commands_without_duplicate_replay(
    tmp_path: Path,
) -> None:
    gateway = _CountingGateway()
    session = NotebookSession(
        project=ProjectContext(tmp_path),
        session_id="notebook-counting",
        gateway=cast(SessionGateway, gateway),
    )

    assert gateway.projection_calls == 0
    session.chat("summarize")
    session.pause()

    assert gateway.projection_calls == 2
    command_ids = [command.command_id for command in gateway.commands]
    assert command_ids[0].startswith("notebook-counting-chat-")
    assert command_ids[1].startswith("notebook-counting-pause-")
    assert len(set(command_ids)) == 2


def test_notebook_approval_command_targets_the_projected_action_group(
    tmp_path: Path,
) -> None:
    gateway = _CountingGateway()
    gateway.projection = SessionProjection(
        session_id="notebook-approval-command",
        event_count=1,
        revision=0,
        pending_approval=ProjectionApprovalGroup(
            group_id="action-set-123",
            actions=(
                ProjectionApprovalAction(
                    target_id="tool-123",
                    tool_name="file_editor",
                    summary="Write the requested file",
                ),
            ),
        ),
    )
    session = NotebookSession(
        project=ProjectContext(tmp_path),
        session_id="notebook-approval-command",
        gateway=cast(SessionGateway, gateway),
    )

    session.approve(group_id="action-set-123")

    assert gateway.commands[-1].payload == {
        "target_type": "action-set",
        "target_id": "action-set-123",
    }


def test_notebook_context_releases_the_shared_gateway(tmp_path: Path) -> None:
    gateway = _CountingGateway()

    with NotebookSession(
        project=ProjectContext(tmp_path),
        gateway=cast(SessionGateway, gateway),
    ):
        pass

    assert gateway.stopped is True


def test_notebook_pause_resume_updates_view_state(tmp_path: Path) -> None:
    session = _deterministic_session(tmp_path, "notebook-lifecycle")
    session.gateway.project.initialize()
    session.gateway._service(session.session_id)._accept_backend_events(
        (
            BackendLifecycleEvent(
                lifecycle=BackendLifecycle.RUNNING,
                source_event_id="synthetic-running",
            ),
        )
    )

    paused = session.pause()
    resumed = session.resume()

    assert paused.paused is True
    assert resumed.paused is False
    assert paused.lifecycle.status == SessionLifecycle.PAUSED
    assert resumed.lifecycle.status == SessionLifecycle.RUNNING


def test_widget_spec_covers_expected_sections(tmp_path: Path) -> None:
    session = _deterministic_session(tmp_path, "notebook-widgets")
    session.pause()
    session.chat("Build the synthetic target-condition cohort and report quality checks.")
    session.audit_export()
    view_model = session.replay()

    sections = build_widget_spec(view_model)
    rendered = render_widgets(view_model)

    assert [section.title for section in sections] == [
        "Conversation",
        "Activity",
        "Approval",
        "Tasks",
        "Runtime",
        "Specialists",
    ]
    assert sections[3].items == ()
    assert isinstance(rendered, object)


def test_widget_rendering_falls_back_without_ipywidgets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = NotebookSession(project=ProjectContext(tmp_path), session_id="notebook-fallback")
    view_model = session.pause()

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
