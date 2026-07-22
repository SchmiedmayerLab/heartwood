# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Session gateway orchestration and model-profile ownership."""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import replace
from functools import wraps
from pathlib import Path
from threading import RLock
from typing import Any, Concatenate, Protocol, cast

from heartwood.adapters.platform import select_platform_adapter
from heartwood.core_adapter import (
    AgentBackend,
    BackendEvent,
    BackendEventKind,
    DeterministicAgentBackend,
    FileSessionStore,
    ProposedToolCall,
    SessionResult,
    SessionService,
)
from heartwood.gateway._action_settings import (
    ACTION_MODE_OPTIONS,
    ActionSettings,
    ActionSettingsError,
)
from heartwood.gateway._credentials import CredentialStore, CredentialStoreError
from heartwood.gateway._gpu_environment import GpuEnvironment, inspect_gpu_environment
from heartwood.gateway._local_import import import_local_model
from heartwood.gateway._local_model_contract import (
    MINIMUM_AGENT_RUNTIME_CONTEXT_WINDOW,
    managed_model_request_body,
    managed_model_token_budgets,
)
from heartwood.gateway._local_models import (
    HuggingFaceModelRepository,
    LocalModelChoice,
    LocalModelRuntime,
    ModelRepositoryError,
    catalog_model_choices,
)
from heartwood.gateway._model_artifacts import (
    LocalModelDownloadManager,
    ModelArtifact,
    ModelArtifactCatalog,
    ModelArtifactError,
    ModelDownload,
    load_model_artifact_catalog,
    verify_model_artifact,
)
from heartwood.gateway._model_artifacts import (
    download_model_artifact as download_artifact,
)
from heartwood.gateway._model_catalog import (
    ModelCatalog,
    ModelCatalogEntry,
    ModelCatalogError,
    ModelCatalogService,
    ModelConnection,
    custom_model_connection,
    load_model_connections,
)
from heartwood.gateway._model_settings import (
    MODEL_PRESETS,
    ModelProfile,
    ModelSettings,
    ModelSettingsError,
    model_profile_from_preset,
)
from heartwood.gateway._model_snapshots import (
    ModelSnapshot,
    ModelSnapshotCatalog,
    ModelSnapshotError,
    ModelTier,
    automatic_model_tier,
    download_model_snapshot,
    load_model_snapshot_catalog,
)
from heartwood.gateway._openhands_sdk import OpenHandsSdkBackend
from heartwood.gateway._project import ProjectContext
from heartwood.gateway._project_config import (
    LocalModelSelection,
    ProjectActionSettingsStore,
    ProjectConfig,
    ProjectConfigError,
    ProjectConfigStore,
    ProjectModelSettingsStore,
)
from heartwood.gateway._readiness import (
    DeploymentReadiness,
    gpu_visible,
    inspect_deployment,
    managed_local_runtime_active,
    model_source_for_connection,
    model_source_options,
    persist_deployment_profile,
)
from heartwood.gateway._session_catalog import (
    DEFAULT_SESSION_ID,
    SessionCatalog,
    SessionCatalogError,
)
from heartwood.gateway._skill_settings import SkillManager
from heartwood.gateway._startup import InterfaceKind, StartupPlan, plan_startup
from heartwood.gateway._stream import EventStreamHub, GatewayEventStream
from heartwood.model_policy import ModelPolicyEngine
from heartwood.schemas import PolicyProfile
from heartwood.session import SessionCommand, SessionEvent

_RESERVED_MODEL_PROFILE_IDS = {"heartwood"}

SessionServiceFactory = Callable[[Path, str], SessionService]


class _SerializedStateOwner(Protocol):
    _state_lock: AbstractContextManager[object]


def _serialized_state[StateOwner: _SerializedStateOwner, **Parameters, Return](
    method: Callable[Concatenate[StateOwner, Parameters], Return],
) -> Callable[Concatenate[StateOwner, Parameters], Return]:
    @wraps(method)
    def locked(
        self: StateOwner,
        *args: Parameters.args,
        **kwargs: Parameters.kwargs,
    ) -> Return:
        with self._state_lock:
            return method(self, *args, **kwargs)

    return cast(Callable[Concatenate[StateOwner, Parameters], Return], locked)


class _ModelSettingsStore(Protocol):
    def load(self) -> ModelSettings:
        """Load model settings."""

    def save(self, settings: ModelSettings) -> None:
        """Persist model settings."""


class _ActionSettingsStore(Protocol):
    def load(self) -> ActionSettings:
        """Load action settings."""

    def save(self, settings: ActionSettings) -> None:
        """Persist action settings."""


class _UnconfiguredAgentBackend:
    """Fail clearly until a model profile has been selected."""

    def __init__(self, action_confirmation_mode: str) -> None:
        self._action_confirmation_mode = action_confirmation_mode

    @property
    def backend_id(self) -> str:
        return "unconfigured"

    @property
    def configuration_error(self) -> str | None:
        return (
            "no active model profile; inspect connections with `heartwood models list` "
            "and select one with `heartwood models connect`"
        )

    @property
    def model_endpoint(self) -> str:
        return "https://model.local.invalid/v1/chat/completions"

    @property
    def model_profile_id(self) -> str:
        return "unconfigured"

    @property
    def capability_tier(self) -> str:
        return "supervised"

    @property
    def credential_reference(self) -> str | None:
        return None

    @property
    def action_confirmation_mode(self) -> str:
        return self._action_confirmation_mode

    @property
    def continuation_requires_model_authorization(self) -> bool:
        return False

    def submit_turn(
        self,
        *,
        session_id: str,  # noqa: ARG002
        prompt: str,  # noqa: ARG002
    ) -> tuple[BackendEvent, ...]:
        return (
            BackendEvent(
                kind=BackendEventKind.ERROR,
                message=self.configuration_error,
            ),
        )

    def restore_pending(
        self,
        tool_calls: tuple[ProposedToolCall, ...],  # noqa: ARG002
    ) -> None:
        return None

    def resolve_confirmation(
        self,
        *,
        session_id: str,  # noqa: ARG002
        tool_call_id: str,  # noqa: ARG002
        approved: bool,  # noqa: ARG002
    ) -> tuple[BackendEvent, ...]:
        return (
            BackendEvent(
                kind=BackendEventKind.ERROR,
                message="no OpenHands conversation is configured",
            ),
        )

    def pause(self) -> None:
        return None

    def resume(self, *, session_id: str) -> tuple[BackendEvent, ...]:  # noqa: ARG002
        return ()

    def close(self) -> None:
        return None


