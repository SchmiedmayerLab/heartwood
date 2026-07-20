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
from heartwood.core_adapter import SessionResult
from heartwood.gateway import (
    CredentialStore,
    ModelCatalogService,
    ModelProfile,
    ProjectContext,
    ProviderModel,
    RestGateway,
    RestRequest,
    SessionGateway,
)
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
        self.stopped = False

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

    paused = session.pause()
    turn = session.chat("inspect the synthetic workspace")
    pending = turn.approval_controls[-1]
    approved = session.approve(tool_call_id=pending.target_id)
    exported = session.audit_export()

    assert paused.paused
    assert turn.policy_status[-1].decision == "allow"
    assert turn.chat[0].role == "user"
    assert turn.chat[0].content == "inspect the synthetic workspace"
    assert turn.chat[1].role == "assistant"
    assert pending.target_type == "tool-call"
    assert pending.decision is None
    assert approved.approval_controls[-1].decision == "approved"
    assert not any(control.target_type == "model-call" for control in turn.approval_controls)
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


def test_notebook_initialization_and_web_route_use_injected_terra_environment(
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
    assert notebook.web_proxy_url(port=9000) == "/user/synthetic/proxy/9000/"


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
        is None
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


def test_notebook_context_releases_the_shared_gateway(tmp_path: Path) -> None:
    gateway = _CountingGateway()

    with NotebookSession(
        project=ProjectContext(tmp_path),
        gateway=cast(SessionGateway, gateway),
    ):
        pass

    assert gateway.stopped is True


def test_notebook_pause_resume_updates_view_state(tmp_path: Path) -> None:
    session = NotebookSession(project=ProjectContext(tmp_path), session_id="notebook-lifecycle")

    paused = session.pause()
    resumed = session.resume()

    assert paused.paused is True
    assert resumed.paused is False


def test_widget_spec_covers_expected_sections(tmp_path: Path) -> None:
    session = _deterministic_session(tmp_path, "notebook-widgets")
    session.pause()
    session.chat("Build the synthetic target-condition cohort and report quality checks.")
    view_model = session.audit_export()

    sections = build_widget_spec(view_model)
    rendered = render_widgets(view_model)

    assert [section.title for section in sections] == [
        "Chat",
        "Activity",
        "Skills",
        "Approvals",
        "Policy",
        "Exports",
    ]
    assert sections[2].items == ()
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
