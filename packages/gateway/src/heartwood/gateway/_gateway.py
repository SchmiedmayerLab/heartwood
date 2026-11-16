# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Session gateway orchestration and model-profile ownership."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager, nullcontext, suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING, Concatenate, Literal, Protocol, cast
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
    CommandConflictError,
    DeterministicAgentBackend,
    FileSessionStore,
    PendingActionGroup,
    SessionResult,
    SessionService,
    TokenDeltaSink,
)
from heartwood.core_adapter.action_review import effective_review_mode, is_tightening
from heartwood.core_adapter.checkpoint_evidence import SessionCheckpointEvidence
from heartwood.core_adapter.external_actions import ExternalActionAdapter
from heartwood.core_adapter.worker_attempts import current_worker_attempt
from heartwood.gateway._action_presentation import action_presentation
from heartwood.gateway._action_settings import (
    ACTION_MODE_LAUNCH_SCOPE_DESCRIPTION,
    ACTION_MODE_OPTIONS,
    ACTION_MODE_SCOPE_DESCRIPTION,
    ActionSettings,
    ActionSettingsError,
    LaunchActionSettingsStore,
)
from heartwood.gateway._credential_isolation import (
    CredentialIsolation,
    assess_credential_isolation,
)
from heartwood.gateway._credentials import CredentialStore, CredentialStoreError
from heartwood.gateway._experiment_store import ExperimentStore
from heartwood.gateway._gpu_environment import (
    GpuEnvironment,
)
from heartwood.gateway._lifecycle import GatewayOperations, gateway_operation
from heartwood.gateway._local_model_contract import (
    managed_model_native_tool_calling,
    managed_model_request_body,
)
from heartwood.gateway._local_model_lifecycle import LocalModelLifecycle
from heartwood.gateway._local_models import (
    HuggingFaceModelRepository,
)
from heartwood.gateway._model_artifacts import (
    ModelArtifactCatalog,
    load_model_artifact_catalog,
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
)
from heartwood.gateway._model_snapshots import (
    ModelSnapshot,
    ModelSnapshotCatalog,
    ModelTier,
    load_model_snapshot_catalog,
)
from heartwood.gateway._openhands_models import prepare_openhands_import
from heartwood.gateway._project import ProjectContext, ProjectStateError
from heartwood.gateway._project_activity import ProjectActivity
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
    inspect_deployment,
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
from heartwood.gateway.worker_sessions import (
    ManagedWorkerSession,
    WorkerPlanFactory,
    WorkerReconciler,
    WorkerSessionRequest,
)
from heartwood.model_policy import ModelPolicyEngine
from heartwood.persistence import DurableFileError, write_private_text_atomic
from heartwood.schemas import (
    ActionReviewMode,
    ActionSettingsResponse,
    AuditExportResponse,
    CredentialSettingsResponse,
    JsonValue,
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
from heartwood.schemas.evaluation import EvaluationRuntimeObservation
from heartwood.schemas.experiments import (
    ExperimentCollection,
    ExperimentExport,
    ExperimentExportBinding,
)
from heartwood.schemas.parallel_reviews import ParallelReviewPlan
from heartwood.schemas.python_environment import PythonEnvironmentSnapshot
from heartwood.schemas.review import (
    ResearchReviewRun,
    ReviewAssessment,
    ReviewCorrectionAssessment,
    ReviewCorrectionPlan,
    ReviewSnapshot,
    ReviewSubmission,
)
from heartwood.schemas.workflows import (
    WorkflowCatalog,
    WorkflowOutcomeStatus,
    WorkflowProjectBinding,
    WorkflowRun,
    WorkflowStageEvaluation,
)
from heartwood.session import CommandKind, EventKind, SessionCommand, SessionEvent
from heartwood.skills import (
    SkillArtifactStore,
    SkillCatalogError,
    SkillSourceRegistry,
    configured_skill_source_registry,
)

if TYPE_CHECKING:
    from heartwood.gateway._openhands_sdk import WorkerConversationContext
    from heartwood.gateway._research_evaluation import ParallelReviewPreparer
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
    model_source: str | None
    policy_profile: PolicyProfile
    local_model: LocalModelSelection | None
    managed_inference: bool


class _UnconfiguredAgentBackend:
    """Fail clearly until a model profile has been selected."""

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
    def model_id(self) -> str:
        """Return the declared model identifier used for result disclosure review."""
        return "unconfigured"

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
    def continuation_requires_model_authorization(self) -> bool:
        return False

    def bind_runtime(
        self,
        *,
        event_sink: BackendEventSink,  # noqa: ARG002
        token_sink: TokenDeltaSink,  # noqa: ARG002
    ) -> None:
        return None

    def wait_for_idle(self, timeout: float) -> bool:  # noqa: ARG002
        """An unconfigured backend cannot have background work."""
        return True

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

    def conversation_readable(self, *, session_id: str) -> bool:  # noqa: ARG002
        return False

    def submit_context(self, *, session_id: str, content: str) -> tuple[BackendEvent, ...]:
        """Use this backend's existing admission behavior for reviewed context."""
        return self.submit_turn(session_id=session_id, prompt=content)

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
        checkpoint_evidence_factory: Callable[[str], SessionCheckpointEvidence] | None = None,
        external_actions_factory: Callable[[str], ExternalActionAdapter] | None = None,
        subscription_provider: SubscriptionProvider | None = None,
        workspace_inspector: WorkspaceInspector | None = None,
        skill_source_registry: SkillSourceRegistry | None = None,
        parallel_review_preparer: ParallelReviewPreparer | None = None,
        deployment_policy: PolicyProfile | None = None,
        worker_plan_factory: WorkerPlanFactory | None = None,
        worker_reconciler: WorkerReconciler | None = None,
        backend_id: str = "auto",
    ) -> None:
        prepare_openhands_import()
        self.project = ProjectContext.current() if project is None else project
        if checkpoint_evidence_factory is not None and (
            self.project.protected_state is None or service_factory is not None
        ):
            raise ValueError(
                "Managed checkpoint evidence requires protected state and owned services"
            )
        self._checkpoint_evidence_factory = checkpoint_evidence_factory
        if worker_plan_factory is not None and (
            self.project.protected_state is None
            or service_factory is not None
            or backend_id not in {"auto", "openhands", "openhands-sdk"}
        ):
            raise ValueError(
                "Managed workers require protected state and the native gateway backend"
            )
        if external_actions_factory is not None and (
            self.project.protected_state is None
            or worker_plan_factory is None
            or service_factory is not None
        ):
            raise ValueError("Outside services require protected state and owned native workers")
        if worker_reconciler is not None and worker_plan_factory is None:
            raise ValueError("Worker reconciliation requires managed workers")
        self._external_actions_factory = external_actions_factory
        self._worker_plan_factory = worker_plan_factory
        self._worker_reconciler = worker_reconciler
        self._worker_sessions: dict[int, ManagedWorkerSession] = {}
        self._project_activity = ProjectActivity(self.project)
        self._operations = GatewayOperations(on_enter=self._project_activity.acquire_existing)
        self.sessions_root = self.project.sessions_dir
        self.env = dict(os.environ if env is None else env)
        self.backend_id = backend_id
        from heartwood.gateway._review_qualification import QUALIFICATION_ENV

        qualification_path = self.env.get(QUALIFICATION_ENV)
        if parallel_review_preparer is not None and qualification_path is not None:
            raise ValueError("Configure only one deployment review evidence source")
        self._review_qualification_path = Path(qualification_path) if qualification_path else None
        self._parallel_review_preparer = parallel_review_preparer or (
            self._qualified_review_preparer if self._review_qualification_path is not None else None
        )
        self._checkpoint_signer_registry_override = checkpoint_signer_registry
        self._checkpoint_signer_registry_cache: CheckpointSignerRegistry | None = None
        self._checkpoint_signer_factory = checkpoint_signer_factory
        # Lock order, outermost first: the state lock, the project-configuration lock, a
        # session service's locks, then the stream lock. A backend read after a service's
        # first holds none of them, so one session's worker cannot delay another session.
        self._state_lock: AbstractContextManager[object] = RLock()
        self._stream_lock = RLock()
        self._stream_epoch = uuid4().hex
        adapter = select_platform_adapter(self.env)
        self._platform_capabilities = adapter.capabilities()
        self._deployment_policy = deployment_policy
        self.config_store = ProjectConfigStore(
            self.project,
            ProjectConfig(
                platform_id=adapter.adapter_id,
                policy=deployment_policy or adapter.default_policy_profile(),
            ),
            deployment_policy=deployment_policy,
            initialize_project=self._project_activity.initialize,
        )
        self.settings_store = settings_store or ProjectModelSettingsStore(self.config_store)
        # Review can loosen only through state that research-directory content cannot write.
        self.action_settings_store = action_settings_store or (
            ProjectActionSettingsStore(self.config_store)
            if self.project.protected_state is not None
            else LaunchActionSettingsStore()
        )
        self._base_model_connections = (
            tuple(model_connections)
            if model_connections is not None
            else load_model_connections(None)
        )
        self.model_catalog_service = model_catalog_service or ModelCatalogService(env=self.env)
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
        resource_root = _runtime_resource_root()
        local_runtime_resources = resource_root / "images" / "generic" / "local-runtime"
        self.local_models = LocalModelLifecycle(
            project=self.project,
            env=self.env,
            config_store=self.config_store,
            artifact_catalog=artifact_catalog
            or load_model_artifact_catalog(local_runtime_resources / "model-catalog.toml"),
            snapshot_catalog=snapshot_catalog
            or load_model_snapshot_catalog(local_runtime_resources / "snapshots.toml"),
            model_repository=model_repository
            or HuggingFaceModelRepository(token=self.env.get("HF_TOKEN")),
            state_lock=self._state_lock,
            on_selection_changed=self._reset_services,
        )
        self.local_model_manager = self.local_models.local_model_manager
        self.model_transfer_manager = self.local_models.model_transfer_manager
        bundled_skills_dir = resource_root / "vendor" / "heartwood-skills" / "skills"
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

    @gateway_operation
    def start(self) -> None:
        """Initialize SDK modules before concurrent requests enter its import graph."""
        from importlib import import_module

        import_module("openhands.sdk")

    @gateway_operation
    @_serialized_state
    def initialize_project(self, *, interface: InterfaceKind = "web") -> StartupPlanResponse:
        """Confirm the current directory as the project and create private state."""
        self._project_activity.initialize()
        return self.startup_plan(interface=interface)

    def stop(self) -> None:
        """Close admission, drain accepted calls, and stop owned background workers."""
        self._operations.close_admission()
        self.local_model_manager.request_stop()
        self.model_transfer_manager.request_stop()
        self._operations.wait_for_idle(timeout=5)
        self._streams.close_all()
        errors: list[Exception] = []
        try:
            for close in (
                self.local_model_manager.close,
                self.model_transfer_manager.close,
                self._reset_services,
            ):
                try:
                    close()
                except Exception as error:
                    errors.append(error)
        finally:
            self.credential_store.clear_process_values()
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise ExceptionGroup("unable to stop all gateway workers", errors)
        self._project_activity.close()

    @gateway_operation
    def reconcile_workers(self) -> tuple[str, ...]:
        """Revoke worker attempts that an ended gateway process left unrevoked.

        Returns the sessions whose attempt remains fenced because revocation was not observed.
        """
        return self._reconcile_workers()

    @_serialized_state
    def _reconcile_workers(self) -> tuple[str, ...]:
        if self._worker_reconciler is None:
            return ()
        unresolved: list[str] = []
        for summary in self.session_catalog.list():
            session_id = summary.session_id
            # A session this process already serves revokes its own attempt when it closes.
            if session_id in self._services:
                continue
            try:
                retained = current_worker_attempt(
                    FileSessionStore(self.sessions_root, session_id).replay_events()
                )
                if retained is None or retained.phase == "revoked":
                    continue
                service = self._service(session_id)
                try:
                    service.revoke_worker(self._worker_reconciler)
                finally:
                    # Releasing the session keeps a later read from starting a replacement
                    # worker; only a command to the session does that.
                    self._services.pop(session_id, None)
                    self._service_configurations.pop(session_id, None)
                    with suppress(Exception):
                        self._close_service(service)
            except Exception:
                unresolved.append(session_id)
        return tuple(unresolved)

    @gateway_operation
    def handle(self, command: SessionCommand) -> SessionResult:
        """Handle one command and publish emitted events."""
        if {"experiment_export", "checkpoint_context"}.intersection(command.payload):
            raise CommandConflictError("Checkpoint bindings must be computed by the gateway")
        return self._handle_command(command)

    def _handle_command(self, command: SessionCommand) -> SessionResult:
        command_kind = str(command.kind)
        starting_workflow = (
            command_kind == CommandKind.WORKFLOW.value and command.payload.get("action") == "start"
        )
        storage_only = command_kind == CommandKind.AUDIT_EXPORT.value or starting_workflow
        reconciled = None if storage_only else self._reconcile_before_lock(command.session_id)
        with self._state_lock, self.config_store.locked():
            self._project_activity.initialize()
            if command.session_id == DEFAULT_SESSION_ID:
                self.session_catalog.default()
            else:
                self.session_catalog.ensure(command.session_id)
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
                        reconciled=reconciled,
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
                if (
                    starting_workflow
                    and not result.replayed
                    and any(event.kind == EventKind.WORKFLOW_UPDATED for event in result.events)
                ):
                    # The next service must construct its SDK with the persisted
                    # workflow's structured finish contract, not a cached default.
                    close_service = True
                return result
            finally:
                if close_service:
                    self._close_service(service)
                    if self._services.get(command.session_id) is service:
                        self._services.pop(command.session_id, None)
                        self._service_configurations.pop(command.session_id, None)

    @gateway_operation
    def sessions(self) -> SessionListResponse:
        """Return persisted sessions ordered by recent activity."""
        return api_response(
            SessionListResponse,
            {"sessions": [summary.safe_dict() for summary in self.session_catalog.list()]},
        )

    @gateway_operation
    def create_session(self, title: str | None = None) -> SessionSummaryResponse:
        """Create and return one empty session."""
        self._project_activity.initialize()
        return api_response(SessionSummaryResponse, self.session_catalog.create(title).safe_dict())

    @gateway_operation
    def default_session(self) -> SessionSummaryResponse:
        """Return the shared first session, creating it when needed."""
        self._project_activity.initialize()
        return api_response(SessionSummaryResponse, self.session_catalog.default().safe_dict())

    @gateway_operation
    def session(self, session_id: str) -> SessionSummaryResponse:
        """Return one persisted session summary."""
        return api_response(
            SessionSummaryResponse,
            self.session_catalog.get(session_id).safe_dict(),
        )

    @gateway_operation
    def rename_session(self, session_id: str, title: str) -> SessionSummaryResponse:
        """Rename one persisted session."""
        return api_response(
            SessionSummaryResponse,
            self.session_catalog.rename(session_id, title).safe_dict(),
        )

    @gateway_operation
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

    @gateway_operation
    def verify_audit(self, session_id: str) -> AuditVerification:
        """Fully verify the paired session and audit history."""
        self.session_catalog.get(session_id)
        _content, verification = FileSessionStore(
            self.sessions_root,
            session_id,
        ).verified_audit_export()
        return verification

    @gateway_operation
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

    @gateway_operation
    @_serialized_state
    def create_audit_checkpoint(
        self,
        *,
        session_id: str,
        output: Path,
        deployment_id: str,
        retention_policy_id: str,
        retain_until: str,
        include_experiments: bool = False,
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
        snapshot = self.export_experiments().jsonl if include_experiments else None
        payload: dict[str, JsonValue] = {}
        if snapshot is not None:
            binding = ExperimentExportBinding.from_content(snapshot.encode("utf-8"))
            payload["experiment_export"] = cast(
                dict[str, JsonValue], binding.model_dump(mode="json")
            )
        self._handle_command(
            SessionCommand(
                command_id=f"audit-checkpoint-{uuid4().hex}",
                session_id=session_id,
                kind=CommandKind.AUDIT_EXPORT,
                actor_id="deployment-operator",
                created_at=(
                    datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
                ),
                payload=payload,
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
            experiment_content=snapshot,
        )

    @gateway_operation
    def checkpoint_signers(self) -> tuple[CheckpointSignerProfile, ...]:
        """Return deployment-approved signer profiles without resolving credentials."""
        return self._checkpoint_signer_registry().profiles

    @gateway_operation
    def default_checkpoint_signer(self) -> CheckpointSignerProfile:
        """Return the deployment-owned default without changing project selection."""
        return self._checkpoint_signer_registry().profile()

    @gateway_operation
    def active_checkpoint_signer(self) -> CheckpointSignerProfile:
        """Resolve the project selection or deployment-owned default profile."""
        selected = self.config_store.load().audit_settings.signer_profile
        return self._checkpoint_signer_registry().profile(selected)

    @gateway_operation
    def select_checkpoint_signer(self, profile_id: str | None) -> CheckpointSignerProfile:
        """Persist a project selection after deployment-registry validation."""
        registry = self._checkpoint_signer_registry()
        profile = registry.profile(profile_id)
        self.config_store.select_checkpoint_signer(profile_id)
        return profile

    @gateway_operation
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
            if profile.trusted_ca_bundle is not None:
                self._deployment_owned_path(profile.trusted_ca_bundle, label="signer CA bundle")
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

    @gateway_operation
    def workspace_tree(
        self,
        *,
        path: str = ".",
        depth: int | None = None,
    ) -> WorkspaceTreeResponse:
        """Return the bounded project tree shared by every interface."""
        self._project_activity.initialize()
        return self.workspace_inspector.tree(path, depth=depth)

    @gateway_operation
    def workspace_file(self, *, path: str) -> WorkspaceFileResponse:
        """Return one bounded read-only project file."""
        self._project_activity.initialize()
        return self.workspace_inspector.file(path)

    @gateway_operation
    def workspace_changes(self, *, session_id: str) -> WorkspaceChangesResponse:
        """Return Git or structured session-derived project changes."""
        projection = self.session_projection(session_id=session_id)
        return self.workspace_inspector.changes(projection)

    @gateway_operation
    def workspace_diff(
        self,
        *,
        session_id: str,
        path: str,
    ) -> WorkspaceDiffResponse:
        """Return one bounded project diff."""
        projection = self.session_projection(session_id=session_id)
        return self.workspace_inspector.diff(projection, path)

    @gateway_operation
    @_serialized_state
    def replay_events(
        self,
        *,
        session_id: str,
        after_sequence: int | None = None,
    ) -> tuple[SessionEvent, ...]:
        """Replay persisted events for a session."""
        self._project_activity.initialize()
        events = FileSessionStore(self.sessions_root, session_id).replay_events()
        return (
            events
            if after_sequence is None
            else tuple(event for event in events if event.sequence > after_sequence)
        )

    @gateway_operation
    def session_projection(self, *, session_id: str) -> SessionProjection:
        """Return the sole interface projection for one session."""
        return self._session_snapshot(session_id=session_id).projection

    @gateway_operation
    def wait_for_session_idle(self, *, session_id: str, timeout: float = 0) -> bool:
        """Observe a settled session before evaluating it or admitting a new stage.

        Waiting holds neither gateway nor session command locks, so a concurrent
        pause remains available. Replacing the service invalidates this observation.
        """
        with self._state_lock:
            service = self._service(session_id)
        if not service.wait_for_idle(timeout):
            return False
        reconciled = self._reconcile_before_lock(session_id)
        with self._state_lock:
            if self._services.get(session_id) is not service or not service.wait_for_idle(0):
                return False
            if reconciled is not service:
                service.reconcile()
            return True

    @gateway_operation
    @_serialized_state
    def evaluation_observation(self, *, session_id: str) -> EvaluationRuntimeObservation:
        """Capture the session's client runtime, distinct from declared server metadata."""
        return self._evaluation_observation(self._service(session_id))

    @gateway_operation
    @_serialized_state
    def bind_evaluation_observer(
        self, *, session_id: str
    ) -> Callable[[str], EvaluationRuntimeObservation]:
        """Bind a read-only observer for admission callbacks in an already owned session.

        Calling the returned reader does not acquire gateway or session command
        locks. Replacing or closing the service revokes it; observation neither
        acquires ownership nor grants authority to execute work.
        """
        service = self._service(session_id)

        return self._owned_evaluation_observer(session_id, service)

    def _owned_evaluation_observer(
        self, session_id: str, service: SessionService
    ) -> Callable[[str], EvaluationRuntimeObservation]:
        """Construct an observation capability without acquiring locks or session ownership."""

        def owned() -> bool:
            return self._services.get(session_id) is service and service.store.owns_writer

        if not owned():
            raise ValueError("Runtime observation requires an owned session")

        def observe(requested_session_id: str) -> EvaluationRuntimeObservation:
            if requested_session_id != session_id or not owned():
                raise ValueError("Runtime observation no longer owns the requested session")
            result = self._evaluation_observation(service)
            if not owned():
                raise ValueError("Session ownership changed during runtime observation")
            return result

        return observe

    def _qualified_review_preparer(
        self, run: WorkflowRun, snapshot: ReviewSnapshot, session_id: str, now: datetime
    ) -> ParallelReviewPlan:
        """Reassess deployment evidence without reentering gateway or native agent locks."""
        from heartwood.gateway._review_qualification import (
            load_review_qualifications,
            qualified_review_plan,
        )

        service = self._services.get(session_id)
        path = self._review_qualification_path
        if service is None or path is None:
            raise ValueError("Qualified review requires its owned runtime and deployment evidence")
        observe = self._owned_evaluation_observer(session_id, service)
        runtime = observe(session_id)
        result = qualified_review_plan(
            evidence=load_review_qualifications(path, project_root=self.project.root),
            project_root=self.project.root,
            run=run,
            snapshot=snapshot,
            session_id=session_id,
            runtime=runtime,
            now=now,
        )
        if observe(session_id) != runtime:
            raise ValueError("Review runtime changed while reading deployment evidence")
        return result

    @staticmethod
    def _evaluation_observation(service: SessionService) -> EvaluationRuntimeObservation:
        """Read one service's actual configuration without entering gateway state machinery."""
        from heartwood.gateway._openhands_sdk import OpenHandsSdkBackend

        policy = json.dumps(
            service.policy_profile.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        )
        digest = hashlib.sha256(policy.encode()).hexdigest()
        backend = service.backend
        if isinstance(backend, OpenHandsSdkBackend):
            return backend.evaluation_observation(
                platform=service.platform_adapter.adapter_id,
                policy_fingerprint=digest,
                action_review=service.action_review_mode(),
            )
        return EvaluationRuntimeObservation(
            backend=backend.backend_id,
            source=(
                "deterministic"
                if isinstance(backend, DeterministicAgentBackend)
                else "unconfigured"
                if isinstance(backend, _UnconfiguredAgentBackend)
                else "injected"
            ),
            request_model=None,
            openhands_version=None,
            model_options_fingerprint=None,
            platform=service.platform_adapter.adapter_id,
            policy_fingerprint=digest,
            action_review=service.action_review_mode(),
        )

    @gateway_operation
    def research_workflows(self) -> WorkflowCatalog:
        """Return maintained workflow choices and runtime support without starting work."""
        from heartwood.gateway._research_evaluation import ResearchStageEvaluator

        return ResearchStageEvaluator.catalog()

    @gateway_operation
    def verification_environment(self, *, python: str | None = None) -> PythonEnvironmentSnapshot:
        """Inspect server Python or an explicitly selected interpreter, without project writes."""
        from heartwood.gateway.python_environment import inspect_verification_environment

        if python is None:
            return inspect_verification_environment()
        executable = Path(python).expanduser()
        if not executable.is_absolute():
            executable = self.project.root / executable
        return inspect_verification_environment(executable)

    @gateway_operation
    @_serialized_state
    def prepare_research_review(self, artifacts: Mapping[str, str]) -> ReviewSnapshot:
        """Capture explicitly selected review context without model work or state mutation."""
        from heartwood.gateway._research_review import ResearchReviewEvaluator

        return ResearchReviewEvaluator(self.workspace_inspector).prepare(artifacts)

    @gateway_operation
    @_serialized_state
    def assess_research_review(
        self, snapshot: ReviewSnapshot, submissions: Sequence[ReviewSubmission]
    ) -> ReviewAssessment:
        """Independently assess associated reviewer proposals without authorizing corrections."""
        from heartwood.gateway._research_review import ResearchReviewEvaluator

        return ResearchReviewEvaluator(self.workspace_inspector).assess(snapshot, submissions)

    @gateway_operation
    @_serialized_state
    def prepare_research_correction(
        self, review: ResearchReviewRun, *, output_directory: str
    ) -> ReviewCorrectionPlan:
        """Declare fresh outputs for verified defects; do not allocate or authorize execution."""
        from heartwood.gateway._research_review import ResearchReviewEvaluator

        return ResearchReviewEvaluator(self.workspace_inspector).prepare_correction(
            review, output_directory=output_directory
        )

    @gateway_operation
    @_serialized_state
    def assess_research_correction(
        self, review: ResearchReviewRun, plan: ReviewCorrectionPlan
    ) -> ReviewCorrectionAssessment:
        """Recheck declared correction bytes without attesting execution or scientific validity."""
        from heartwood.gateway._research_review import ResearchReviewEvaluator

        return ResearchReviewEvaluator(self.workspace_inspector).assess_correction(review, plan)

    @gateway_operation
    @_serialized_state
    def experiment_records(self) -> ExperimentCollection:
        """Synchronize committed stage records and query the shared project provenance store."""
        store = self._experiment_store()
        if store is None:
            return ExperimentCollection()
        return ExperimentCollection(runs=store.runs())

    @gateway_operation
    @_serialized_state
    def export_experiments(self) -> ExperimentExport:
        """Return a verified scientific export without model work or new observations."""
        store = self._experiment_store()
        content = store.export() if store is not None else b""
        return ExperimentExport(sha256=hashlib.sha256(content).hexdigest(), jsonl=content.decode())

    def _experiment_store(self) -> ExperimentStore | None:
        from heartwood.core_adapter.workflow_provenance import workflow_experiment_events

        if not self.project.state_exists():
            return None
        self._project_activity.initialize()
        store = ExperimentStore(self.project.state_root / "experiments.jsonl")
        store.synchronize(
            record
            for summary in sorted(self.session_catalog.list(), key=lambda item: item.session_id)
            for record in workflow_experiment_events(
                FileSessionStore(self.sessions_root, summary.session_id).replay_events()
            )
        )
        return store

    @gateway_operation
    @_serialized_state
    def prepare_research_workflow(
        self, workflow_id: str, *, inputs: Mapping[str, str], output_directory: str
    ) -> WorkflowProjectBinding:
        """Bind project-local workflow inputs without starting model or tool work."""
        from heartwood.gateway._research_evaluation import ResearchStageEvaluator

        return ResearchStageEvaluator(self.workspace_inspector).prepare(
            workflow_id, inputs=inputs, output_directory=output_directory
        )

    @gateway_operation
    @_serialized_state
    def evaluate_research_stage(
        self,
        binding: WorkflowProjectBinding,
        stage_id: str,
        *,
        model_status: WorkflowOutcomeStatus | None,
    ) -> WorkflowStageEvaluation:
        """Evaluate current artifacts without granting stage or tool approval."""
        from heartwood.gateway._research_evaluation import ResearchStageEvaluator

        return ResearchStageEvaluator(self.workspace_inspector).evaluate(
            binding, stage_id, model_status=model_status
        )

    @gateway_operation
    @_serialized_state
    def persisted_session_projection(self, *, session_id: str) -> SessionProjection:
        """Project committed Heartwood events without reconciling OpenHands state."""
        with self._stream_lock:
            self._project_activity.initialize()
            events = FileSessionStore(self.sessions_root, session_id).replay_events()
            return project_session(
                events,
                session_id=session_id,
                streaming_text=self._streaming_text.get(session_id, ""),
                stream_epoch=self._stream_epoch,
                stream_revision=self._stream_revisions.get(session_id, 0),
                parallel_reviews_available=self._parallel_review_preparer is not None,
            )

    @gateway_operation
    def session_snapshot(
        self,
        *,
        session_id: str,
        after_sequence: int | None = None,
    ) -> GatewaySessionSnapshot:
        """Return events and projection from one serialized gateway snapshot."""
        return self._session_snapshot(
            session_id=session_id,
            after_sequence=after_sequence,
        )

    @gateway_operation
    def websocket(
        self,
        *,
        session_id: str,
        after_sequence: int | None = None,
    ) -> GatewayEventStream:
        """Connect an event stream with replay."""
        with self._reconciled_session(session_id) as all_events, self._stream_lock:
            snapshot = self._snapshot_from_events_locked(
                session_id=session_id,
                all_events=all_events,
                after_sequence=after_sequence,
            )
            return self._streams.connect(
                session_id=session_id,
                replay_events=snapshot.events,
            )

    @gateway_operation
    def open_event_stream(
        self,
        *,
        session_id: str,
        after_sequence: int | None = None,
    ) -> tuple[GatewayEventStream, GatewaySessionSnapshot]:
        """Connect a stream and capture its first coherent snapshot atomically."""
        with self._reconciled_session(session_id) as all_events, self._stream_lock:
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

    @gateway_operation
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

    @gateway_operation
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

    @gateway_operation
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

    @gateway_operation
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

    @gateway_operation
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

    @gateway_operation
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

    @gateway_operation
    def deployment_readiness(self) -> DeploymentReadiness:
        """Inspect the project with every resolvable credential binding."""
        return inspect_deployment(
            self.project,
            self._credential_environment(strict=False),
            bundled_skills_dir=self.skill_manager.bundled_dir,
            bundled_skill_digests=self.skill_manager.bundled_digests,
        )

    @gateway_operation
    def project_readiness(self) -> ProjectReadinessResponse:
        """Return content-free project diagnostics for presentation adapters."""
        return api_response(ProjectReadinessResponse, self.deployment_readiness().safe_dict())

    @gateway_operation
    def platform_capabilities(self) -> PlatformCapabilitiesResponse:
        """Return capabilities owned by the detected platform adapter."""
        return api_response(
            PlatformCapabilitiesResponse,
            self._platform_capabilities.safe_dict(),
        )

    @gateway_operation
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
            bundled_skills_dir=self.skill_manager.bundled_dir,
            bundled_skill_digests=self.skill_manager.bundled_digests,
        )

    @gateway_operation
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

    @gateway_operation
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

    @gateway_operation
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

    @gateway_operation
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

    @gateway_operation
    def action_settings(self) -> ActionSettingsResponse:
        """Return the review mode for new action sets and what the deployment allows."""
        try:
            policy_allowed = self.config_store.load().policy.allowed_action_review_modes
            selected = self.action_settings_store.load().review_mode
            isolated = self._credentials_isolated(
                self.settings_store.load(), model_source=self.config_store.load().model_source
            )
        except (ProjectConfigError, ModelSettingsError) as error:
            raise ActionSettingsError(str(error)) from error
        sandboxed = self._worker_plan_factory is not None
        blocking_sessions = self._active_service_session_ids()
        stored = isinstance(self.action_settings_store, ProjectActionSettingsStore)
        return api_response(
            ActionSettingsResponse,
            {
                "schema_version": "heartwood.action-settings.v1",
                "review_mode": effective_review_mode(
                    selected,
                    allowed=policy_allowed,
                    sandboxed=sandboxed,
                    credentials_isolated=isolated,
                ),
                "default_mode": effective_review_mode(
                    None,
                    allowed=policy_allowed,
                    sandboxed=sandboxed,
                    credentials_isolated=isolated,
                ),
                "scope": "project" if stored else "launch",
                "scope_description": (
                    ACTION_MODE_SCOPE_DESCRIPTION
                    if stored
                    else ACTION_MODE_LAUNCH_SCOPE_DESCRIPTION
                ),
                "presentation": action_presentation(),
                "change_allowed": not blocking_sessions,
                "change_blocked_reason": (
                    None
                    if not blocking_sessions
                    else "Finish or resolve active session work before reducing review. "
                    "A stricter mode can be selected at any time."
                ),
                "modes": [
                    {
                        **option.safe_dict(),
                        "allowed": (
                            reason := _review_mode_unavailable_reason(
                                option.mode,
                                allowed=policy_allowed,
                                sandboxed=sandboxed,
                                credentials_isolated=isolated,
                            )
                        )
                        is None,
                        "unavailable_reason": reason,
                    }
                    for option in ACTION_MODE_OPTIONS
                ],
            },
        )

    @gateway_operation
    @_serialized_state
    def select_action_review_mode(self, mode: str) -> ActionSettingsResponse:
        """Select a review mode within the deployment ceiling and execution boundary."""
        current = self.action_settings()["review_mode"]
        # Only a stored selection touches project state; a launch selection stays in memory.
        stored = isinstance(self.action_settings_store, ProjectActionSettingsStore)
        try:
            with self.config_store.locked() if stored else nullcontext():
                policy_allowed = self.config_store.load().policy.allowed_action_review_modes
                settings = self.action_settings_store.load().selecting(mode)
                selected = cast(ActionReviewMode, mode)
                reason = _review_mode_unavailable_reason(
                    selected,
                    allowed=policy_allowed,
                    sandboxed=self._worker_plan_factory is not None,
                    credentials_isolated=self._credentials_isolated(
                        self.settings_store.load(),
                        model_source=self.config_store.load().model_source,
                    ),
                )
                if reason is not None:
                    raise ActionSettingsError(reason)
                if not is_tightening(current, selected) and self._active_service_session_ids():
                    raise ActionSettingsError(
                        "action review can be reduced only while no session is active"
                    )
                self.action_settings_store.save(settings)
        except (ProjectConfigError, ModelSettingsError) as error:
            raise ActionSettingsError(str(error)) from error
        return self.action_settings()

    @gateway_operation
    @_serialized_state
    def save_model_profile(self, profile: ModelProfile) -> ModelSettingsResponse:
        """Add or replace a non-secret profile and reset active services."""
        profile = align_model_profile_request_endpoint(profile)
        if profile.profile_id in _RESERVED_MODEL_PROFILE_IDS:
            raise ModelSettingsError(
                f"model profile id is reserved by Heartwood: {profile.profile_id}"
            )
        settings = self.settings_store.load().with_profile(profile)
        self.settings_store.save(settings)
        self._reset_services()
        return self.model_settings()

    @gateway_operation
    @_serialized_state
    def select_model_profile(self, profile_id: str) -> ModelSettingsResponse:
        """Select a profile and reset active services."""
        settings = self.settings_store.load().selecting(profile_id)
        self._save_model_selection(self._model_source_for_profile(settings.profile()), settings)
        self._reset_services()
        return self.model_settings()

    @gateway_operation
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

    @gateway_operation
    def validate_model_profile(
        self,
        profile_id: str | None = None,
    ) -> ModelValidationResponse:
        """Validate credential availability and platform route authorization."""
        profile = self.settings_store.load().profile(profile_id)
        policy_profile = self._policy_profile()
        purpose = (
            "selected Heartwood-managed model"
            if profile.profile_id == "heartwood"
            else f"model profile {profile.profile_id}"
        )
        decision = ModelPolicyEngine(policy_profile).evaluate(
            endpoint=profile.policy_endpoint,
            capability_tier=profile.capability_tier,
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
                "action_review_mode": self.action_settings()["review_mode"],
                "policy_decision": decision.model_dump(mode="json"),
            },
        )

    @gateway_operation
    def model_artifacts(self) -> ModelArtifactsResponse:
        """Return normalized local choices, source metadata, and download status."""
        return self.local_models.model_artifacts()

    @gateway_operation
    def gpu_environment(self, *, refresh: bool = False) -> GpuEnvironment:
        """Return the shared GPU and scheduler inventory for this deployment."""
        return self.local_models.gpu_environment(refresh=refresh)

    @gateway_operation
    def recommend_managed_model(
        self,
        *,
        maximum_tier: ModelTier,
        requested_gpus: int | None = None,
        gpu_environment: GpuEnvironment | None = None,
    ) -> ModelSnapshot | None:
        """Choose one qualified catalog model for the detected resource envelopes."""
        return self.local_models.recommend_managed_model(
            maximum_tier=maximum_tier,
            requested_gpus=requested_gpus,
            gpu_environment=gpu_environment,
        )

    @gateway_operation
    def inspect_model_repository(
        self,
        repository: str,
        *,
        revision: str | None = None,
    ) -> ModelRepositoryPlanResponse:
        """Build one automatic download plan without downloading model weights."""
        return self.local_models.inspect_model_repository(repository, revision=revision)

    @gateway_operation
    def download_local_model(self, model_id: str) -> ModelDownloadResponse:
        """Start a known local-model download into project storage."""
        self._project_activity.initialize()
        return self.local_models.download_local_model(model_id)

    @gateway_operation
    def download_custom_local_model(
        self,
        repository: str,
        *,
        revision: str | None = None,
    ) -> ModelDownloadResponse:
        """Resolve and start one user-selected Hugging Face model download."""
        self._project_activity.initialize()
        return self.local_models.download_custom_local_model(repository, revision=revision)

    @gateway_operation
    def download_local_model_now(
        self,
        model_id: str,
        *,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> Path:
        """Download and verify a known model, selecting it when agent-compatible."""
        self._project_activity.initialize()
        return self.local_models.download_local_model_now(
            model_id,
            progress_callback=progress_callback,
        )

    @gateway_operation
    def download_custom_local_model_now(
        self,
        repository: str,
        *,
        revision: str | None = None,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> Path:
        """Resolve, download, verify, and select one user-selected model."""
        self._project_activity.initialize()
        return self.local_models.download_custom_local_model_now(
            repository,
            revision=revision,
            progress_callback=progress_callback,
        )

    @gateway_operation
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
        self._project_activity.initialize()
        return self.local_models.import_local_model(
            source,
            source_repository=source_repository,
            source_revision=source_revision,
            license_posture=license_posture,
            context_window=context_window,
        )

    @gateway_operation
    def inspect_local_model_bundle(self, path: Path) -> ModelTransferPlanResponse:
        """Inspect one portable model bundle without changing project state."""
        return self.local_models.inspect_local_model_bundle(path)

    @gateway_operation
    def export_local_model(self, path: Path) -> ModelTransferResponse:
        """Start a verified export of the selected Heartwood-managed model."""
        self._project_activity.initialize()
        return self.local_models.export_local_model(path)

    @gateway_operation
    def import_local_model_bundle(
        self,
        path: Path,
        *,
        approved: bool,
        manifest_sha256: str,
    ) -> ModelTransferResponse:
        """Start an atomic model-bundle import after explicit review."""
        self._project_activity.initialize()
        return self.local_models.import_local_model_bundle(
            path,
            approved=approved,
            manifest_sha256=manifest_sha256,
        )

    @gateway_operation
    def cancel_model_transfer(self, transfer_id: str) -> ModelTransferResponse:
        """Request cancellation of one active model transfer."""
        return self.local_models.cancel_model_transfer(transfer_id)

    @gateway_operation
    def model_transfer_status(self, transfer_id: str) -> ModelTransferResponse:
        """Return one shared model-transfer status snapshot."""
        return self.local_models.model_transfer_status(transfer_id)

    @gateway_operation
    def skill_settings(self) -> SkillSettingsResponse:
        """Return the shared bundled, installed, and signed-catalog projection."""
        return api_response(
            SkillSettingsResponse,
            {"skills": [summary.safe_dict() for summary in self.skill_manager.summaries()]},
        )

    @gateway_operation
    @_serialized_state
    def refresh_skills(self, source_id: str | None = None) -> SkillSettingsResponse:
        """Refresh signed Skill sources and apply their current revocation state."""
        self._project_activity.initialize()
        try:
            self.skill_manager.refresh(source_id)
        finally:
            # A failure after a persisted revocation must not leave sessions with the Skill.
            self._reset_services()
        return self.skill_settings()

    @gateway_operation
    def specialist_settings(self) -> SpecialistSettingsResponse:
        """Return the validated research-specialist catalog."""
        return api_response(
            SpecialistSettingsResponse,
            self._specialist_catalog().safe_dict(),
        )

    @gateway_operation
    def inspect_skill(self, name: str, *, source_id: str | None = None) -> SkillSummaryResponse:
        """Verify one signed catalog entry without installing its archive."""
        summary = self.skill_manager.inspect_catalog(name, source_id=source_id)
        return api_response(SkillSummaryResponse, summary.safe_dict())

    @gateway_operation
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
        self._project_activity.initialize()
        self.skill_manager.install_catalog(
            name,
            source_id=source_id,
            expected_tree_sha256=expected_tree_sha256,
            approved=approved,
        )
        self._reset_services()
        return self.skill_settings()

    @gateway_operation
    def inspect_local_skill(self, source: Path) -> SkillSummaryResponse:
        """Verify one advanced local Skill without assigning repository review."""
        summary = self.skill_manager.inspect_local(self._project_skill_source(source))
        return api_response(SkillSummaryResponse, summary.safe_dict())

    @gateway_operation
    @_serialized_state
    def install_local_skill(
        self,
        source: Path,
        *,
        expected_tree_sha256: str,
        approved: bool,
    ) -> SkillSettingsResponse:
        """Install one digest-pinned local, unreviewed Skill after approval."""
        self._project_activity.initialize()
        self.skill_manager.install_local(
            self._project_skill_source(source),
            expected_tree_sha256=expected_tree_sha256,
            approved=approved,
        )
        self._reset_services()
        return self.skill_settings()

    def _project_skill_source(self, source: Path) -> Path:
        try:
            return self.project.require_project_path(source)
        except ProjectStateError as error:
            raise SkillSettingsError(str(error)) from error

    @gateway_operation
    @_serialized_state
    def remove_skill(self, name: str) -> SkillSettingsResponse:
        """Remove one installed extension and reset active conversations."""
        try:
            self.skill_manager.remove(name)
        finally:
            # A failure after the index change must not leave sessions with the Skill.
            self._reset_services()
        return self.skill_settings()

    @_serialized_state
    def _service(self, session_id: str) -> SessionService:
        configuration = self._service_configuration()
        service = self._services.get(session_id)
        if service is not None and self._service_configurations.get(session_id) != configuration:
            self._close_service(service)
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
        return _ServiceConfiguration(
            model_settings=model_settings,
            model_source=config.model_source,
            policy_profile=config.policy,
            local_model=config.local_model,
            managed_inference=config.model_source == "heartwood",
        )

    def _default_service(
        self,
        session_id: str,
        configuration: _ServiceConfiguration,
    ) -> SessionService:
        from heartwood.gateway._research_evaluation import ResearchStageEvaluator

        checkpoint_evidence = (
            self._checkpoint_evidence_factory(session_id)
            if self._checkpoint_evidence_factory is not None
            else None
        )
        if checkpoint_evidence is not None and (
            checkpoint_evidence.records.scope.project_id != self.project.project_id
            or checkpoint_evidence.records.scope.session_id != session_id
        ):
            raise ValueError("Checkpoint evidence does not authorize this project and session")
        worker: ManagedWorkerSession | None = None

        def worker_context() -> WorkerConversationContext:
            if worker is None:
                raise ValueError("Managed worker session is unavailable")
            return worker.context()

        backend = self._backend(
            model_settings=configuration.model_settings,
            selected_model=configuration.local_model,
            managed_inference=configuration.managed_inference,
            session_id=session_id,
            worker_context=worker_context if self._worker_plan_factory is not None else None,
        )
        service = SessionService.local_default(
            self.sessions_root,
            session_id=session_id,
            backend=backend,
            policy_profile=configuration.policy_profile,
            env=self.env,
            worker_project_id=self.project.project_id,
            checkpoint_evidence=checkpoint_evidence,
            action_review_mode=lambda: self.action_settings_store.load().review_mode,
            # The boundary of the route this session's backend was built with, not a later one.
            credentials_isolated=lambda: self._credentials_isolated(
                configuration.model_settings, model_source=configuration.model_source
            ),
            external_actions=(
                self._external_actions_factory(session_id)
                if self._external_actions_factory is not None
                else None
            ),
            workflow_evaluator=ResearchStageEvaluator(
                self.workspace_inspector,
                parallel_review_preparer=self._parallel_review_preparer,
            ),
            event_sink=lambda events: self._publish_background_events(
                session_id=session_id,
                events=events,
            ),
            token_sink=lambda delta: self._publish_token_delta(
                session_id=session_id,
                delta=delta,
            ),
        )
        if self._worker_plan_factory is not None:
            from heartwood.gateway._openhands_sdk import OpenHandsSdkBackend

            if isinstance(backend, OpenHandsSdkBackend):
                assert backend.specialist_catalog is not None
                worker = ManagedWorkerSession(
                    service,
                    WorkerSessionRequest(
                        project=self.project,
                        session_id=session_id,
                        profile=backend.profile,
                        policy=service.policy_profile,
                        skills=self.skill_manager,
                        specialists=backend.specialist_catalog,
                    ),
                    self._worker_plan_factory,
                )
                self._worker_sessions[id(service)] = worker
        if self._parallel_review_preparer is not None:
            from heartwood.gateway._openhands_sdk import OpenHandsSdkBackend
            from heartwood.gateway._workflow_review_execution import bind_workflow_review_execution

            if isinstance(backend, OpenHandsSdkBackend):
                bind_workflow_review_execution(backend, service)
        return service

    def _storage_service(self, session_id: str) -> SessionService:
        """Build an uncached service for commands that only access durable state."""
        from heartwood.gateway._research_evaluation import ResearchStageEvaluator

        configuration = self._service_configuration()
        return SessionService.local_default(
            self.sessions_root,
            session_id=session_id,
            backend=_UnconfiguredAgentBackend(),
            policy_profile=configuration.policy_profile,
            env=self.env,
            worker_project_id=self.project.project_id,
            workflow_evaluator=ResearchStageEvaluator(
                self.workspace_inspector,
                parallel_review_preparer=self._parallel_review_preparer,
            ),
        )

    def _backend(
        self,
        *,
        model_settings: ModelSettings,
        selected_model: LocalModelSelection | None,
        session_id: str,
        worker_context: Callable[[], WorkerConversationContext] | None = None,
        managed_inference: bool = False,
    ) -> AgentBackend:
        backend_id = self.backend_id
        if backend_id in {"deterministic", "deterministic-local"}:
            return DeterministicAgentBackend(
                persistence_path=(self.sessions_root / session_id / ".deterministic-backend.json"),
            )
        if backend_id not in {"auto", "openhands", "openhands-sdk"}:
            msg = f"unsupported agent backend: {backend_id}"
            raise ValueError(msg)
        try:
            profile = model_settings.profile()
        except ModelSettingsError:
            return _UnconfiguredAgentBackend()
        selected_model = selected_model if profile.is_local else None
        from heartwood.core_adapter.workflow_runtime import workflow_run
        from heartwood.gateway._openhands_sdk import OpenHandsSdkBackend

        return OpenHandsSdkBackend(
            profile=profile,
            workspace=self.project.root,
            verified_skills=self.skill_manager.active_skill_manifests(),
            specialist_catalog=self._specialist_catalog(),
            persistence_dir=self.sessions_root / session_id / "openhands",
            conversation_key=f"{self.project.root}#{session_id}",
            credential_environment_names=tuple(
                configured_profile.api_key_env
                for configured_profile in model_settings.profiles
                if configured_profile.credential_kind == "environment"
                and configured_profile.api_key_env is not None
            ),
            env={} if worker_context is not None else self._credential_environment(strict=False),
            worker_context=worker_context,
            managed_inference=managed_inference,
            llm_extra_body=managed_model_request_body(
                selected_model.model_type if selected_model is not None else None
            ),
            native_tool_calling=(
                managed_model_native_tool_calling(selected_model.tool_call_parser)
                if selected_model is not None
                else None
            ),
            structured_task_outcomes=(
                workflow_run(FileSessionStore(self.sessions_root, session_id).replay_events())
                is not None
            ),
        )

    def _specialist_catalog(self) -> SpecialistCatalog:
        catalog = self._specialist_catalog_cache
        if catalog is not None:
            return catalog
        from heartwood.gateway._specialists import load_specialist_catalog

        catalog = load_specialist_catalog(
            _runtime_resource_root() / "agents" / "verified",
            self.skill_manager.bundled_dir,
            platform_id=self.skill_manager.platform_id,
            bundled_digests=self.skill_manager.bundled_digests,
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

    def _session_snapshot(
        self,
        *,
        session_id: str,
        after_sequence: int | None = None,
    ) -> GatewaySessionSnapshot:
        with self._reconciled_session(session_id) as all_events, self._stream_lock:
            return self._snapshot_from_events_locked(
                session_id=session_id,
                all_events=all_events,
                after_sequence=after_sequence,
            )

    def _reconcile_before_lock(self, session_id: str) -> SessionService | None:
        """Read a served session's backend before the state lock is taken.

        The session's worker answers this read, so it must not delay other sessions or
        settings. A service's first read can launch its worker and stays under the lock,
        as does the first read of a service that replaces this one meanwhile.
        """
        with self._state_lock:
            service = self._services.get(session_id)
        if service is None or not service.backend_opened:
            return None
        try:
            service.reconcile()
        except RuntimeError:
            with self._state_lock:
                if self._services.get(session_id) is service:
                    raise
            return None
        return service

    @contextmanager
    def _reconciled_session(self, session_id: str) -> Iterator[tuple[SessionEvent, ...]]:
        """Hold the state lock with one session's reconciled durable events."""
        reconciled = self._reconcile_before_lock(session_id)
        with self._state_lock:
            yield self._reconciled_session_events(session_id=session_id, reconciled=reconciled)

    def _reconciled_session_events(
        self,
        *,
        session_id: str,
        service: SessionService | None = None,
        reconciled: SessionService | None = None,
    ) -> tuple[SessionEvent, ...]:
        """Resolve the service and reconcile durable state without holding the stream lock."""
        self._project_activity.initialize()
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
            if active_service is not reconciled:
                active_service.reconcile()
            return active_service.replay_events()
        finally:
            if close_service:
                self._close_service(active_service)

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
                    parallel_reviews_available=self._parallel_review_preparer is not None,
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
        if self._deployment_policy is not None:
            # Route selection never adds authority to an operator-owned ceiling.
            # Existing catalog and inference admission check the current policy.
            return
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
        decision = ModelPolicyEngine(catalog_policy).evaluate(
            endpoint=connection.catalog_endpoint,
            capability_tier="supervised",
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
                self._close_service(service)
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

    def _close_service(self, service: SessionService) -> None:
        worker = self._worker_sessions.get(id(service))
        if worker is None:
            service.close()
        else:
            worker.close()
            self._worker_sessions.pop(id(service), None)

    @_serialized_state
    def _save_model_selection(self, source: str | None, settings: ModelSettings) -> None:
        if isinstance(self.settings_store, ProjectModelSettingsStore):
            self.settings_store.save_selection(source, settings)
            return
        self.settings_store.save(settings)
        self.config_store.select_model_source(source, settings)

    def _credentials_isolated(self, settings: ModelSettings, *, model_source: str | None) -> bool:
        """Return whether a model route keeps its credential from the agent tool runtime."""
        if self.backend_id in {"deterministic", "deterministic-local"}:
            # The deterministic backend calls no model and holds no credential.
            return True
        return self._credential_isolation(
            settings, model_source=model_source
        ).unattended_actions_allowed

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


def _runtime_resource_root() -> Path:
    module_dir = Path(__file__).resolve().parent
    packaged = module_dir / "_bundled"
    if packaged.is_dir():
        return packaged
    if len(module_dir.parents) > 4:
        source_root = module_dir.parents[4]
        if source_root / "packages/gateway/src/heartwood/gateway" == module_dir:
            return source_root
    raise RuntimeError("Gateway package is missing bundled runtime resources")


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


def _review_mode_unavailable_reason(
    mode: ActionReviewMode, *, allowed: Sequence[str], sandboxed: bool, credentials_isolated: bool
) -> str | None:
    """Explain why the execution boundary, ceiling or credential boundary excludes a mode.

    A launch without isolated workers can never apply sandbox review, whatever its policy
    lists, so that is the reason reported; the policy is named only where it is the limit.
    """
    if mode == "review-outside-effects" and not sandboxed:
        return "Requires sessions that run in an isolated worker."
    if mode not in allowed:
        return "Not permitted by this deployment's policy."
    if mode != "review-all" and not sandboxed and not credentials_isolated:
        return (
            "Unavailable until the selected model route is credential-free or platform-isolated: "
            "a model credential is otherwise not isolated from the agent tool runtime."
        )
    return None


def _catalog_entry(catalog: ModelCatalog, model_id: str) -> ModelCatalogEntry:
    selected = model_id.strip()
    for entry in catalog.models:
        if selected in {entry.model_id, entry.execution_model}:
            return entry
    raise ModelCatalogError(f"model is not present in the discovered catalog: {model_id}")