class SessionGateway:
    """Own session services, streams, settings, and the OpenHands adapter."""

    def __init__(
        self,
        *,
        project: ProjectContext | None = None,
        service_factory: SessionServiceFactory | None = None,
        env: Mapping[str, str] | None = None,
        settings_store: _ModelSettingsStore | None = None,
        action_settings_store: _ActionSettingsStore | None = None,
        artifact_catalog: ModelArtifactCatalog | None = None,
        snapshot_catalog: ModelSnapshotCatalog | None = None,
        model_connections: Sequence[ModelConnection] | None = None,
        model_catalog_service: ModelCatalogService | None = None,
        model_repository: HuggingFaceModelRepository | None = None,
        credential_store: CredentialStore | None = None,
        backend_id: str = "auto",
    ) -> None:
        self.project = ProjectContext.current() if project is None else project
        self.sessions_root = self.project.sessions_dir
        self.env = dict(os.environ if env is None else env)
        self.backend_id = backend_id
        self._state_lock: AbstractContextManager[object] = RLock()
        self._gpu_environment: GpuEnvironment | None = None
        adapter = select_platform_adapter(self.env)
        self.config_store = ProjectConfigStore(
            self.project,
            ProjectConfig(
                platform_id=adapter.adapter_id,
                policy=adapter.default_policy_profile(),
            ),
        )
        self.settings_store = settings_store or ProjectModelSettingsStore(self.config_store)
        self.action_settings_store = action_settings_store or ProjectActionSettingsStore(
            self.config_store
        )
        self._base_model_connections = (
            tuple(model_connections)
            if model_connections is not None
            else load_model_connections(None)
        )
        self.model_catalog_service = model_catalog_service or ModelCatalogService()
        self._model_connections: dict[str, ModelConnection] = {}
        self._reload_model_connections()
        self.credential_store = credential_store or CredentialStore(
            project_root=self.project.root,
            capabilities=adapter.capabilities(),
            env=self.env,
            use_system_keyring=env is None,
        )
        self._verified_local_artifacts: set[tuple[Path, int, int, str]] = set()
        repository_root = _repository_root()
        catalog_path = (
            repository_root / "images" / "generic" / "local-runtime" / "model-catalog.toml"
        )
        self.artifact_catalog = artifact_catalog or load_model_artifact_catalog(catalog_path)
        snapshot_catalog_path = (
            repository_root / "images" / "generic" / "local-runtime" / "snapshots.toml"
        )
        self.snapshot_catalog = snapshot_catalog or load_model_snapshot_catalog(
            snapshot_catalog_path
        )
        downloadable_choices = catalog_model_choices(
            self.artifact_catalog.artifacts,
            self.snapshot_catalog.snapshots,
            recommended_only=False,
        )
        self._downloadable_local_model_choices = {
            choice.model_id: choice for choice in downloadable_choices
        }
        self._local_model_choices = dict(self._downloadable_local_model_choices)
        self._recommended_local_model_ids = {
            artifact.artifact_id
            for artifact in self.artifact_catalog.artifacts
            if artifact.recommended
        } | {
            snapshot.snapshot_id
            for snapshot in self.snapshot_catalog.snapshots
            if snapshot.recommended
        }
        selected_local_model = self.config_store.load().local_model
        if selected_local_model is not None:
            selected_choice = self._downloadable_local_model_choices.get(
                selected_local_model.artifact_id
            )
            if selected_choice is None and selected_local_model.catalog_source == "user-selected":
                selected_choice = _selected_local_model_choice(selected_local_model)
                self._downloadable_local_model_choices[selected_choice.model_id] = selected_choice
            if selected_choice is not None:
                self._local_model_choices[selected_choice.model_id] = selected_choice
        self._repository_plans: dict[tuple[str, str], LocalModelChoice] = {}
        self.model_repository = model_repository or HuggingFaceModelRepository(
            token=self.env.get("HF_TOKEN")
        )
        self.model_cache_dir = self.project.models_dir
        self.local_model_manager = LocalModelDownloadManager(
            artifact_catalog=self.artifact_catalog,
            snapshot_catalog=self.snapshot_catalog,
            cache_dir=self.model_cache_dir,
            on_ready=self._select_downloaded_local_model,
        )
        bundled_skills_dir = repository_root / "skills" / "verified"
        self.installed_skills_dir = self.project.skills_dir
        self.skill_manager = SkillManager(
            bundled_dir=bundled_skills_dir,
            installed_dir=self.installed_skills_dir,
            audit_path=self.project.audit_dir / "skill-installations.jsonl",
        )
        self._service_factory = service_factory
        self.session_catalog = SessionCatalog(self.sessions_root)
        self._services: dict[str, SessionService] = {}
        self._streams = EventStreamHub()

    def start(self) -> None:
        """Start the interface lifecycle without requiring an agent dependency import."""

    @_serialized_state
    def initialize_project(self, *, interface: InterfaceKind = "web") -> dict[str, object]:
        """Confirm the current directory as the project and create private state."""
        self.project.initialize()
        return self.startup_plan(interface=interface)

    @_serialized_state
    def stop(self) -> None:
        """Close active OpenHands conversations."""
        self._reset_services()
        self.credential_store.clear_process_values()

    @_serialized_state
    def handle(self, command: SessionCommand) -> SessionResult:
        """Handle one command and publish emitted events."""
        self.project.initialize()
        if command.session_id == DEFAULT_SESSION_ID:
            self.session_catalog.default()
        else:
            self.session_catalog.ensure(command.session_id)
        service = self._service(command.session_id)
        result = service.handle(command)
        self._streams.publish(session_id=command.session_id, events=result.events)
        return result

    def sessions(self) -> dict[str, object]:
        """Return persisted sessions ordered by recent activity."""
        return {"sessions": [summary.safe_dict() for summary in self.session_catalog.list()]}

    def create_session(self, title: str | None = None) -> dict[str, object]:
        """Create and return one empty session."""
        self.project.initialize()
        return self.session_catalog.create(title).safe_dict()

    def default_session(self) -> dict[str, object]:
        """Return the shared first session, creating it when needed."""
        self.project.initialize()
        return self.session_catalog.default().safe_dict()

    def session(self, session_id: str) -> dict[str, object]:
        """Return one persisted session summary."""
        return self.session_catalog.get(session_id).safe_dict()

    def rename_session(self, session_id: str, title: str) -> dict[str, object]:
        """Rename one persisted session."""
        return self.session_catalog.rename(session_id, title).safe_dict()

    def audit_export(self, session_id: str) -> dict[str, object]:
        """Return a generated scrubbed audit export for browser delivery."""
        self.session_catalog.get(session_id)
        store = FileSessionStore(self.sessions_root, session_id)
        try:
            content = store.read_audit_export()
        except OSError as error:
            msg = f"audit export is not available for session: {session_id}"
            raise SessionCatalogError(msg) from error
        return {
            "filename": f"{session_id}-audit.jsonl",
            "content": content,
        }

    @_serialized_state
    def replay_events(
        self,
        *,
        session_id: str,
        after_sequence: int | None = None,
    ) -> tuple[SessionEvent, ...]:
        """Replay persisted events for a session."""
        events = self._service(session_id).replay_events()
        if after_sequence is None:
            return events
        return tuple(event for event in events if event.sequence > after_sequence)

    def websocket(
        self,
        *,
        session_id: str,
        after_sequence: int | None = None,
    ) -> GatewayEventStream:
        """Connect an event stream with replay."""
        return self._streams.connect(
            session_id=session_id,
            replay_events=self.replay_events(
                session_id=session_id,
                after_sequence=after_sequence,
            ),
        )

    def model_settings(self) -> dict[str, object]:
        """Return API-safe settings, connections, and advanced presets."""
        settings = self.settings_store.load()
        config = self.config_store.load()
        credential_env = self._credential_environment(strict=False)
        credential_bindings = sorted(self._credential_binding_ids())
        return {
            **settings.safe_dict(credential_env),
            "model_source": config.model_source,
            "source_options": [
                option.safe_dict(selected=option.source_id == config.model_source)
                for option in model_source_options(self.env)
            ],
            "connections": [
                connection.safe_dict(credential_env)
                for connection in sorted(
                    self._model_connections.values(),
                    key=lambda connection: connection.presentation_order,
                )
            ],
            "presets": [preset.safe_dict() for preset in MODEL_PRESETS],
            "credential_store": self.credential_store.availability().safe_dict(),
            "credential_bindings": [
                self.credential_store.status(binding).safe_dict() for binding in credential_bindings
            ],
        }

    def credential_settings(self) -> dict[str, object]:
        """Return non-secret credential storage and binding state."""
        bindings = sorted(self._credential_binding_ids())
        return {
            "store": self.credential_store.availability().safe_dict(),
            "bindings": [self.credential_store.status(binding).safe_dict() for binding in bindings],
        }

    @_serialized_state
    def forget_credential(self, connection_id: str) -> dict[str, object]:
        """Forget the process and persisted token for one model connection."""
        connection = self._model_connections.get(connection_id)
        if connection is None:
            raise ModelCatalogError(f"unknown model connection: {connection_id}")
        if connection.credential_kind != "environment" or connection.api_key_env is None:
            raise ModelCatalogError("this model connection has no forgettable credential")
        self.credential_store.forget(connection.api_key_env)
        return self.credential_settings()

    def deployment_readiness(self) -> DeploymentReadiness:
        """Inspect the project with every resolvable credential binding."""
        return inspect_deployment(
            self.project,
            self._credential_environment(strict=False),
        )

    def project_readiness(self) -> dict[str, object]:
        """Return content-free project diagnostics for presentation adapters."""
        return self.deployment_readiness().safe_dict()

    def platform_capabilities(self) -> dict[str, object]:
        """Return capabilities owned by the detected platform adapter."""
        return select_platform_adapter(self.env).capabilities().safe_dict()

    def startup(
        self,
        *,
        interface: InterfaceKind,
        port: int = 8767,
    ) -> StartupPlan:
        """Plan the next action with every resolvable credential binding."""
        return plan_startup(
            self.project,
            interface=interface,
            port=port,
            env=self._credential_environment(strict=False),
        )

    def startup_plan(
        self,
        *,
        interface: InterfaceKind,
        port: int = 8767,
    ) -> dict[str, object]:
        """Return the shared startup plan for presentation adapters."""
        return self.startup(interface=interface, port=port).safe_dict()

    @_serialized_state
    def configure_model_source(self, model_source: str) -> dict[str, object]:
        """Prepare one shared model source and its deployment policy."""
        option = next(
            (
                candidate
                for candidate in model_source_options(self.env)
                if candidate.source_id == model_source
            ),
            None,
        )
        if option is None:
            raise ProjectConfigError(f"unsupported model source: {model_source}")
        persist_deployment_profile(
            self.project,
            model_source=option.source_id,
            env=self.env,
        )
        self._reload_model_connections()
        self._reset_services()
        return self.model_settings()

    def discover_models(
        self,
        connection_id: str,
        *,
        token: str | None = None,
        base_url: str | None = None,
        refresh: bool = False,
        remember: bool = False,
    ) -> dict[str, object]:
        """Authorize and discover every model exposed by one connection."""
        connection = self._resolve_model_connection(
            connection_id,
            token=token,
            base_url=base_url,
        )
        if connection.protocol != "static":
            self._authorize_model_catalog(connection)
        if token is not None:
            self._remember_runtime_credential(connection, token, remember=remember)
            refresh = True
        elif remember:
            if connection.credential_kind != "environment" or connection.api_key_env is None:
                raise ModelCatalogError("this model connection has no credential to remember")
            resolved = self.credential_store.resolve(connection.api_key_env)
            if resolved is None:
                raise ModelCatalogError("the provider credential is unavailable")
            self._remember_runtime_credential(connection, resolved, remember=True)
        credential_env = self._credential_environment()
        api_key = connection.resolve_api_key(credential_env)
        catalog = self.model_catalog_service.discover(
            connection,
            api_key=api_key,
            refresh=refresh,
        )
        return catalog.safe_dict(credential_env)

    def connect_model(
        self,
        connection_id: str,
        model_id: str,
        *,
        token: str | None = None,
        base_url: str | None = None,
        manual: bool = False,
        remember: bool = False,
    ) -> dict[str, object]:
        """Select a discovered model and materialize its OpenHands profile."""
        connection = self._resolve_model_connection(
            connection_id,
            token=token,
            base_url=base_url,
        )
        if manual:
            catalog: ModelCatalog | None = self.model_catalog_service.manual(connection, model_id)
            if token is not None:
                self._authorize_model_catalog(connection)
                self._remember_runtime_credential(connection, token, remember=remember)
        elif token is not None:
            self._authorize_model_catalog(connection)
            self._remember_runtime_credential(connection, token, remember=remember)
            catalog = self.model_catalog_service.discover(
                connection,
                api_key=connection.resolve_api_key(self._credential_environment()),
                refresh=True,
            )
        else:
            catalog = self.model_catalog_service.cached(connection.connection_id)
        if catalog is None:
            discovered = self.discover_models(
                connection_id,
                base_url=base_url,
                remember=remember,
            )
            catalog = self.model_catalog_service.cached(connection.connection_id)
            if catalog is None:  # pragma: no cover - defensive invariant
                raise ModelCatalogError(
                    f"model catalog did not cache discovery result: {discovered}"
                )
        entry = _catalog_entry(catalog, model_id)
        if entry.availability == "unsupported":
            raise ModelCatalogError(f"model is unavailable for OpenHands: {entry.reason}")
        if connection.policy_endpoint is None:
            raise ModelCatalogError("model connection does not define a completion endpoint")
        profile = ModelProfile(
            profile_id=connection.connection_id,
            model=entry.execution_model,
            policy_endpoint=connection.policy_endpoint,
            capability_tier=(
                "experimental" if entry.availability == "experimental" else "supervised"
            ),
            base_url=connection.base_url,
            credential_kind=connection.credential_kind,
            api_key_env=connection.api_key_env,
            api_key_file=connection.api_key_file,
            api_version=connection.api_version,
            aws_region_name=connection.aws_region_name,
            aws_profile_name=connection.aws_profile_name,
            description=f"{connection.label}: {entry.display_name}",
        )
        with self._state_lock:
            settings = (
                self.settings_store.load().with_profile(profile).selecting(profile.profile_id)
            )
            self._save_model_selection(self._model_source_for_connection(connection), settings)
            self._reset_services()
            return self.model_settings()

    def action_settings(self) -> dict[str, object]:
        """Return the selected and deployment-allowed confirmation modes."""
        settings = self.action_settings_store.load()
        policy_profile = self._policy_profile()
        allowed = set(policy_profile.allowed_action_confirmation_modes)
        return {
            **settings.safe_dict(),
            "modes": [
                {**option.safe_dict(), "allowed": option.mode in allowed}
                for option in ACTION_MODE_OPTIONS
            ],
        }

    @_serialized_state
    def select_action_confirmation_mode(self, mode: str) -> dict[str, object]:
        """Select a deployment-allowed OpenHands confirmation mode."""
        policy_profile = self._policy_profile()
        if mode not in policy_profile.allowed_action_confirmation_modes:
            msg = f"action confirmation mode is not allowed by platform policy: {mode}"
            raise ActionSettingsError(msg)
        settings = self.action_settings_store.load().selecting(mode)
        self.action_settings_store.save(settings)
        self._reset_services()
        return self.action_settings()

    @_serialized_state
    def save_model_profile(self, profile: ModelProfile) -> dict[str, object]:
        """Add or replace a non-secret profile and reset active services."""
        if profile.profile_id in _RESERVED_MODEL_PROFILE_IDS:
            raise ModelSettingsError(
                f"model profile id is reserved by Heartwood: {profile.profile_id}"
            )
        settings = self.settings_store.load().with_profile(profile)
        self.settings_store.save(settings)
        self._reset_services()
        return self.model_settings()

    @_serialized_state
    def select_model_profile(self, profile_id: str) -> dict[str, object]:
        """Select a profile and reset active services."""
        settings = self.settings_store.load().selecting(profile_id)
        self._save_model_selection(self._model_source_for_profile(settings.profile()), settings)
        self._reset_services()
        return self.model_settings()

    @_serialized_state
    def remove_model_profile(self, profile_id: str) -> dict[str, object]:
        """Remove a profile and reset active services."""
        if profile_id in _RESERVED_MODEL_PROFILE_IDS:
            raise ModelSettingsError(f"model profile is managed by Heartwood: {profile_id}")
        settings = self.settings_store.load().without_profile(profile_id)
        source = self.config_store.load().model_source
        self._save_model_selection(None if source == profile_id else source, settings)
        self._reset_services()
        return self.model_settings()

    def validate_model_profile(self, profile_id: str | None = None) -> dict[str, object]:
        """Validate credential availability and platform route authorization."""
        profile = self.settings_store.load().profile(profile_id)
        action_settings = self.action_settings_store.load()
        policy_profile = self._policy_profile()
        purpose = (
            "selected Heartwood-managed model"
            if profile.profile_id == "heartwood"
            else f"model profile {profile.profile_id}"
        )
        decision = ModelPolicyEngine(policy_profile).evaluate(
            endpoint=profile.policy_endpoint,
            capability_tier=profile.capability_tier,
            action_confirmation_mode=action_settings.confirmation_mode,
            credential_reference=profile.credential_reference,
            decision_id=f"model-profile-{profile.profile_id}",
            purpose=purpose,
        )
        return {
            "profile": profile.safe_dict(),
            "credential_status": profile.credential_status(self._credential_environment()),
            "action_confirmation_mode": action_settings.confirmation_mode,
            "policy_decision": decision.model_dump(mode="json"),
        }

    def model_artifacts(self) -> dict[str, object]:
        """Return normalized local choices, source metadata, and download status."""
        snapshot_catalog = self.snapshot_catalog.safe_dict()
        statuses = {status.model_id: status for status in self.local_model_manager.statuses()}
        selected = self.config_store.load().local_model
        active_model_id = (
            selected.artifact_id
            if selected is not None and managed_local_runtime_active(selected, self.env)
            else None
        )
        if selected is not None and selected.artifact_id not in statuses:
            path = selected.resolved_path(self.project)
            if path.exists():
                size = selected.size_bytes or self._local_model_size(selected.artifact_id, path)
                try:
                    self._verify_selected_local_artifact(selected, path)
                except (OSError, ValueError) as error:
                    statuses[selected.artifact_id] = ModelDownload(
                        model_id=selected.artifact_id,
                        status="error",
                        bytes_downloaded=0,
                        bytes_total=size,
                        error=f"Selected model integrity check failed: {error}",
                    )
                else:
                    statuses[selected.artifact_id] = ModelDownload(
                        model_id=selected.artifact_id,
                        status="ready",
                        bytes_downloaded=size,
                        bytes_total=size,
                        path=str(path),
                    )
        gpu_environment = self.gpu_environment()
        preferred_runtime = self._preferred_local_runtime()
        local_choices = list(self._local_model_choices.values())
        local_choices.sort(
            key=lambda choice: (
                selected is None or choice.model_id != selected.artifact_id,
                not self._local_runtime_available(choice.runtime),
                choice.runtime != preferred_runtime,
            )
        )
        recommendation = self.recommend_managed_model(
            maximum_tier=automatic_model_tier(gpu_environment.platform_id),
            gpu_environment=gpu_environment,
        )
        preferred_id = (
            recommendation.snapshot_id
            if recommendation is not None
            else next(
                (
                    choice.model_id
                    for choice in local_choices
                    if choice.model_id in self._recommended_local_model_ids
                    and choice.qualification_for(gpu_environment.platform_id) == "qualified"
                    and self._local_runtime_available(choice.runtime)
                    and choice.runtime == preferred_runtime
                ),
                None,
            )
        )
        choices = [
            self._local_model_choice_dict(
                choice,
                active=choice.model_id == active_model_id,
                selected=selected is not None and choice.model_id == selected.artifact_id,
                recommendation=(
                    "Selected for this project"
                    if selected is not None and choice.model_id == selected.artifact_id
                    else (
                        "Recommended for this deployment"
                        if selected is None and choice.model_id == preferred_id
                        else None
                    )
                ),
                gpu_environment=gpu_environment,
            )
            for choice in local_choices
        ]
        return {
            **self.artifact_catalog.safe_dict(),
            "snapshot_schema_version": snapshot_catalog["schema_version"],
            "snapshots": snapshot_catalog["snapshots"],
            "models": choices,
            "downloads": [status.safe_dict() for status in statuses.values()],
            "gpu_environment": {
                "platform_id": gpu_environment.platform_id,
                "capacities": [
                    {
                        "label": capacity.label,
                        "gpu_model": capacity.gpu_model,
                        "gpu_count": capacity.gpu_count,
                        "gpu_memory_bytes": capacity.gpu_memory_bytes,
                        "allocation_required": capacity.allocation_required,
                        "partition": capacity.partition,
                    }
                    for capacity in gpu_environment.capacities
                ],
            },
        }

    def gpu_environment(self, *, refresh: bool = False) -> GpuEnvironment:
        """Return the shared GPU and scheduler inventory for this deployment."""
        if refresh or self._gpu_environment is None:
            self._gpu_environment = inspect_gpu_environment(
                self.config_store.load().platform_id,
                self.env,
            )
        return self._gpu_environment

    def recommend_managed_model(
        self,
        *,
        maximum_tier: ModelTier,
        requested_gpus: int | None = None,
        gpu_environment: GpuEnvironment | None = None,
    ) -> ModelSnapshot | None:
        """Choose one qualified catalog model for the detected resource envelopes."""
        environment = gpu_environment or self.gpu_environment()
        return self.snapshot_catalog.recommend_for_capacities(
            platform_id=environment.platform_id,
            capacities=tuple(
                (capacity.gpu_count, capacity.gpu_memory_bytes)
                for capacity in environment.capacities
            ),
            maximum_tier=maximum_tier,
            requested_gpus=requested_gpus,
        )

    def _verify_selected_local_artifact(
        self,
        selected: LocalModelSelection,
        path: Path,
    ) -> None:
        runtime = selected.runtime
        if runtime == "auto":
            runtime = "llama-cpp" if path.suffix.casefold() == ".gguf" else "vllm"
        if runtime != "llama-cpp":
            return
        if selected.size_bytes is None or selected.artifact_sha256 is None:
            raise ModelArtifactError(
                "selected llama.cpp artifact is missing persisted size or checksum metadata"
            )
        stat = path.stat()
        cache_key = (path, stat.st_size, stat.st_mtime_ns, selected.artifact_sha256)
        if cache_key in self._verified_local_artifacts:
            return
        verify_model_artifact(
            path,
            expected_size_bytes=selected.size_bytes,
            expected_sha256=selected.artifact_sha256,
        )
        self._verified_local_artifacts = {cache_key}

    def inspect_model_repository(
        self,
        repository: str,
        *,
        revision: str | None = None,
    ) -> dict[str, object]:
        """Build one automatic download plan without downloading model weights."""
        plan = self.model_repository.plan(
            repository,
            revision=revision,
            cpu_available=self._local_runtime_available("llama-cpp"),
            gpu_available=self._local_runtime_available("vllm"),
        )
        self._repository_plans[(repository.strip(), (revision or "").strip())] = plan.model
        self._repository_plans[(plan.model.source_repository, plan.model.source_revision)] = (
            plan.model
        )
        return {
            "model": self._local_model_choice_dict(plan.model),
            "selection_reason": plan.selection_reason,
        }

    def download_local_model(self, model_id: str) -> dict[str, object]:
        """Start a known local-model download into project storage."""
        self.project.initialize()
        choice = self._require_local_model_runtime(model_id)
        return self.local_model_manager.start_model(choice.download_model()).safe_dict()

    def download_custom_local_model(
        self,
        repository: str,
        *,
        revision: str | None = None,
    ) -> dict[str, object]:
        """Resolve and start one user-selected Hugging Face model download."""
        self.project.initialize()
        choice = self._custom_local_model_choice(
            repository,
            revision=revision,
        )
        return self.local_model_manager.start_model(choice.download_model()).safe_dict()

    def download_local_model_now(
        self,
        model_id: str,
        *,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> Path:
        """Download and verify a known model, selecting it when agent-compatible."""
        self.project.initialize()
        model = self._require_local_model_runtime(model_id).download_model()
        if isinstance(model, ModelArtifact):
            path = download_artifact(
                model,
                cache_dir=self.model_cache_dir,
                progress_callback=progress_callback,
            )
        else:
            path = download_model_snapshot(
                model,
                cache_dir=self.model_cache_dir,
                progress_callback=progress_callback,
            )
        runtime_profile = model.runtime_profile
        self._select_downloaded_local_model(model_id, path, runtime_profile)
        return path

    def download_custom_local_model_now(
        self,
        repository: str,
        *,
        revision: str | None = None,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> Path:
        """Resolve, download, verify, and select one user-selected model."""
        self.project.initialize()
        choice = self._custom_local_model_choice(
            repository,
            revision=revision,
        )
        model = choice.download_model()
        if isinstance(model, ModelArtifact):
            path = download_artifact(
                model,
                cache_dir=self.model_cache_dir,
                progress_callback=progress_callback,
            )
        else:
            path = download_model_snapshot(
                model,
                cache_dir=self.model_cache_dir,
                progress_callback=progress_callback,
            )
        self._select_downloaded_local_model(choice.model_id, path, model.runtime_profile)
        return path

    @_serialized_state
    def import_local_model(
        self,
        source: Path,
        *,
        source_repository: str,
        source_revision: str,
        license_posture: str,
        context_window: int,
    ) -> dict[str, object]:
        """Import and select a reviewed local GGUF file or vLLM snapshot."""
        self.project.initialize()
        imported = import_local_model(
            source,
            models_dir=self.model_cache_dir,
            source_repository=source_repository,
            source_revision=source_revision,
            license_posture=license_posture,
            context_window=context_window,
        )
        choice = imported.model
        previous_config = self.config_store.load() if self.config_store.configured else None
        self._local_model_choices[choice.model_id] = choice
        self._downloadable_local_model_choices[choice.model_id] = choice
        runtime_profile = "llama-cpp-cpu" if choice.runtime == "llama-cpp" else "vllm-cuda"
        try:
            self._select_downloaded_local_model(choice.model_id, imported.path, runtime_profile)
        except BaseException:
            self._local_model_choices.pop(choice.model_id, None)
            self._downloadable_local_model_choices.pop(choice.model_id, None)
            self.config_store.restore(previous_config)
            shutil.rmtree(imported.storage_root, ignore_errors=True)
            raise
        return imported.safe_dict()

    def skill_settings(self) -> dict[str, object]:
        """Return bundled and explicitly installed Skills."""
        return {"skills": [summary.safe_dict() for summary in self.skill_manager.summaries()]}

    def inspect_skill(self, source: Path) -> dict[str, object]:
        """Verify a mounted Skill source without installing it."""
        return self.skill_manager.inspect(source).safe_dict()

    @_serialized_state
    def install_skill(self, source: Path, *, approved: bool) -> dict[str, object]:
        """Install one extension after an explicit trust decision."""
        self.project.initialize()
        self.skill_manager.install(source, approved=approved)
        self._reset_services()
        return self.skill_settings()

    @_serialized_state
    def remove_skill(self, name: str) -> dict[str, object]:
        """Remove one installed extension and reset active conversations."""
        self.skill_manager.remove(name)
        self._reset_services()
        return self.skill_settings()

    @_serialized_state
    def _service(self, session_id: str) -> SessionService:
        service = self._services.get(session_id)
        if service is None:
            if self._service_factory is not None:
                service = self._service_factory(self.sessions_root, session_id)
            else:
                service = self._default_service(session_id)
            self._services[session_id] = service
        return service

    def _default_service(self, session_id: str) -> SessionService:
        model_settings = self.settings_store.load()
        action_settings = self.action_settings_store.load()
        backend = self._backend(
            model_settings=model_settings,
            action_settings=action_settings,
            session_id=session_id,
        )
        return SessionService.local_default(
            self.sessions_root,
            session_id=session_id,
            backend=backend,
            policy_profile=self._policy_profile(),
            env=self.env,
        )

    def _backend(
        self,
        *,
        model_settings: ModelSettings,
        action_settings: ActionSettings,
        session_id: str,
    ) -> AgentBackend:
        backend_id = self.backend_id
        if backend_id in {"deterministic", "deterministic-local"}:
            return DeterministicAgentBackend(
                action_confirmation_mode=action_settings.confirmation_mode
            )
        if backend_id not in {"auto", "openhands", "openhands-sdk"}:
            msg = f"unsupported agent backend: {backend_id}"
            raise ValueError(msg)
        try:
            profile = model_settings.profile()
        except ModelSettingsError:
            return _UnconfiguredAgentBackend(action_settings.confirmation_mode)
        selected_model = self.config_store.load().local_model if profile.is_local else None
        return OpenHandsSdkBackend(
            profile=profile,
            workspace=self.project.root,
            skills_dir=self.skill_manager.bundled_dir,
            additional_skills_dirs=(self.installed_skills_dir,),
            persistence_dir=self.sessions_root / session_id / "openhands",
            conversation_key=f"{self.project.root}#{session_id}",
            credential_environment_names=tuple(
                configured_profile.api_key_env
                for configured_profile in model_settings.profiles
                if configured_profile.credential_kind == "environment"
                and configured_profile.api_key_env is not None
            ),
            action_confirmation_mode=action_settings.confirmation_mode,
            env=self._credential_environment(),
            llm_extra_body=managed_model_request_body(
                selected_model.model_type if selected_model is not None else None
            ),
        )

    def _policy_profile(self) -> PolicyProfile:
        return self.config_store.load().policy

    @_serialized_state
    def _resolve_model_connection(
        self,
        connection_id: str,
        *,
        token: str | None,
        base_url: str | None,
    ) -> ModelConnection:
        connection = self._model_connections.get(connection_id)
        if connection is None:
            raise ModelCatalogError(f"unknown model connection: {connection_id}")
        if connection_id != "custom-api":
            if base_url is not None:
                raise ModelCatalogError("base_url is only accepted for Custom API")
            return connection
        if base_url is None:
            raise ModelCatalogError("Custom API requires a server URL")
        normalized_base_url = base_url.strip().rstrip("/")
        credential_name = connection.api_key_env or "HEARTWOOD_CUSTOM_MODEL_API_KEY"
        runtime_token = (
            self.credential_store.resolve(credential_name)
            if connection.base_url == normalized_base_url
            else None
        )
        has_token = bool(token) or bool(self.env.get(credential_name)) or bool(runtime_token)
        dynamic = custom_model_connection(base_url, has_token=has_token)
        if connection != dynamic:
            self._configure_custom_policy(dynamic)
            if connection.base_url != dynamic.base_url:
                self.credential_store.discard_process_value(credential_name)
            self._model_connections[connection_id] = dynamic
            self.model_catalog_service.invalidate(connection_id)
        return dynamic

    def _configure_custom_policy(self, connection: ModelConnection) -> None:
        if connection.catalog_endpoint is None or connection.policy_endpoint is None:
            raise ModelCatalogError("Custom API requires catalog and completion endpoints")
        adapter = select_platform_adapter(self.env)
        if "custom" not in adapter.capabilities().model_sources:
            raise ModelCatalogError("Custom API is unavailable on this platform")
        default_policy = adapter.default_policy_profile()
        credential_allowlist = default_policy.credential_allowlist
        if connection.credential_reference is not None:
            credential_allowlist = (
                *credential_allowlist,
                connection.credential_reference,
            )
        policy = default_policy.model_copy(
            update={
                "policy_id": f"{adapter.adapter_id}-custom-api",
                "allowed_model_endpoints": (
                    *default_policy.allowed_model_endpoints,
                    connection.policy_endpoint,
                ),
                "allowed_model_catalog_endpoints": (
                    *default_policy.allowed_model_catalog_endpoints,
                    connection.catalog_endpoint,
                ),
                "credential_allowlist": tuple(dict.fromkeys(credential_allowlist)),
                "notes": "Generic project policy for one explicitly selected Custom API route.",
            }
        )

        def apply(config: ProjectConfig) -> ProjectConfig:
            if config.platform_id != adapter.adapter_id or config.policy.policy_id not in {
                default_policy.policy_id,
                f"{adapter.adapter_id}-custom-api",
            }:
                return config
            return replace(config, policy=policy)

        self.config_store.update(apply)

    def _authorize_model_catalog(self, connection: ModelConnection) -> None:
        if connection.catalog_endpoint is None:
            raise ModelCatalogError("model connection does not define a catalog endpoint")
        policy = self._policy_profile()
        catalog_policy = policy.model_copy(
            update={"allowed_model_endpoints": policy.allowed_model_catalog_endpoints}
        )
        action_settings = self.action_settings_store.load()
        decision = ModelPolicyEngine(catalog_policy).evaluate(
            endpoint=connection.catalog_endpoint,
            capability_tier="supervised",
            action_confirmation_mode=action_settings.confirmation_mode,
            credential_reference=connection.credential_reference,
            decision_id=f"model-catalog-{connection.connection_id}",
            purpose=f"model catalog {connection.connection_id}",
        )
        if decision.decision != "allow":
            raise ModelCatalogError(f"model catalog discovery denied: {decision.reason}")

    def _remember_runtime_credential(
        self,
        connection: ModelConnection,
        token: str,
        *,
        remember: bool,
    ) -> None:
        if connection.credential_kind != "environment" or connection.api_key_env is None:
            raise ModelCatalogError("this model connection does not accept an API token")
        if not token.strip():
            raise ModelCatalogError("API token must not be empty")
        if remember and connection.connection_id == "custom-api":
            raise ModelCatalogError(
                "Custom service tokens are process-only because they are tied to a server URL"
            )
        try:
            self.credential_store.save(connection.api_key_env, token, remember=remember)
        except CredentialStoreError as error:
            raise ModelCatalogError(str(error)) from error

    def _credential_environment(self, *, strict: bool = True) -> dict[str, str]:
        return self.credential_store.environment(
            tuple(self._credential_binding_ids()),
            tolerate_backend_errors=not strict,
        )

    def _credential_binding_ids(self) -> set[str]:
        bindings = {
            connection.api_key_env
            for connection in self._model_connections.values()
            if connection.credential_kind == "environment" and connection.api_key_env is not None
        }
        try:
            settings = self.settings_store.load()
        except ModelSettingsError:
            return bindings
        bindings.update(
            profile.api_key_env
            for profile in settings.profiles
            if profile.credential_kind == "environment" and profile.api_key_env is not None
        )
        return bindings

    @_serialized_state
    def _reset_services(self) -> None:
        for service in self._services.values():
            service.close()
        self._services.clear()

    @_serialized_state
    def _save_model_selection(self, source: str | None, settings: ModelSettings) -> None:
        if isinstance(self.settings_store, ProjectModelSettingsStore):
            self.settings_store.save_selection(source, settings)
            return
        self.settings_store.save(settings)
        self.config_store.select_model_source(source, settings)

    @_serialized_state
    def _reload_model_connections(self) -> None:
        configured = self.config_store.load().additional_connections
        allowed_connection_ids = {option.connection_id for option in model_source_options(self.env)}
        loaded = tuple(
            connection
            for connection in (*self._base_model_connections, *configured)
            if connection.connection_id in allowed_connection_ids or connection.source == "platform"
        )
        for connection in loaded:
            connection.validate(configurable=connection.connection_id == "custom-api")
        connection_ids = [connection.connection_id for connection in loaded]
        if len(connection_ids) != len(set(connection_ids)):
            raise ModelCatalogError("model connection ids must be unique")
        previous_ids = set(self._model_connections)
        self._model_connections = {connection.connection_id: connection for connection in loaded}
        for connection_id in previous_ids | set(self._model_connections):
            self.model_catalog_service.invalidate(connection_id)

    def _model_source_for_connection(self, connection: ModelConnection) -> str:
        try:
            return model_source_for_connection(connection.connection_id)
        except ProjectConfigError:
            if connection.source == "platform":
                return connection.connection_id
            raise

    def _model_source_for_profile(self, profile: ModelProfile) -> str:
        if profile.is_local:
            return "heartwood"
        for connection in self._model_connections.values():
            if connection.policy_endpoint == profile.policy_endpoint:
                return self._model_source_for_connection(connection)
        source = self.config_store.load().model_source
        return "custom" if source is None else source

    @_serialized_state
    def _select_downloaded_local_model(
        self,
        model_id: str,
        path: Path,
        runtime_profile: str,
    ) -> None:
        execution_model = "heartwood-managed-model"
        choice = self._downloadable_local_model_choices.get(model_id)
        if choice is None:
            raise ModelRepositoryError(
                f"Heartwood-managed model metadata is unavailable: {model_id}"
            )
        if choice.context_window < MINIMUM_AGENT_RUNTIME_CONTEXT_WINDOW:
            return
        input_capacity, output_budget = managed_model_token_budgets(choice.context_window)
        profile = replace(
            model_profile_from_preset("heartwood-managed", execution_model),
            profile_id="heartwood",
            description=choice.label,
            max_input_tokens=input_capacity,
            max_output_tokens=output_budget,
        )
        settings = (
            self.config_store.load()
            .model_settings.with_profile(profile)
            .selecting(profile.profile_id)
        )
        platform_id = self.config_store.load().platform_id
        self.config_store.select_local_model(
            artifact_id=model_id,
            path=path,
            runtime=_runtime_kind(runtime_profile),
            model_id=execution_model,
            display_name=choice.label,
            source_repository=choice.source_repository,
            source_revision=choice.source_revision,
            source_path=choice.source_path,
            model_type=choice.model_type,
            size_bytes=choice.size_bytes,
            minimum_free_bytes=choice.minimum_free_bytes,
            license_posture=choice.license_posture,
            license_id=choice.license_id,
            artifact_sha256=choice.artifact_sha256,
            context_window=choice.context_window,
            maximum_context_window=choice.maximum_context_window,
            minimum_resource_envelope=choice.minimum_resource_envelope,
            recommended_resource_envelope=choice.recommended_resource_envelope,
            precision=choice.precision,
            tier=choice.tier,
            qualification=choice.qualification_for(platform_id),
            minimum_gpu_count=choice.minimum_gpu_count,
            minimum_gpu_memory_bytes=choice.minimum_gpu_memory_bytes,
            recommended_ram_bytes=choice.recommended_ram_bytes,
            recommended_disk_bytes=choice.recommended_disk_bytes,
            tool_call_parser=choice.tool_call_parser,
            tensor_parallel_size=choice.tensor_parallel_size,
            startup_seconds_min=choice.startup_seconds_min,
            startup_seconds_max=choice.startup_seconds_max,
            download_policy=choice.download_policy,
            allow_patterns=choice.allow_patterns,
            ignore_patterns=choice.ignore_patterns,
            validated_platforms=choice.validated_platforms,
            qualification_test=choice.qualification_test,
            catalog_source=choice.catalog_source,
            settings=settings,
        )
        self._reset_services()

    def _custom_local_model_choice(
        self,
        repository: str,
        *,
        revision: str | None,
    ) -> LocalModelChoice:
        key = (repository.strip(), (revision or "").strip())
        choice = self._repository_plans.get(key)
        if choice is None:
            choice = self.model_repository.plan(
                repository,
                revision=revision,
                cpu_available=self._local_runtime_available("llama-cpp"),
                gpu_available=self._local_runtime_available("vllm"),
            ).model
        existing = self._local_model_choices.get(choice.model_id)
        if existing is not None and existing != choice:
            raise ModelRepositoryError(f"Heartwood-managed model id collision: {choice.model_id}")
        self._local_model_choices[choice.model_id] = choice
        self._downloadable_local_model_choices[choice.model_id] = choice
        return choice

    def _local_model_choice_dict(
        self,
        choice: LocalModelChoice,
        *,
        active: bool = False,
        selected: bool = False,
        recommendation: str | None = None,
        gpu_environment: GpuEnvironment | None = None,
    ) -> dict[str, object]:
        platform_id = (
            gpu_environment.platform_id
            if gpu_environment is not None
            else self.config_store.load().platform_id
        )
        qualification = choice.qualification_for(platform_id)
        runtime_available = self._local_runtime_available(choice.runtime)
        resource_reason: str | None = None
        available = runtime_available
        if choice.runtime == "vllm" and runtime_available:
            environment = gpu_environment or self.gpu_environment()
            available, resource_reason = environment.assess(
                gpu_count=choice.tensor_parallel_size,
                gpu_memory_bytes=choice.minimum_gpu_memory_bytes,
            )
        if qualification == "candidate":
            candidate_reason = "Evaluation candidate; not yet a recommended model"
            recommendation = (
                f"{recommendation}; {candidate_reason.lower()}"
                if recommendation
                else candidate_reason
            )
        unavailable_reason = resource_reason if resource_reason and not available else None
        if resource_reason and available:
            recommendation = (
                f"{recommendation}; {resource_reason}" if recommendation else resource_reason
            )
        reason = self._local_model_availability_reason(
            choice.runtime,
            available=available,
            recommendation=recommendation,
            unavailable_reason=unavailable_reason,
        )
        return {
            **choice.safe_dict(),
            "qualification": qualification,
            "active": active,
            "available": available,
            "selected": selected,
            "availability_reason": reason,
            "recommended": (
                qualification == "qualified"
                and choice.model_id in self._recommended_local_model_ids
            ),
        }

    @staticmethod
    def _local_model_availability_reason(
        runtime: str,
        *,
        available: bool,
        recommendation: str | None,
        unavailable_reason: str | None = None,
    ) -> str:
        if available:
            return recommendation or "Available on this deployment"
        unavailable = unavailable_reason or (
            "Requires a Heartwood NVIDIA GPU runtime"
            if runtime == "vllm"
            else "The portable CPU runtime is not available on this deployment"
        )
        return f"{recommendation}; {unavailable.lower()}" if recommendation else unavailable

    def _preferred_local_runtime(self) -> str | None:
        if self._local_runtime_available("vllm"):
            return "vllm"
        if self._local_runtime_available("llama-cpp"):
            return "llama-cpp"
        return None

    def _require_local_model_runtime(self, model_id: str) -> LocalModelChoice:
        choice = self._downloadable_local_model_choices.get(model_id)
        if choice is None:
            raise ModelRepositoryError(f"unknown Heartwood-managed model: {model_id}")
        details = self._local_model_choice_dict(choice)
        if not details["available"]:
            raise ModelRepositoryError(
                f"{choice.label} is unavailable: {details['availability_reason']}"
            )
        return choice

    def _local_runtime_available(self, runtime: str) -> bool:
        platform_id = self.config_store.load().platform_id
        executable_path = self.env.get("PATH")
        if runtime == "llama-cpp":
            return self._runtime_executable_available(Path("/opt/llama.cpp/llama-server")) or (
                executable_path is not None
                and shutil.which("llama-server", path=executable_path) is not None
            )
        if runtime != "vllm":
            return False
        if platform_id == "carina":
            return True
        runtime_available = self._runtime_executable_available(
            Path("/opt/heartwood-vllm/bin/vllm")
        ) or (
            executable_path is not None and shutil.which("vllm", path=executable_path) is not None
        )
        return runtime_available and gpu_visible(self.env)

    @staticmethod
    def _runtime_executable_available(path: Path) -> bool:
        try:
            return path.is_file() and os.access(path, os.X_OK)
        except OSError:
            return False

    def _local_model_size(self, model_id: str, path: Path) -> int:
        try:
            return self.artifact_catalog.artifact(model_id).artifact_size_bytes
        except ModelArtifactError:
            try:
                return self.snapshot_catalog.snapshot(model_id).expected_size_bytes
            except ModelSnapshotError:
                if path.is_file():
                    return path.stat().st_size
                return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[5]


def _selected_local_model_choice(selection: LocalModelSelection) -> LocalModelChoice:
    """Restore one normalized choice from persisted user-selected metadata."""
    if (
        selection.display_name is None
        or selection.source_repository is None
        or selection.source_revision is None
        or selection.size_bytes is None
        or selection.minimum_free_bytes is None
        or selection.license_posture is None
        or selection.minimum_resource_envelope is None
        or selection.recommended_resource_envelope is None
    ):  # pragma: no cover - validated project-config invariant
        raise ModelRepositoryError("persisted user-selected model provenance is incomplete")
    runtime = selection.runtime
    if runtime == "auto":
        runtime = (
            "llama-cpp" if (selection.source_path or "").casefold().endswith(".gguf") else "vllm"
        )
    choice = LocalModelChoice(
        model_id=selection.artifact_id,
        label=selection.display_name,
        purpose=(
            "User-selected Hugging Face model; Heartwood has not reviewed its capabilities, "
            "license, or suitability."
        ),
        runtime=cast(LocalModelRuntime, runtime),
        source_repository=selection.source_repository,
        source_revision=selection.source_revision,
        source_path=selection.source_path,
        model_type=selection.model_type,
        size_bytes=selection.size_bytes,
        minimum_free_bytes=selection.minimum_free_bytes,
        license_posture=selection.license_posture,
        catalog_source="user-selected",
        artifact_sha256=selection.artifact_sha256,
        context_window=selection.context_window,
        minimum_resource_envelope=selection.minimum_resource_envelope,
        recommended_resource_envelope=selection.recommended_resource_envelope,
        license_id=selection.license_id or "Unspecified",
        precision=selection.precision or "Unspecified",
        tier=cast(Any, selection.tier),
        qualification=cast(Any, selection.qualification),
        minimum_gpu_count=selection.minimum_gpu_count,
        minimum_gpu_memory_bytes=selection.minimum_gpu_memory_bytes,
        recommended_ram_bytes=selection.recommended_ram_bytes or selection.minimum_free_bytes,
        recommended_disk_bytes=selection.recommended_disk_bytes or selection.minimum_free_bytes,
        maximum_context_window=selection.maximum_context_window,
        tool_call_parser=cast(Any, selection.tool_call_parser),
        tensor_parallel_size=selection.tensor_parallel_size,
        startup_seconds_min=selection.startup_seconds_min,
        startup_seconds_max=selection.startup_seconds_max,
        download_policy=selection.download_policy,
        allow_patterns=selection.allow_patterns,
        ignore_patterns=selection.ignore_patterns,
        validated_platforms=selection.validated_platforms,
        qualification_test=selection.qualification_test,
    )
    choice.validate()
    return choice


def _catalog_entry(catalog: ModelCatalog, model_id: str) -> ModelCatalogEntry:
    selected = model_id.strip()
    for entry in catalog.models:
        if selected in {entry.model_id, entry.execution_model}:
            return entry
    raise ModelCatalogError(f"model is not present in the discovered catalog: {model_id}")


def _runtime_kind(profile: str) -> str:
    if profile.startswith("llama-cpp"):
        return "llama-cpp"
    if profile.startswith("vllm"):
        return "vllm"
    raise ModelArtifactError(f"unsupported managed runtime profile: {profile}")
