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
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING, Any, Concatenate, Literal, Protocol, cast
from uuid import uuid4

from heartwood.adapters.platform import select_platform_adapter
from heartwood.audit import (
    AuditCheckpointVerification,
    AuditVerification,
    CheckpointSigner,
    CheckpointSignerProfile,
    CheckpointSignerRegistry,
    create_audit_checkpoint,
    discover_checkpoint_signer_registry,
    verify_audit_checkpoint,
)
from heartwood.core_adapter import (
    AgentBackend,
    BackendErrorCode,
    BackendErrorEvent,
    BackendEvent,
    BackendEventSink,
    DeterministicAgentBackend,
    FileSessionStore,
    PendingActionGroup,
    SessionResult,
    SessionService,
    TokenDeltaSink,
)
from heartwood.gateway._action_presentation import action_presentation
from heartwood.gateway._action_settings import (
    ACTION_MODE_OPTIONS,
    ACTION_MODE_SCOPE_DESCRIPTION,
    ActionSettings,
    ActionSettingsError,
)
from heartwood.gateway._credential_isolation import (
    CredentialIsolation,
    assess_credential_isolation,
    credential_isolation_unavailable_reason,
)
from heartwood.gateway._credentials import CredentialStore, CredentialStoreError
from heartwood.gateway._gpu_environment import (
    GpuEnvironment,
    inspect_gpu_environment,
    minimum_compute_capability_for_model,
)
from heartwood.gateway._local_import import import_local_model
from heartwood.gateway._local_model_contract import (
    MINIMUM_AGENT_RUNTIME_CONTEXT_WINDOW,
    managed_model_native_tool_calling,
    managed_model_request_body,
    managed_model_token_budgets,
)
from heartwood.gateway._local_models import (
    HuggingFaceModelRepository,
    LocalModelCatalogSource,
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
    active_model_connections,
    custom_model_connection,
    load_model_connections,
    matching_model_connection,
)
from heartwood.gateway._model_settings import (
    MODEL_PRESETS,
    ModelProfile,
    ModelSettings,
    ModelSettingsError,
    align_model_profile_request_endpoint,
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
from heartwood.gateway._model_transfer import (
    ModelTransferError,
    ModelTransferManager,
    inspect_model_bundle,
)
from heartwood.gateway._openhands_models import prepare_openhands_import
from heartwood.gateway._project import ProjectContext, ProjectStateError
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
from heartwood.gateway._session_projection import (
    SessionLifecycle,
    SessionProjection,
    project_session,
)
from heartwood.gateway._skill_settings import SkillManager, SkillSettingsError
from heartwood.gateway._startup import InterfaceKind, StartupPlan, plan_startup
from heartwood.gateway._stream import EventStreamHub, GatewayEventStream
from heartwood.gateway._subscriptions import (
    OpenHandsOpenAISubscription,
    SubscriptionError,
    SubscriptionProvider,
)
from heartwood.gateway._workspace import WorkspaceInspector
from heartwood.model_policy import ModelPolicyEngine
from heartwood.persistence import DurableFileError, write_private_text_atomic
from heartwood.schemas import (
    ActionSettingsResponse,
    AuditExportResponse,
    CredentialSettingsResponse,
    LocalModelImportResponse,
    ModelArtifactsResponse,
    ModelCatalogResponse,
    ModelDownloadResponse,
    ModelRepositoryPlanResponse,
    ModelSettingsResponse,
    ModelTransferPlanResponse,
    ModelTransferResponse,
    ModelValidationResponse,
    PlatformCapabilitiesResponse,
    PolicyProfile,
    ProjectReadinessResponse,
    SessionListResponse,
    SessionSummaryResponse,
    SkillSettingsResponse,
    SkillSummaryResponse,
    SpecialistSettingsResponse,
    StartupPlanResponse,
    SubscriptionDeviceLoginResponse,
    WorkspaceChangesResponse,
    WorkspaceDiffResponse,
    WorkspaceFileResponse,
    WorkspaceTreeResponse,
    api_response,
)
from heartwood.session import CommandKind, EventKind, SessionCommand, SessionEvent
from heartwood.skills import (
    SkillArtifactStore,
    SkillCatalogError,
    SkillSourceRegistry,
    configured_skill_source_registry,
)

if TYPE_CHECKING:
    from heartwood.gateway._specialists import SpecialistCatalog

_RESERVED_MODEL_PROFILE_IDS = {"heartwood"}
_PROJECTED_COMMANDS = frozenset(
    {
        CommandKind.APPROVE.value,
        CommandKind.CHAT.value,
        CommandKind.DENY.value,
        CommandKind.PAUSE.value,
        CommandKind.RESUME.value,
    }
)
_STREAMING_COMMANDS = frozenset(
    {
        CommandKind.APPROVE.value,
        CommandKind.CHAT.value,
        CommandKind.RESUME.value,
    }
)

SessionServiceFactory = Callable[[Path, str], SessionService]


@dataclass(frozen=True, slots=True)
class GatewaySessionSnapshot:
    """One coherent durable-event and interface-projection snapshot."""

    events: tuple[SessionEvent, ...]
    projection: SessionProjection


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


@dataclass(frozen=True, slots=True)
class _ServiceConfiguration:
    """Configuration snapshot that owns one cached agent service."""

    model_settings: ModelSettings
    action_settings: ActionSettings
    policy_profile: PolicyProfile
    local_model: LocalModelSelection | None


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

    def bind_runtime(
        self,
        *,
        event_sink: BackendEventSink,  # noqa: ARG002
        token_sink: TokenDeltaSink,  # noqa: ARG002
    ) -> None:
        return None

    def reconcile(
        self,
        *,
        session_id: str,  # noqa: ARG002
        known_source_event_ids: frozenset[str],  # noqa: ARG002
    ) -> tuple[BackendEvent, ...]:
        return ()

    def pending_action_group(
        self,
        *,
        session_id: str,  # noqa: ARG002
    ) -> PendingActionGroup | None:
        return None

    def submit_turn(
        self,
        *,
        session_id: str,  # noqa: ARG002
        prompt: str,  # noqa: ARG002
    ) -> tuple[BackendEvent, ...]:
        return (
            BackendErrorEvent(
                error_code=BackendErrorCode.RUNTIME_UNAVAILABLE,
            ),
        )

    def resolve_confirmation(
        self,
        *,
        session_id: str,  # noqa: ARG002
        action_group_id: str,  # noqa: ARG002
        approved: bool,  # noqa: ARG002
    ) -> tuple[BackendEvent, ...]:
        return (
            BackendErrorEvent(
                error_code=BackendErrorCode.RUNTIME_UNAVAILABLE,
            ),
        )

    def pause(self, *, session_id: str) -> tuple[BackendEvent, ...]:  # noqa: ARG002
        return (
            BackendErrorEvent(
                error_code=BackendErrorCode.RUNTIME_UNAVAILABLE,
            ),
        )

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
        checkpoint_signer_registry: CheckpointSignerRegistry | None = None,
        checkpoint_signer_factory: Callable[[CheckpointSignerProfile], CheckpointSigner]
        | None = None,
        subscription_provider: SubscriptionProvider | None = None,
        workspace_inspector: WorkspaceInspector | None = None,
        skill_source_registry: SkillSourceRegistry | None = None,
        backend_id: str = "auto",
    ) -> None:
        prepare_openhands_import()
        self.project = ProjectContext.current() if project is None else project
        self.sessions_root = self.project.sessions_dir
        self.env = dict(os.environ if env is None else env)
        self.backend_id = backend_id
        self._checkpoint_signer_registry_override = checkpoint_signer_registry
        self._checkpoint_signer_registry_cache: CheckpointSignerRegistry | None = None
        self._checkpoint_signer_factory = checkpoint_signer_factory
        self._state_lock: AbstractContextManager[object] = RLock()
        self._stream_lock = RLock()
        self._stream_epoch = uuid4().hex
        self._gpu_environment: GpuEnvironment | None = None
        adapter = select_platform_adapter(self.env)
        self._platform_capabilities = adapter.capabilities()
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
            capabilities=self._platform_capabilities,
            env=self.env,
            use_system_keyring=env is None,
        )
        self.subscription_provider = subscription_provider or OpenHandsOpenAISubscription()
        self.workspace_inspector = workspace_inspector or WorkspaceInspector(self.project)
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
            if selected_local_model.catalog_source == "transferred" or (
                selected_choice is None and selected_local_model.catalog_source == "user-selected"
            ):
                selected_choice = _selected_local_model_choice(selected_local_model)
                if selected_local_model.catalog_source == "user-selected":
                    self._downloadable_local_model_choices[selected_choice.model_id] = (
                        selected_choice
                    )
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
        self.model_transfer_manager = ModelTransferManager(
            models_dir=self.model_cache_dir,
            on_import_ready=self._select_transferred_local_model,
        )
        bundled_skills_dir = repository_root / "vendor" / "heartwood-skills" / "skills" / "verified"
        if skill_source_registry is None:
            try:
                skill_source_registry, _ = configured_skill_source_registry(self.env)
            except SkillCatalogError as error:
                raise SkillSettingsError(str(error)) from error
        self.skill_manager = SkillManager(
            bundled_dir=bundled_skills_dir,
            store=SkillArtifactStore(self.project.skills_dir),
            source_registry=skill_source_registry,
            cache_dir=self.project.cache_dir / "skills",
            audit_path=self.project.audit_dir / "skill-installations.jsonl",
            platform_id=adapter.adapter_id,
        )
        self._specialist_catalog_cache: SpecialistCatalog | None = None
        self._service_factory = service_factory
        self.session_catalog = SessionCatalog(self.sessions_root)
        self._services: dict[str, SessionService] = {}
        self._service_configurations: dict[str, _ServiceConfiguration] = {}
        self._streams = EventStreamHub()
        self._streaming_text: dict[str, str] = {}
        self._stream_revisions: dict[str, int] = {}
        self._streaming_active: set[str] = set()
        self._published_stream_sequences: dict[str, int] = {}
        self._pending_stream_events: dict[str, dict[int, SessionEvent]] = {}

    def start(self) -> None:
        """Start the interface lifecycle without requiring an agent dependency import."""

    @_serialized_state
    def initialize_project(self, *, interface: InterfaceKind = "web") -> StartupPlanResponse:
        """Confirm the current directory as the project and create private state."""
        self.project.initialize()
        return self.startup_plan(interface=interface)

    @_serialized_state
    def stop(self) -> None:
        """Close active OpenHands conversations."""
        try:
            self._reset_services()
        finally:
            self.credential_store.clear_process_values()

    @_serialized_state
    def handle(self, command: SessionCommand) -> SessionResult:
        """Handle one command and publish emitted events."""
        with self.config_store.locked():
            self.project.initialize()
            if command.session_id == DEFAULT_SESSION_ID:
                self.session_catalog.default()
            else:
                self.session_catalog.ensure(command.session_id)
            command_kind = str(command.kind)
            storage_only = command_kind == CommandKind.AUDIT_EXPORT.value
            fatal_unavailable_reason: str | None = None
            if not storage_only and command_kind in _PROJECTED_COMMANDS:
                persisted = FileSessionStore(
                    self.sessions_root,
                    command.session_id,
                ).replay_events()
                persisted_projection = project_session(
                    persisted,
                    session_id=command.session_id,
                )
                if (
                    persisted_projection.lifecycle.status == SessionLifecycle.ERROR
                    and not persisted_projection.lifecycle.can_steer
                ):
                    fatal_unavailable_reason = (
                        f"{command_kind} is unavailable while the agent is "
                        f"{persisted_projection.lifecycle.status}"
                    )
            close_service = False
            if storage_only or fatal_unavailable_reason is not None:
                service = self._services.get(command.session_id)
                if service is None or fatal_unavailable_reason is not None:
                    service = self._storage_service(command.session_id)
                    close_service = True
            else:
                service = self._service(command.session_id)
            try:
                state_reconciled = not storage_only and fatal_unavailable_reason is None
                all_events = (
                    self._reconciled_session_events(
                        session_id=command.session_id,
                        service=service,
                    )
                    if state_reconciled
                    else service.replay_events()
                )
                with self._stream_lock:
                    projection = self._snapshot_from_events_locked(
                        session_id=command.session_id,
                        all_events=all_events,
                    ).projection
                    unavailable_reason = fatal_unavailable_reason or (
                        None
                        if command_kind not in _PROJECTED_COMMANDS
                        or command_kind in projection.available_commands
                        else (
                            f"{command_kind} is unavailable while the agent is "
                            f"{projection.lifecycle.status}"
                        )
                    )
                    streaming_started = (
                        unavailable_reason is None and command_kind in _STREAMING_COMMANDS
                    )
                    streaming_was_active = command.session_id in self._streaming_active
                    if streaming_started:
                        self._streaming_active.add(command.session_id)
                try:
                    result = (
                        service.handle(
                            command,
                            reconcile_before_command=not state_reconciled,
                        )
                        if unavailable_reason is None
                        else service.handle(
                            command,
                            unavailable_reason=unavailable_reason,
                            reconcile_before_command=not state_reconciled,
                        )
                    )
                except Exception:
                    if streaming_started and not streaming_was_active:
                        with self._stream_lock:
                            self._streaming_active.discard(command.session_id)
                            if self._streaming_text.pop(command.session_id, None) is not None:
                                self._advance_stream_revision(command.session_id)
                                self._streams.notify(session_id=command.session_id)
                    raise
                if result.replayed:
                    if streaming_started and not streaming_was_active:
                        with self._stream_lock:
                            self._streaming_active.discard(command.session_id)
                else:
                    self._publish_committed_events(
                        session_id=command.session_id,
                        events=result.events,
                    )
                return result
            finally:
                if close_service:
                    service.close()

    def sessions(self) -> SessionListResponse:
        """Return persisted sessions ordered by recent activity."""
        return api_response(
            SessionListResponse,
            {"sessions": [summary.safe_dict() for summary in self.session_catalog.list()]},
        )

    def create_session(self, title: str | None = None) -> SessionSummaryResponse:
        """Create and return one empty session."""
        self.project.initialize()
        return api_response(SessionSummaryResponse, self.session_catalog.create(title).safe_dict())

    def default_session(self) -> SessionSummaryResponse:
        """Return the shared first session, creating it when needed."""
        self.project.initialize()
        return api_response(SessionSummaryResponse, self.session_catalog.default().safe_dict())

    def session(self, session_id: str) -> SessionSummaryResponse:
        """Return one persisted session summary."""
        return api_response(
            SessionSummaryResponse,
            self.session_catalog.get(session_id).safe_dict(),
        )

    def rename_session(self, session_id: str, title: str) -> SessionSummaryResponse:
        """Rename one persisted session."""
        return api_response(
            SessionSummaryResponse,
            self.session_catalog.rename(session_id, title).safe_dict(),
        )

    def audit_export(self, session_id: str) -> AuditExportResponse:
        """Return a generated scrubbed audit export for browser delivery."""
        self.session_catalog.get(session_id)
        store = FileSessionStore(self.sessions_root, session_id)
        try:
            content = store.read_audit_export()
        except (DurableFileError, OSError) as error:
            msg = f"audit export is not available for session: {session_id}"
            raise SessionCatalogError(msg) from error
        return api_response(
            AuditExportResponse,
            {
                "filename": f"{session_id}-audit.jsonl",
                "content": content,
            },
        )

    def verify_audit(self, session_id: str) -> AuditVerification:
        """Fully verify the paired session and audit history."""
        self.session_catalog.get(session_id)
        _content, verification = FileSessionStore(
            self.sessions_root,
            session_id,
        ).verified_audit_export()
        return verification

    def copy_audit_export(self, session_id: str, output: Path) -> Path:
        """Write a generated export to a caller-selected non-state path safely."""
        resolved = self._resolve_audit_path(output, label="copy destination")
        if resolved == self.project.state_root or self.project.state_root in resolved.parents:
            raise ProjectStateError("audit copies cannot overwrite private Heartwood state")
        content = self.audit_export(session_id)["content"]
        try:
            write_private_text_atomic(resolved, content, secure_parent=False)
        except (DurableFileError, OSError) as error:
            raise ProjectStateError("unable to write the audit copy safely") from error
        return resolved

    def create_audit_checkpoint(
        self,
        *,
        session_id: str,
        output: Path,
        deployment_id: str,
        retention_policy_id: str,
        retain_until: str,
    ) -> AuditCheckpointVerification:
        """Generate and sign an authoritative export outside the agent project."""
        resolved_output = self._deployment_owned_path(output, label="checkpoint output")
        if resolved_output.exists() or resolved_output.is_symlink():
            raise ProjectStateError("audit checkpoint output already exists")
        profile = self.active_checkpoint_signer()
        signer = (
            profile.signer()
            if self._checkpoint_signer_factory is None
            else profile.validating_signer(self._checkpoint_signer_factory(profile))
        )
        self.handle(
            SessionCommand(
                command_id=f"audit-checkpoint-{uuid4().hex}",
                session_id=session_id,
                kind=CommandKind.AUDIT_EXPORT,
                actor_id="deployment-operator",
                created_at=(
                    datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
                ),
                payload={},
            )
        )
        content = self.audit_export(session_id)["content"]
        return create_audit_checkpoint(
            audit_content=content,
            session_id=session_id,
            output=resolved_output,
            deployment_id=deployment_id,
            retention_policy_id=retention_policy_id,
            retain_until=retain_until,
            signer=signer,
        )

    def checkpoint_signers(self) -> tuple[CheckpointSignerProfile, ...]:
        """Return deployment-approved signer profiles without resolving credentials."""
        return self._checkpoint_signer_registry().profiles

    def default_checkpoint_signer(self) -> CheckpointSignerProfile:
        """Return the deployment-owned default without changing project selection."""
        return self._checkpoint_signer_registry().profile()

    def active_checkpoint_signer(self) -> CheckpointSignerProfile:
        """Resolve the project selection or deployment-owned default profile."""
        selected = self.config_store.load().audit_settings.signer_profile
        return self._checkpoint_signer_registry().profile(selected)

    def select_checkpoint_signer(self, profile_id: str | None) -> CheckpointSignerProfile:
        """Persist a project selection after deployment-registry validation."""
        registry = self._checkpoint_signer_registry()
        profile = registry.profile(profile_id)
        self.config_store.select_checkpoint_signer(profile_id)
        return profile

    def verify_audit_checkpoint(
        self,
        *,
        bundle: Path,
        public_key: Path | None = None,
    ) -> AuditCheckpointVerification:
        """Verify an authoritative export with a deployment-owned public key."""
        resolved_bundle = self._deployment_owned_path(bundle, label="checkpoint bundle")
        profile = self.active_checkpoint_signer() if public_key is None else None
        key = profile.trusted_public_key if profile is not None else public_key
        if key is None:  # pragma: no cover - branch is constrained above
            raise ProjectStateError("trusted public key is unavailable")
        resolved_key = self._deployment_owned_path(key, label="trusted public key")
        verification = verify_audit_checkpoint(
            bundle=resolved_bundle,
            public_key=resolved_key,
        )
        if profile is not None:
            profile.validate_signature(verification.checkpoint.signature)
        return verification

    def _checkpoint_signer_registry(self) -> CheckpointSignerRegistry:
        if self._checkpoint_signer_registry_cache is not None:
            return self._checkpoint_signer_registry_cache
        registry = self._checkpoint_signer_registry_override or discover_checkpoint_signer_registry(
            self.env
        )
        if registry.source is not None:
            self._deployment_owned_path(registry.source, label="signer registry")
        for profile in registry.profiles:
            self._deployment_owned_path(
                profile.trusted_public_key,
                label="trusted public key",
            )
            if profile.authorization_token_file is not None:
                self._deployment_owned_path(
                    profile.authorization_token_file,
                    label="signer authorization token",
                )
        self._checkpoint_signer_registry_cache = registry
        return registry

    def _deployment_owned_path(self, path: Path, *, label: str) -> Path:
        resolved = self._resolve_audit_path(path, label=label)
        if resolved == self.project.root or self.project.root in resolved.parents:
            raise ProjectStateError(f"audit {label} must be outside the Heartwood project")
        return resolved

    @staticmethod
    def _resolve_audit_path(path: Path, *, label: str) -> Path:
        expanded = path.expanduser()
        absolute = expanded if expanded.is_absolute() else Path.cwd() / expanded
        if absolute.is_symlink():
            raise ProjectStateError(f"audit {label} must not be a symbolic link")
        try:
            resolved = absolute.resolve()
        except (OSError, RuntimeError) as error:
            raise ProjectStateError(f"audit {label} is unavailable") from error
        return resolved

    def workspace_tree(
        self,
        *,
        path: str = ".",
        depth: int | None = None,
    ) -> WorkspaceTreeResponse:
        """Return the bounded project tree shared by every interface."""
        self.project.initialize()
        return self.workspace_inspector.tree(path, depth=depth)

    def workspace_file(self, *, path: str) -> WorkspaceFileResponse:
        """Return one bounded read-only project file."""
        self.project.initialize()
        return self.workspace_inspector.file(path)

    def workspace_changes(self, *, session_id: str) -> WorkspaceChangesResponse:
        """Return Git or structured session-derived project changes."""
        projection = self.session_projection(session_id=session_id)
        return self.workspace_inspector.changes(projection)

    def workspace_diff(
        self,
        *,
        session_id: str,
        path: str,
    ) -> WorkspaceDiffResponse:
        """Return one bounded project diff."""
        projection = self.session_projection(session_id=session_id)
        return self.workspace_inspector.diff(projection, path)

    @_serialized_state
    def replay_events(
        self,
        *,
        session_id: str,
        after_sequence: int | None = None,
    ) -> tuple[SessionEvent, ...]:
        """Replay persisted events for a session."""
        self.project.initialize()
        events = FileSessionStore(self.sessions_root, session_id).replay_events()
        return (
            events
            if after_sequence is None
            else tuple(event for event in events if event.sequence > after_sequence)
        )

    @_serialized_state
    def session_projection(self, *, session_id: str) -> SessionProjection:
        """Return the sole interface projection for one session."""
        return self._session_snapshot_locked(session_id=session_id).projection

    @_serialized_state
    def persisted_session_projection(self, *, session_id: str) -> SessionProjection:
        """Project committed Heartwood events without reconciling OpenHands state."""
        with self._stream_lock:
            self.project.initialize()
            events = FileSessionStore(self.sessions_root, session_id).replay_events()
            return project_session(
                events,
                session_id=session_id,
                streaming_text=self._streaming_text.get(session_id, ""),
                stream_epoch=self._stream_epoch,
                stream_revision=self._stream_revisions.get(session_id, 0),
            )

    @_serialized_state
    def session_snapshot(
        self,
        *,
        session_id: str,
        after_sequence: int | None = None,
    ) -> GatewaySessionSnapshot:
        """Return events and projection from one serialized gateway snapshot."""
        return self._session_snapshot_locked(
            session_id=session_id,
            after_sequence=after_sequence,
        )

    @_serialized_state
    def websocket(
        self,
        *,
        session_id: str,
        after_sequence: int | None = None,
    ) -> GatewayEventStream:
        """Connect an event stream with replay."""
        all_events = self._reconciled_session_events(session_id=session_id)
        with self._stream_lock:
            snapshot = self._snapshot_from_events_locked(
                session_id=session_id,
                all_events=all_events,
                after_sequence=after_sequence,
            )
            return self._streams.connect(
                session_id=session_id,
                replay_events=snapshot.events,
            )

    @_serialized_state
    def open_event_stream(
        self,
        *,
        session_id: str,
        after_sequence: int | None = None,
    ) -> tuple[GatewayEventStream, GatewaySessionSnapshot]:
        """Connect a stream and capture its first coherent snapshot atomically."""
        all_events = self._reconciled_session_events(session_id=session_id)
        with self._stream_lock:
            snapshot = self._snapshot_from_events_locked(
                session_id=session_id,
                all_events=all_events,
                after_sequence=after_sequence,
            )
            stream = self._streams.connect(
                session_id=session_id,
                initial_changed=False,
            )
            return stream, snapshot

    def model_settings(self) -> ModelSettingsResponse:
        """Return API-safe settings, connections, and advanced presets."""
        settings = self.settings_store.load()
        config = self.config_store.load()
        credential_env = self._credential_environment(strict=False)
        credential_bindings = sorted(self._credential_binding_ids())
        safe_settings = settings.safe_dict(credential_env)
        profiles = safe_settings.get("profiles", [])
        has_subscription_profile = isinstance(profiles, list) and any(
            isinstance(profile, dict) and profile.get("auth_type") == "subscription"
            for profile in profiles
        )
        has_subscription_connection = any(
            connection.protocol == "subscription" for connection in self._model_connections.values()
        )
        subscription_status = (
            self._subscription_credential_status()
            if has_subscription_profile or has_subscription_connection
            else None
        )
        if isinstance(profiles, list):
            for profile in profiles:
                if isinstance(profile, dict) and profile.get("auth_type") == "subscription":
                    profile["credential_status"] = subscription_status
        credential_isolation = self._credential_isolation(
            settings,
            model_source=config.model_source,
        )
        return api_response(
            ModelSettingsResponse,
            {
                **safe_settings,
                "model_source": config.model_source,
                "source_options": [
                    option.safe_dict(selected=option.source_id == config.model_source)
                    for option in model_source_options(self.env)
                ],
                "connections": [
                    self._safe_connection(connection, credential_env, subscription_status)
                    for connection in sorted(
                        self._model_connections.values(),
                        key=lambda connection: connection.presentation_order,
                    )
                ],
                "presets": [preset.safe_dict() for preset in MODEL_PRESETS],
                "credential_store": self.credential_store.availability().safe_dict(),
                "credential_bindings": [
                    self.credential_store.status(binding).safe_dict()
                    for binding in credential_bindings
                ],
                "credential_isolation": credential_isolation.safe_dict(),
            },
        )

    def credential_settings(self) -> CredentialSettingsResponse:
        """Return non-secret credential storage and binding state."""
        bindings = sorted(self._credential_binding_ids())
        return api_response(
            CredentialSettingsResponse,
            {
                "store": self.credential_store.availability().safe_dict(),
                "bindings": [
                    self.credential_store.status(binding).safe_dict() for binding in bindings
                ],
            },
        )

    @_serialized_state
    def forget_credential(self, connection_id: str) -> CredentialSettingsResponse:
        """Forget the process and persisted token for one model connection."""
        connection = self._model_connections.get(connection_id)
        if connection is None:
            raise ModelCatalogError(f"unknown model connection: {connection_id}")
        if connection.protocol == "subscription":
            try:
                self.subscription_provider.logout()
            except SubscriptionError as error:
                raise ModelCatalogError(str(error)) from error
            self._reset_services()
            return self.credential_settings()
        if connection.credential_kind != "environment" or connection.api_key_env is None:
            raise ModelCatalogError("this model connection has no forgettable credential")
        self.credential_store.forget(connection.api_key_env)
        return self.credential_settings()

    def login_subscription(
        self,
        connection_id: str,
        *,
        model_id: str,
        force_login: bool = False,
        open_browser: bool = True,
        auth_method: Literal["browser", "device_code"] = "browser",
    ) -> ModelSettingsResponse:
        """Run the OpenHands interactive subscription login flow."""
        connection = self._subscription_connection(connection_id)
        if auth_method not in {"browser", "device_code"}:
            raise ModelCatalogError("unsupported subscription login method")
        try:
            self.subscription_provider.login(
                model=connection.provider_model_id(model_id),
                force_login=force_login,
                open_browser=open_browser,
                auth_method=auth_method,
            )
        except SubscriptionError as error:
            raise ModelCatalogError(str(error)) from error
        return self.model_settings()

    @_serialized_state
    def start_subscription_device_login(
        self,
        connection_id: str,
    ) -> SubscriptionDeviceLoginResponse:
        """Start an OpenHands device-code flow for browser and remote clients."""
        self._subscription_connection(connection_id)
        try:
            return api_response(
                SubscriptionDeviceLoginResponse,
                self.subscription_provider.start_device_login().safe_dict(),
            )
        except SubscriptionError as error:
            raise ModelCatalogError(str(error)) from error

    @_serialized_state
    def poll_subscription_device_login(
        self,
        connection_id: str,
        login_id: str,
    ) -> SubscriptionDeviceLoginResponse:
        """Poll one OpenHands device-code flow."""
        self._subscription_connection(connection_id)
        try:
            return api_response(
                SubscriptionDeviceLoginResponse,
                self.subscription_provider.poll_device_login(login_id).safe_dict(),
            )
        except SubscriptionError as error:
            raise ModelCatalogError(str(error)) from error

    def deployment_readiness(self) -> DeploymentReadiness:
        """Inspect the project with every resolvable credential binding."""
        return inspect_deployment(
            self.project,
            self._credential_environment(strict=False),
        )

    def project_readiness(self) -> ProjectReadinessResponse:
        """Return content-free project diagnostics for presentation adapters."""
        return api_response(ProjectReadinessResponse, self.deployment_readiness().safe_dict())

    def platform_capabilities(self) -> PlatformCapabilitiesResponse:
        """Return capabilities owned by the detected platform adapter."""
        return api_response(
            PlatformCapabilitiesResponse,
            self._platform_capabilities.safe_dict(),
        )

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
    ) -> StartupPlanResponse:
        """Return the shared startup plan for presentation adapters."""
        return api_response(
            StartupPlanResponse,
            self.startup(interface=interface, port=port).safe_dict(),
        )

    @_serialized_state
    def configure_model_source(self, model_source: str) -> ModelSettingsResponse:
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
    ) -> ModelCatalogResponse:
        """Authorize and discover every model exposed by one connection."""
        connection = self._resolve_model_connection(
            connection_id,
            token=token,
            base_url=base_url,
        )
        if connection.protocol not in {"static", "subscription"}:
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
        payload = catalog.safe_dict(credential_env)
        if connection.protocol == "subscription":
            safe_connection = payload.get("connection")
            if isinstance(safe_connection, dict):
                safe_connection["credential_status"] = self._subscription_credential_status()
        return api_response(ModelCatalogResponse, payload)

    def connect_model(
        self,
        connection_id: str,
        model_id: str,
        *,
        token: str | None = None,
        base_url: str | None = None,
        manual: bool = False,
        remember: bool = False,
    ) -> ModelSettingsResponse:
        """Select a discovered model and materialize its OpenHands profile."""
        self._prepare_model_connection(connection_id)
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
        request_endpoint = connection.request_endpoint(entry.execution_model)
        profile = ModelProfile(
            profile_id=connection.connection_id,
            model=entry.execution_model,
            policy_endpoint=request_endpoint,
            capability_tier=(
                "experimental" if entry.availability == "experimental" else "supervised"
            ),
            base_url=connection.base_url,
            credential_kind=connection.credential_kind,
            auth_type=("subscription" if connection.protocol == "subscription" else "api_key"),
            subscription_vendor=connection.subscription_vendor,
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

    def action_settings(self) -> ActionSettingsResponse:
        """Return the selected and deployment-allowed confirmation modes."""
        try:
            config = self.config_store.load()
        except ProjectConfigError as error:
            raise ActionSettingsError(str(error)) from error
        settings = (
            config.action_settings
            if isinstance(self.action_settings_store, ProjectActionSettingsStore)
            else self.action_settings_store.load()
        )
        model_settings = (
            config.model_settings
            if isinstance(self.settings_store, ProjectModelSettingsStore)
            else self.settings_store.load()
        )
        policy_profile = config.policy
        policy_allowed = set(policy_profile.allowed_action_confirmation_modes)
        isolation = self._credential_isolation(
            model_settings,
            model_source=config.model_source,
        )
        blocking_sessions = self._active_service_session_ids()
        return api_response(
            ActionSettingsResponse,
            {
                **settings.safe_dict(),
                "scope_description": ACTION_MODE_SCOPE_DESCRIPTION,
                "presentation": action_presentation(),
                "change_allowed": not blocking_sessions,
                "change_blocked_reason": (
                    None
                    if not blocking_sessions
                    else "Finish or resolve active session work before changing approvals."
                ),
                "modes": [
                    {
                        **option.safe_dict(),
                        "allowed": (
                            option.mode in policy_allowed and isolation.allows(option.mode)
                        ),
                        "unavailable_reason": (
                            None
                            if (option.mode in policy_allowed and isolation.allows(option.mode))
                            else (
                                "Unavailable under the active platform policy."
                                if option.mode not in policy_allowed
                                else credential_isolation_unavailable_reason(
                                    isolation,
                                    option.mode,
                                )
                            )
                        ),
                    }
                    for option in ACTION_MODE_OPTIONS
                ],
            },
        )

    @_serialized_state
    def select_action_confirmation_mode(self, mode: str) -> ActionSettingsResponse:
        """Select a deployment-allowed OpenHands confirmation mode."""
        blocking_sessions = self._active_service_session_ids()
        if blocking_sessions:
            raise ActionSettingsError(
                "action confirmation mode cannot change while a session is active"
            )
        validated_mode = ActionSettings().selecting(mode).confirmation_mode
        config = self.config_store.load()
        isolation = self._credential_isolation(
            self.settings_store.load(),
            model_source=config.model_source,
        )
        reason = credential_isolation_unavailable_reason(
            isolation,
            validated_mode,
        )
        if reason is not None:
            raise ActionSettingsError(reason)
        if isinstance(self.action_settings_store, ProjectActionSettingsStore):
            try:
                self.config_store.update(
                    lambda config: _select_action_confirmation_mode(config, mode)
                )
            except ProjectConfigError as error:
                raise ActionSettingsError(str(error)) from error
        else:
            with self.config_store.locked():
                config = self.config_store.load()
                if mode not in config.policy.allowed_action_confirmation_modes:
                    msg = f"action confirmation mode is not allowed by platform policy: {mode}"
                    raise ActionSettingsError(msg)
                settings = self.action_settings_store.load().selecting(mode)
                self.action_settings_store.save(settings)
        self._reset_services()
        return self.action_settings()

    @_serialized_state
    def save_model_profile(self, profile: ModelProfile) -> ModelSettingsResponse:
        """Add or replace a non-secret profile and reset active services."""
        profile = align_model_profile_request_endpoint(profile)
        if profile.profile_id in _RESERVED_MODEL_PROFILE_IDS:
            raise ModelSettingsError(
                f"model profile id is reserved by Heartwood: {profile.profile_id}"
            )
        settings = self.settings_store.load().with_profile(profile)
        isolation = self._credential_isolation(
            settings,
            model_source=self.config_store.load().model_source,
        )
        reason = credential_isolation_unavailable_reason(
            isolation,
            self.action_settings_store.load().confirmation_mode,
        )
        if reason is not None:
            raise ModelSettingsError(reason)
        self.settings_store.save(settings)
        self._reset_services()
        return self.model_settings()

    @_serialized_state
    def select_model_profile(self, profile_id: str) -> ModelSettingsResponse:
        """Select a profile and reset active services."""
        settings = self.settings_store.load().selecting(profile_id)
        self._save_model_selection(self._model_source_for_profile(settings.profile()), settings)
        self._reset_services()
        return self.model_settings()

    @_serialized_state
    def remove_model_profile(self, profile_id: str) -> ModelSettingsResponse:
        """Remove a profile and reset active services."""
        if profile_id in _RESERVED_MODEL_PROFILE_IDS:
            raise ModelSettingsError(f"model profile is managed by Heartwood: {profile_id}")
        settings = self.settings_store.load().without_profile(profile_id)
        source = self.config_store.load().model_source
        self._save_model_selection(None if source == profile_id else source, settings)
        self._reset_services()
        return self.model_settings()

    def validate_model_profile(
        self,
        profile_id: str | None = None,
    ) -> ModelValidationResponse:
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
        return api_response(
            ModelValidationResponse,
            {
                "profile": profile.safe_dict(),
                "credential_status": (
                    self._subscription_credential_status()
                    if profile.auth_type == "subscription"
                    else profile.credential_status(self._credential_environment())
                ),
                "credential_isolation": assess_credential_isolation(
                    profile,
                    self._platform_capabilities,
                    model_source=self._model_source_for_profile(profile),
                    model_connections=self._model_connections.values(),
                ).safe_dict(),
                "action_confirmation_mode": action_settings.confirmation_mode,
                "policy_decision": decision.model_dump(mode="json"),
            },
        )

    def model_artifacts(self) -> ModelArtifactsResponse:
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
        return api_response(
            ModelArtifactsResponse,
            {
                **self.artifact_catalog.safe_dict(),
                "snapshot_schema_version": snapshot_catalog["schema_version"],
                "snapshots": snapshot_catalog["snapshots"],
                "models": choices,
                "downloads": [status.safe_dict() for status in statuses.values()],
                "transfers": [
                    transfer.safe_dict() for transfer in self.model_transfer_manager.statuses()
                ],
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
            },
        )

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
    ) -> ModelRepositoryPlanResponse:
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
        return api_response(
            ModelRepositoryPlanResponse,
            {
                "model": self._local_model_choice_dict(plan.model),
                "selection_reason": plan.selection_reason,
            },
        )

    def download_local_model(self, model_id: str) -> ModelDownloadResponse:
        """Start a known local-model download into project storage."""
        self.project.initialize()
        choice = self._require_downloadable_local_model(model_id)
        return api_response(
            ModelDownloadResponse,
            self.local_model_manager.start_model(choice.download_model()).safe_dict(),
        )

    def download_custom_local_model(
        self,
        repository: str,
        *,
        revision: str | None = None,
    ) -> ModelDownloadResponse:
        """Resolve and start one user-selected Hugging Face model download."""
        self.project.initialize()
        choice = self._custom_local_model_choice(
            repository,
            revision=revision,
        )
        return api_response(
            ModelDownloadResponse,
            self.local_model_manager.start_model(choice.download_model()).safe_dict(),
        )

    def download_local_model_now(
        self,
        model_id: str,
        *,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> Path:
        """Download and verify a known model, selecting it when agent-compatible."""
        self.project.initialize()
        model = self._require_downloadable_local_model(model_id).download_model()
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
    ) -> LocalModelImportResponse:
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
        except Exception:
            self._local_model_choices.pop(choice.model_id, None)
            self._downloadable_local_model_choices.pop(choice.model_id, None)
            self.config_store.restore(previous_config)
            shutil.rmtree(imported.storage_root, ignore_errors=True)
            raise
        selected = self.config_store.load().local_model
        selected_for_project = selected is not None and selected.artifact_id == choice.model_id
        return api_response(
            LocalModelImportResponse,
            {
                "model": self._local_model_choice_dict(
                    choice,
                    active=(
                        selected_for_project
                        and selected is not None
                        and managed_local_runtime_active(selected, self.env)
                    ),
                    selected=selected_for_project,
                    recommendation="Selected for this project",
                ),
                "path": str(imported.path),
                "status": "ready",
            },
        )

    def inspect_local_model_bundle(self, path: Path) -> ModelTransferPlanResponse:
        """Inspect one portable model bundle without changing project state."""
        plan = inspect_model_bundle(path)
        choice = self._trusted_transferred_model_choice(plan.model)
        warnings = self._model_transfer_warnings(choice)
        return api_response(
            ModelTransferPlanResponse,
            {
                "bundle_path": str(plan.bundle_path),
                "bundle_size_bytes": plan.bundle_size_bytes,
                "manifest_sha256": plan.manifest_sha256,
                "file_count": len(plan.manifest.files),
                "runtime_profile": plan.manifest.runtime_profile,
                "model": self._local_model_choice_dict(choice),
                "warnings": list(warnings),
            },
        )

    def export_local_model(self, path: Path) -> ModelTransferResponse:
        """Start a verified export of the selected Heartwood-managed model."""
        self.project.initialize()
        selected = self.config_store.load().local_model
        if selected is None:
            raise ModelTransferError(
                "select or download a Heartwood-managed model before exporting a bundle"
            )
        choice = self._local_model_choices.get(selected.artifact_id)
        if choice is None:
            choice = _selected_local_model_choice(selected)
        transfer = self.model_transfer_manager.start_export(
            choice=choice,
            model_path=selected.resolved_path(self.project),
            bundle_path=path,
            warnings=self._model_transfer_warnings(choice),
        )
        return api_response(ModelTransferResponse, transfer.safe_dict())

    def import_local_model_bundle(
        self,
        path: Path,
        *,
        approved: bool,
        manifest_sha256: str,
    ) -> ModelTransferResponse:
        """Start an atomic model-bundle import after explicit review."""
        self.project.initialize()
        plan = inspect_model_bundle(path)
        if plan.manifest_sha256 != manifest_sha256:
            raise ModelTransferError(
                "model bundle manifest changed after review; inspect the bundle again"
            )
        choice = self._trusted_transferred_model_choice(plan.model)
        transfer = self.model_transfer_manager.start_import(
            plan=plan,
            approved=approved,
            warnings=self._model_transfer_warnings(choice),
        )
        return api_response(ModelTransferResponse, transfer.safe_dict())

    def cancel_model_transfer(self, transfer_id: str) -> ModelTransferResponse:
        """Request cancellation of one active model transfer."""
        return api_response(
            ModelTransferResponse,
            self.model_transfer_manager.cancel(transfer_id).safe_dict(),
        )

    def model_transfer_status(self, transfer_id: str) -> ModelTransferResponse:
        """Return one shared model-transfer status snapshot."""
        return api_response(
            ModelTransferResponse,
            self.model_transfer_manager.status(transfer_id).safe_dict(),
        )

    def skill_settings(self) -> SkillSettingsResponse:
        """Return the shared bundled, installed, and signed-catalog projection."""
        return api_response(
            SkillSettingsResponse,
            {"skills": [summary.safe_dict() for summary in self.skill_manager.summaries()]},
        )

    @_serialized_state
    def refresh_skills(self, source_id: str | None = None) -> SkillSettingsResponse:
        """Refresh signed Skill sources and apply their current revocation state."""
        self.project.initialize()
        self.skill_manager.refresh(source_id)
        self._reset_services()
        return self.skill_settings()

    def specialist_settings(self) -> SpecialistSettingsResponse:
        """Return the validated research-specialist catalog."""
        return api_response(
            SpecialistSettingsResponse,
            self._specialist_catalog().safe_dict(),
        )

    def inspect_skill(self, name: str, *, source_id: str | None = None) -> SkillSummaryResponse:
        """Verify one signed catalog entry without installing its archive."""
        summary = self.skill_manager.inspect_catalog(name, source_id=source_id)
        return api_response(SkillSummaryResponse, summary.safe_dict())

    @_serialized_state
    def install_skill(
        self,
        name: str,
        *,
        source_id: str | None,
        expected_tree_sha256: str,
        approved: bool,
    ) -> SkillSettingsResponse:
        """Install one signed, digest-pinned Skill after explicit approval."""
        self.project.initialize()
        self.skill_manager.install_catalog(
            name,
            source_id=source_id,
            expected_tree_sha256=expected_tree_sha256,
            approved=approved,
        )
        self._reset_services()
        return self.skill_settings()

    def inspect_local_skill(self, source: Path) -> SkillSummaryResponse:
        """Verify one advanced local Skill without assigning repository review."""
        summary = self.skill_manager.inspect_local(source)
        return api_response(SkillSummaryResponse, summary.safe_dict())

    @_serialized_state
    def install_local_skill(
        self,
        source: Path,
        *,
        expected_tree_sha256: str,
        approved: bool,
    ) -> SkillSettingsResponse:
        """Install one digest-pinned local, unreviewed Skill after approval."""
        self.project.initialize()
        self.skill_manager.install_local(
            source,
            expected_tree_sha256=expected_tree_sha256,
            approved=approved,
        )
        self._reset_services()
        return self.skill_settings()

    @_serialized_state
    def remove_skill(self, name: str) -> SkillSettingsResponse:
        """Remove one installed extension and reset active conversations."""
        self.skill_manager.remove(name)
        self._reset_services()
        return self.skill_settings()

    @_serialized_state
    def _service(self, session_id: str) -> SessionService:
        configuration = self._service_configuration()
        service = self._services.get(session_id)
        if service is not None and self._service_configurations.get(session_id) != configuration:
            service.close()
            self._services.pop(session_id, None)
            self._service_configurations.pop(session_id, None)
            with self._stream_lock:
                had_transient_state = (
                    session_id in self._streaming_active or session_id in self._streaming_text
                )
                self._streaming_active.discard(session_id)
                self._streaming_text.pop(session_id, None)
                if had_transient_state:
                    self._advance_stream_revision(session_id)
                    self._streams.notify(session_id=session_id)
            service = None
        if service is None:
            if self._service_factory is not None:
                service = self._service_factory(self.sessions_root, session_id)
            else:
                service = self._default_service(session_id, configuration)
            self._services[session_id] = service
            self._service_configurations[session_id] = configuration
        return service

    def _service_configuration(self) -> _ServiceConfiguration:
        config = self.config_store.load()
        model_settings = (
            config.model_settings
            if isinstance(self.settings_store, ProjectModelSettingsStore)
            else self.settings_store.load()
        )
        action_settings = (
            config.action_settings
            if isinstance(self.action_settings_store, ProjectActionSettingsStore)
            else self.action_settings_store.load()
        )
        isolation = self._credential_isolation(
            model_settings,
            model_source=config.model_source,
        )
        reason = credential_isolation_unavailable_reason(
            isolation,
            action_settings.confirmation_mode,
        )
        if reason is not None:
            raise ActionSettingsError(reason)
        return _ServiceConfiguration(
            model_settings=model_settings,
            action_settings=action_settings,
            policy_profile=config.policy,
            local_model=config.local_model,
        )

    def _default_service(
        self,
        session_id: str,
        configuration: _ServiceConfiguration,
    ) -> SessionService:
        backend = self._backend(
            model_settings=configuration.model_settings,
            action_settings=configuration.action_settings,
            selected_model=configuration.local_model,
            session_id=session_id,
        )
        return SessionService.local_default(
            self.sessions_root,
            session_id=session_id,
            backend=backend,
            policy_profile=configuration.policy_profile,
            env=self.env,
            event_sink=lambda events: self._publish_background_events(
                session_id=session_id,
                events=events,
            ),
            token_sink=lambda delta: self._publish_token_delta(
                session_id=session_id,
                delta=delta,
            ),
        )

    def _storage_service(self, session_id: str) -> SessionService:
        """Build an uncached service for commands that only access durable state."""
        configuration = self._service_configuration()
        return SessionService.local_default(
            self.sessions_root,
            session_id=session_id,
            backend=_UnconfiguredAgentBackend(configuration.action_settings.confirmation_mode),
            policy_profile=configuration.policy_profile,
            env=self.env,
        )

    def _backend(
        self,
        *,
        model_settings: ModelSettings,
        action_settings: ActionSettings,
        selected_model: LocalModelSelection | None,
        session_id: str,
    ) -> AgentBackend:
        backend_id = self.backend_id
        if backend_id in {"deterministic", "deterministic-local"}:
            return DeterministicAgentBackend(
                action_confirmation_mode=action_settings.confirmation_mode,
                persistence_path=(self.sessions_root / session_id / ".deterministic-backend.json"),
            )
        if backend_id not in {"auto", "openhands", "openhands-sdk"}:
            msg = f"unsupported agent backend: {backend_id}"
            raise ValueError(msg)
        try:
            profile = model_settings.profile()
        except ModelSettingsError:
            return _UnconfiguredAgentBackend(action_settings.confirmation_mode)
        selected_model = selected_model if profile.is_local else None
        from heartwood.gateway._openhands_sdk import OpenHandsSdkBackend

        return OpenHandsSdkBackend(
            profile=profile,
            workspace=self.project.root,
            skills_dir=self.skill_manager.bundled_dir,
            additional_skills_dirs=self.skill_manager.active_skill_roots(),
            specialist_catalog=self._specialist_catalog(),
            persistence_dir=self.sessions_root / session_id / "openhands",
            conversation_key=f"{self.project.root}#{session_id}",
            credential_environment_names=tuple(
                configured_profile.api_key_env
                for configured_profile in model_settings.profiles
                if configured_profile.credential_kind == "environment"
                and configured_profile.api_key_env is not None
            ),
            action_confirmation_mode=action_settings.confirmation_mode,
            env=self._credential_environment(strict=False),
            llm_extra_body=managed_model_request_body(
                selected_model.model_type if selected_model is not None else None
            ),
            native_tool_calling=(
                managed_model_native_tool_calling(selected_model.tool_call_parser)
                if selected_model is not None
                else None
            ),
        )

    def _specialist_catalog(self) -> SpecialistCatalog:
        catalog = self._specialist_catalog_cache
        if catalog is not None:
            return catalog
        from heartwood.gateway._specialists import load_specialist_catalog

        catalog = load_specialist_catalog(
            _repository_root() / "agents" / "verified",
            self.skill_manager.bundled_dir,
        )
        self._specialist_catalog_cache = catalog
        return catalog

    def _publish_background_events(
        self,
        *,
        session_id: str,
        events: tuple[SessionEvent, ...],
    ) -> None:
        """Publish events committed by the supervised OpenHands worker."""
        if not events:
            return
        self._publish_committed_events(session_id=session_id, events=events)

    def _publish_token_delta(self, *, session_id: str, delta: str) -> None:
        """Update transient visible model text without writing the event log."""
        if not delta:
            return
        with self._stream_lock:
            if session_id not in self._streaming_active:
                return
            self._streaming_text[session_id] = self._streaming_text.get(session_id, "") + delta
            self._advance_stream_revision(session_id)
            self._streams.notify(session_id=session_id)

    def _session_snapshot_locked(
        self,
        *,
        session_id: str,
        after_sequence: int | None = None,
    ) -> GatewaySessionSnapshot:
        all_events = self._reconciled_session_events(session_id=session_id)
        with self._stream_lock:
            return self._snapshot_from_events_locked(
                session_id=session_id,
                all_events=all_events,
                after_sequence=after_sequence,
            )

    def _reconciled_session_events(
        self,
        *,
        session_id: str,
        service: SessionService | None = None,
    ) -> tuple[SessionEvent, ...]:
        """Resolve the service and reconcile durable state without holding the stream lock."""
        self.project.initialize()
        if service is None:
            store = FileSessionStore(
                self.sessions_root,
                session_id,
            )
            persisted_events = store.replay_events()
            persisted_projection = project_session(
                persisted_events,
                session_id=session_id,
            )
            lifecycle = persisted_projection.lifecycle.status
            unresolved_command_ids = store.unresolved_command_ids()
            if (
                lifecycle
                not in {
                    SessionLifecycle.RUNNING,
                    SessionLifecycle.PAUSED,
                    SessionLifecycle.WAITING_FOR_CONFIRMATION,
                }
                and not unresolved_command_ids
                and session_id not in self._services
            ):
                return persisted_events
            close_service = False
            if (
                lifecycle == SessionLifecycle.ERROR
                and not persisted_projection.lifecycle.can_steer
                and unresolved_command_ids
                and session_id not in self._services
            ):
                active_service = self._storage_service(session_id)
                close_service = True
            else:
                active_service = self._service(session_id)
        else:
            active_service = service
            close_service = False
        try:
            active_service.reconcile()
            return active_service.replay_events()
        finally:
            if close_service:
                active_service.close()

    def _snapshot_from_events_locked(
        self,
        *,
        session_id: str,
        all_events: tuple[SessionEvent, ...],
        after_sequence: int | None = None,
    ) -> GatewaySessionSnapshot:
        """Publish and project one durable event snapshot while holding the stream lock."""
        with self._stream_lock:
            self._publish_committed_events(
                session_id=session_id,
                events=all_events,
            )
            events = (
                all_events
                if after_sequence is None
                else tuple(event for event in all_events if event.sequence > after_sequence)
            )
            return GatewaySessionSnapshot(
                events=events,
                projection=project_session(
                    all_events,
                    session_id=session_id,
                    streaming_text=self._streaming_text.get(session_id, ""),
                    stream_epoch=self._stream_epoch,
                    stream_revision=self._stream_revisions.get(session_id, 0),
                ),
            )

    def _update_streaming_state(
        self,
        *,
        session_id: str,
        events: tuple[SessionEvent, ...],
    ) -> None:
        for event in events:
            if _starts_streaming_text(event):
                self._streaming_active.add(session_id)
            if _clears_streaming_text(event):
                self._streaming_active.discard(session_id)
                if self._streaming_text.pop(session_id, None) is not None:
                    self._advance_stream_revision(session_id)

    def _publish_committed_events(
        self,
        *,
        session_id: str,
        events: tuple[SessionEvent, ...],
    ) -> tuple[SessionEvent, ...]:
        """Publish each durable sequence once and apply transient boundaries in order."""
        if not events:
            return ()
        with self._stream_lock:
            watermark = self._published_stream_sequences.get(session_id, -1)
            pending = self._pending_stream_events.setdefault(session_id, {})
            for event in events:
                if event.sequence > watermark:
                    pending[event.sequence] = event
            next_sequence = watermark + 1
            unpublished_events: list[SessionEvent] = []
            while next_sequence in pending:
                event = pending.pop(next_sequence)
                unpublished_events.append(event)
                next_sequence += 1
            unpublished = tuple(unpublished_events)
            if not unpublished:
                if not pending:
                    self._pending_stream_events.pop(session_id, None)
                return ()
            self._update_streaming_state(session_id=session_id, events=unpublished)
            self._published_stream_sequences[session_id] = unpublished[-1].sequence
            if not pending:
                self._pending_stream_events.pop(session_id, None)
            self._streams.publish(session_id=session_id, events=unpublished)
            return unpublished

    def _advance_stream_revision(self, session_id: str) -> None:
        self._stream_revisions[session_id] = self._stream_revisions.get(session_id, 0) + 1

    def _policy_profile(self) -> PolicyProfile:
        return self.config_store.load().policy

    def _prepare_model_connection(self, connection_id: str) -> None:
        if connection_id in self._model_connections:
            return
        try:
            model_source = model_source_for_connection(connection_id)
        except ProjectConfigError:
            return
        available_sources = {option.source_id for option in model_source_options(self.env)}
        if model_source not in available_sources:
            raise ModelCatalogError(f"{connection_id} is unavailable in the detected environment")
        try:
            self.configure_model_source(model_source)
        except ProjectConfigError as error:
            raise ModelCatalogError(str(error)) from error

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

    def _subscription_connection(self, connection_id: str) -> ModelConnection:
        connection = self._model_connections.get(connection_id)
        if connection is None:
            raise ModelCatalogError(f"unknown model connection: {connection_id}")
        if (
            connection.protocol != "subscription"
            or connection.connection_id != self.subscription_provider.connection_id
        ):
            raise ModelCatalogError("this model connection does not support account sign-in")
        return connection

    def _subscription_credential_status(self) -> str:
        try:
            return "available" if self.subscription_provider.credential_available() else "missing"
        except SubscriptionError:
            return "missing"

    def _safe_connection(
        self,
        connection: ModelConnection,
        credential_env: Mapping[str, str],
        subscription_status: str | None,
    ) -> dict[str, object]:
        payload = connection.safe_dict(credential_env)
        if connection.protocol == "subscription":
            payload["credential_status"] = subscription_status or "missing"
        return payload

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
        responses_endpoints = (
            (connection.policy_endpoint.removesuffix("/chat/completions") + "/responses",)
            if connection.policy_endpoint.endswith("/chat/completions")
            else ()
        )
        policy = default_policy.model_copy(
            update={
                "policy_id": f"{adapter.adapter_id}-custom-api",
                "allowed_model_endpoints": tuple(
                    dict.fromkeys(
                        (
                            *default_policy.allowed_model_endpoints,
                            connection.policy_endpoint,
                            *responses_endpoints,
                        )
                    )
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
            raise ModelCatalogError("this model connection does not accept an API key")
        if not token.strip():
            raise ModelCatalogError("API key must not be empty")
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
    def _active_service_session_ids(self) -> tuple[str, ...]:
        active: list[str] = []
        for session_id, service in self._services.items():
            projection = project_session(
                service.replay_events(),
                session_id=session_id,
                streaming_text=self._streaming_text.get(session_id, ""),
                stream_epoch=self._stream_epoch,
                stream_revision=self._stream_revisions.get(session_id, 0),
            )
            if projection.lifecycle.status in {
                SessionLifecycle.RUNNING,
                SessionLifecycle.PAUSED,
                SessionLifecycle.WAITING_FOR_CONFIRMATION,
            }:
                active.append(session_id)
        return tuple(sorted(active))

    @_serialized_state
    def _reset_services(self) -> None:
        services = tuple(self._services.items())
        failed: dict[str, SessionService] = {}
        configurations = dict(self._service_configurations)
        self._services.clear()
        self._service_configurations.clear()
        failed_configurations: dict[str, _ServiceConfiguration] = {}
        errors: list[Exception] = []
        for session_id, service in services:
            try:
                service.close()
            except Exception as error:
                failed[session_id] = service
                configuration = configurations.get(session_id)
                if configuration is not None:
                    failed_configurations[session_id] = configuration
                errors.append(error)
            else:
                with self._stream_lock:
                    had_transient_state = (
                        session_id in self._streaming_active or session_id in self._streaming_text
                    )
                    self._streaming_active.discard(session_id)
                    self._streaming_text.pop(session_id, None)
                    if had_transient_state:
                        self._advance_stream_revision(session_id)
                        self._streams.notify(session_id=session_id)
        self._services = failed
        self._service_configurations = failed_configurations
        if errors:
            raise ExceptionGroup("unable to close all session services", errors)

    @_serialized_state
    def _save_model_selection(self, source: str | None, settings: ModelSettings) -> None:
        isolation = self._credential_isolation(
            settings,
            model_source=source,
        )
        action_settings = self.action_settings_store.load()
        reason = credential_isolation_unavailable_reason(
            isolation,
            action_settings.confirmation_mode,
        )
        if reason is not None:
            raise ModelSettingsError(reason)
        if isinstance(self.settings_store, ProjectModelSettingsStore):
            self.settings_store.save_selection(source, settings)
            return
        self.settings_store.save(settings)
        self.config_store.select_model_source(source, settings)

    def _credential_isolation(
        self,
        settings: ModelSettings,
        *,
        model_source: str | None,
    ) -> CredentialIsolation:
        try:
            profile = settings.profile()
        except ModelSettingsError:
            profile = None
        return assess_credential_isolation(
            profile,
            self._platform_capabilities,
            model_source=model_source,
            model_connections=self._model_connections.values(),
        )

    @_serialized_state
    def _reload_model_connections(self) -> None:
        configured = self.config_store.load().additional_connections
        allowed_connection_ids = {option.connection_id for option in model_source_options(self.env)}
        loaded = active_model_connections(
            self._base_model_connections,
            configured,
            allowed_connection_ids=allowed_connection_ids,
        )
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
        connection = matching_model_connection(profile, self._model_connections.values())
        if connection is not None:
            return self._model_source_for_connection(connection)
        return "custom"

    @_serialized_state
    def _select_downloaded_local_model(
        self,
        model_id: str,
        path: Path,
        runtime_profile: str,
    ) -> None:
        choice = self._downloadable_local_model_choices.get(model_id)
        if choice is None:
            raise ModelRepositoryError(
                f"Heartwood-managed model metadata is unavailable: {model_id}"
            )
        self._select_local_model(choice, path, runtime_profile)

    def _select_local_model(
        self,
        choice: LocalModelChoice,
        path: Path,
        runtime_profile: str,
    ) -> None:
        """Select one verified local model through the shared project contract."""
        execution_model = "heartwood-managed-model"
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
        platform_qualification = choice.qualification_for(platform_id)
        self.config_store.select_local_model(
            artifact_id=choice.model_id,
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
            qualification=platform_qualification,
            minimum_gpu_count=choice.minimum_gpu_count,
            minimum_gpu_memory_bytes=choice.minimum_gpu_memory_bytes,
            recommended_ram_bytes=choice.recommended_ram_bytes,
            recommended_disk_bytes=choice.recommended_disk_bytes,
            recommended_cpu_count=choice.recommended_cpu_count,
            tool_call_parser=choice.tool_call_parser,
            tensor_parallel_size=choice.tensor_parallel_size,
            startup_seconds_min=choice.startup_seconds_min,
            startup_seconds_max=choice.startup_seconds_max,
            download_policy=choice.download_policy,
            allow_patterns=choice.allow_patterns,
            ignore_patterns=choice.ignore_patterns,
            validated_platforms=choice.validated_platforms,
            qualification_test=choice.qualification_test,
            qualification_date=(
                choice.qualification_date if platform_qualification != "unvalidated" else None
            ),
            qualification_evidence=(
                choice.qualification_evidence if platform_qualification != "unvalidated" else None
            ),
            catalog_source=choice.catalog_source,
            settings=settings,
        )
        self._reset_services()

    @_serialized_state
    def _select_transferred_local_model(
        self,
        choice: LocalModelChoice,
        path: Path,
        runtime_profile: str,
    ) -> None:
        """Register and select one bundle model through the normal managed-model path."""
        choice = self._trusted_transferred_model_choice(choice)
        previous_choice = self._local_model_choices.get(choice.model_id)
        previous_config = self.config_store.load() if self.config_store.configured else None
        self._local_model_choices[choice.model_id] = choice
        try:
            self._select_local_model(choice, path, runtime_profile)
        except Exception:
            if previous_choice is None:
                self._local_model_choices.pop(choice.model_id, None)
            else:
                self._local_model_choices[choice.model_id] = previous_choice
            self.config_store.restore(previous_config)
            raise

    def _trusted_transferred_model_choice(self, choice: LocalModelChoice) -> LocalModelChoice:
        """Reject identity collisions and never trust bundle-supplied qualification."""
        choice.validate()
        try:
            managed_model_token_budgets(choice.context_window)
        except ValueError as error:
            raise ModelRepositoryError(str(error)) from error
        existing = self._local_model_choices.get(choice.model_id)
        if existing is not None and _model_transfer_identity(choice) != _model_transfer_identity(
            existing
        ):
            raise ModelTransferError(
                f"model bundle metadata conflicts with the configured catalog: {choice.model_id}"
            )
        trusted = self._downloadable_local_model_choices.get(choice.model_id)
        if trusted is not None and _model_transfer_identity(choice) == _model_transfer_identity(
            trusted
        ):
            return replace(trusted, catalog_source="transferred")
        return replace(
            choice,
            qualification="unvalidated",
            validated_platforms=(),
            qualification_test=None,
            qualification_date=None,
            qualification_evidence=None,
        )

    def _model_transfer_warnings(self, choice: LocalModelChoice) -> tuple[str, ...]:
        """Return shared import/export guidance for one transfer model."""
        warnings = [
            "Verify the upstream license and use an institution-approved transfer channel; "
            "bundle checksums provide integrity, not transfer authorization."
        ]
        platform_id = self.config_store.load().platform_id
        if choice.qualification_for(platform_id) != "qualified":
            warnings.append(
                "This exact model configuration has not completed Heartwood qualification "
                f"for {platform_id}."
            )
        if not self._local_runtime_installed(choice.runtime):
            runtime = "NVIDIA vLLM" if choice.runtime == "vllm" else "portable CPU"
            warnings.append(
                f"The {runtime} runtime is not installed in this deployment; import is allowed, "
                "but launch will remain unavailable until a compatible runtime is provided."
            )
        return tuple(warnings)

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
                minimum_compute_capability=minimum_compute_capability_for_model(
                    model_id=choice.source_repository,
                    precision=choice.precision,
                ),
            )
        if qualification != "qualified":
            candidate_reason = "No completed Heartwood qualification exists for this platform"
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

    def _require_downloadable_local_model(self, model_id: str) -> LocalModelChoice:
        choice = self._downloadable_local_model_choices.get(model_id)
        if choice is None:
            raise ModelRepositoryError(f"unknown Heartwood-managed model: {model_id}")
        if not self._local_runtime_installed(choice.runtime):
            reason = self._local_model_availability_reason(
                choice.runtime,
                available=False,
                recommendation=None,
            )
            raise ModelRepositoryError(f"{choice.label} cannot be downloaded: {reason}")
        return choice

    def _local_runtime_available(self, runtime: str) -> bool:
        platform_id = self.config_store.load().platform_id
        installed = self._local_runtime_installed(runtime)
        if runtime == "llama-cpp":
            return installed
        if runtime != "vllm":
            return False
        if platform_id == "carina":
            return installed
        return installed and gpu_visible(self.env)

    def _local_runtime_installed(self, runtime: str) -> bool:
        executable_path = self.env.get("PATH")
        if runtime == "llama-cpp":
            return self._runtime_executable_available(Path("/opt/llama.cpp/llama-server")) or (
                executable_path is not None
                and shutil.which("llama-server", path=executable_path) is not None
            )
        if runtime != "vllm":
            return False
        if self.config_store.load().platform_id == "carina":
            return True
        return self._runtime_executable_available(Path("/opt/heartwood-vllm/bin/vllm")) or (
            executable_path is not None and shutil.which("vllm", path=executable_path) is not None
        )

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


def _clears_streaming_text(event: SessionEvent) -> bool:
    kind = str(event.kind)
    if kind == EventKind.AGENT_MESSAGE_EMITTED.value:
        return True
    if kind == EventKind.ERROR_RECORDED.value:
        return event.payload.get("affects_lifecycle") is not False
    if kind in {
        EventKind.CONFIRMATION_REQUESTED.value,
        EventKind.SESSION_PAUSED.value,
    }:
        return True
    if kind != EventKind.AGENT_LIFECYCLE_UPDATED.value:
        return False
    return event.payload.get("status") != "running"


def _starts_streaming_text(event: SessionEvent) -> bool:
    return (
        str(event.kind) == EventKind.AGENT_LIFECYCLE_UPDATED.value
        and event.payload.get("status") == "running"
    )


def _select_action_confirmation_mode(
    config: ProjectConfig,
    mode: str,
) -> ProjectConfig:
    if mode not in config.policy.allowed_action_confirmation_modes:
        msg = f"action confirmation mode is not allowed by platform policy: {mode}"
        raise ActionSettingsError(msg)
    return config.with_action_settings(config.action_settings.selecting(mode))


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
    source_description = (
        "Transferred model with verified Heartwood bundle provenance; capability and platform "
        "qualification remain specific to the recorded configuration."
        if selection.catalog_source == "transferred"
        else (
            "User-selected Hugging Face model; Heartwood has not reviewed its capabilities, "
            "license, or suitability."
        )
    )
    choice = LocalModelChoice(
        model_id=selection.artifact_id,
        label=selection.display_name,
        purpose=source_description,
        runtime=cast(LocalModelRuntime, runtime),
        source_repository=selection.source_repository,
        source_revision=selection.source_revision,
        source_path=selection.source_path,
        model_type=selection.model_type,
        size_bytes=selection.size_bytes,
        minimum_free_bytes=selection.minimum_free_bytes,
        license_posture=selection.license_posture,
        catalog_source=cast(LocalModelCatalogSource, selection.catalog_source),
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
        recommended_cpu_count=selection.recommended_cpu_count,
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
        qualification_date=selection.qualification_date,
        qualification_evidence=selection.qualification_evidence,
    )
    choice.validate()
    return choice


def _model_transfer_identity(choice: LocalModelChoice) -> tuple[object, ...]:
    """Return fields that determine transferred weights and runtime behavior."""
    return (
        choice.model_id,
        choice.label,
        choice.purpose,
        choice.runtime,
        choice.source_repository,
        choice.source_revision,
        choice.source_path,
        choice.size_bytes,
        choice.minimum_free_bytes,
        choice.license_id,
        choice.license_posture,
        choice.model_type,
        choice.context_window,
        choice.artifact_sha256,
        choice.minimum_resource_envelope,
        choice.recommended_resource_envelope,
        choice.precision,
        choice.tier,
        choice.minimum_gpu_count,
        choice.minimum_gpu_memory_bytes,
        choice.recommended_ram_bytes,
        choice.recommended_disk_bytes,
        choice.maximum_context_window,
        choice.tool_call_parser,
        choice.tensor_parallel_size,
        choice.startup_seconds_min,
        choice.startup_seconds_max,
        choice.download_policy,
        choice.allow_patterns,
        choice.ignore_patterns,
        choice.recommended_cpu_count,
    )


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
