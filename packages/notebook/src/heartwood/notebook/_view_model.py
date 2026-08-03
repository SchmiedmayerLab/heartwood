# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Notebook presentation over the gateway-owned session projection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from heartwood.gateway import (
    DEFAULT_SESSION_ID,
    ModelProfile,
    ProjectContext,
    ProjectionActionRecord,
    ProjectionActivity,
    ProjectionApprovalGroup,
    ProjectionLifecycleState,
    ProjectionMessage,
    ProjectionModelContext,
    ProjectionResearcherNotice,
    ProjectionResearcherStatus,
    ProjectionSubagent,
    ProjectionSuggestion,
    ProjectionTask,
    ProjectionUsage,
    SessionGateway,
    SessionProjection,
)
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
    ModelValidationResponse,
    PlatformCapabilitiesResponse,
    ProjectReadinessResponse,
    SpecialistSettingsResponse,
    StartupPlanResponse,
    WorkspaceChangesResponse,
    WorkspaceDiffResponse,
    WorkspaceFileResponse,
    WorkspaceTreeResponse,
)
from heartwood.session import CommandKind, JsonValue, SessionCommand, new_command_id


@dataclass(frozen=True, slots=True)
class NotebookViewModel:
    """Notebook presentation of one authoritative gateway projection."""

    projection: SessionProjection

    @property
    def session_id(self) -> str:
        """Return the projected session identifier."""
        return self.projection.session_id

    @property
    def event_count(self) -> int:
        """Return the number of durable events represented by the projection."""
        return self.projection.event_count

    @property
    def revision(self) -> int:
        """Return the last durable event sequence represented by the projection."""
        return self.projection.revision

    @property
    def activity(self) -> tuple[ProjectionActivity, ...]:
        """Return gateway-labeled session activity."""
        return self.projection.activity

    @property
    def conversation(self) -> tuple[ProjectionMessage, ...]:
        """Return the shared conversation and trace presentation."""
        return self.projection.conversation

    @property
    def actions(self) -> tuple[ProjectionActionRecord, ...]:
        """Return correlated OpenHands actions and their current outcomes."""
        return self.projection.actions

    @property
    def pending_approval(self) -> ProjectionApprovalGroup | None:
        """Return the one atomic action group awaiting a decision."""
        return self.projection.pending_approval

    @property
    def context(self) -> ProjectionModelContext:
        """Return the active model-routing context."""
        return self.projection.context

    @property
    def lifecycle(self) -> ProjectionLifecycleState:
        """Return the shared lifecycle and available transitions."""
        return self.projection.lifecycle

    @property
    def researcher_status(self) -> ProjectionResearcherStatus:
        """Return the shared researcher-facing session state."""
        return self.projection.researcher_status

    @property
    def researcher_notice(self) -> ProjectionResearcherNotice | None:
        """Return the latest non-lifecycle outcome that requires attention."""
        return self.projection.researcher_notice

    @property
    def task_plan(self) -> tuple[ProjectionTask, ...]:
        """Return the shared task plan."""
        return self.projection.task_plan

    @property
    def usage(self) -> ProjectionUsage | None:
        """Return accumulated model usage when available."""
        return self.projection.usage

    @property
    def usage_by_purpose(self) -> tuple[ProjectionUsage, ...]:
        """Return OpenHands usage separated by runtime purpose."""
        return self.projection.usage_by_purpose

    @property
    def subagents(self) -> tuple[ProjectionSubagent, ...]:
        """Return projected parent and specialist-agent lineage."""
        return self.projection.subagents

    @property
    def suggestions(self) -> tuple[ProjectionSuggestion, ...]:
        """Return deterministic next-step suggestions from the gateway."""
        return self.projection.suggestions

    @property
    def streaming_text(self) -> str:
        """Return transient model output not yet represented by a final event."""
        return self.projection.streaming_text

    @property
    def available_commands(
        self,
    ) -> tuple[str, ...]:
        """Return commands allowed by the shared lifecycle projection."""
        return self.projection.available_commands

    @property
    def paused(self) -> bool:
        """Return whether the projected session is paused."""
        return self.projection.paused


class NotebookSession:
    """Notebook API over the same gateway state used by terminal and browser clients."""

    def __init__(
        self,
        *,
        project: ProjectContext | None = None,
        session_id: str = DEFAULT_SESSION_ID,
        gateway: SessionGateway | None = None,
    ) -> None:
        gateway_project = getattr(gateway, "project", None)
        if project is None and isinstance(gateway_project, ProjectContext):
            self.project = gateway_project
        else:
            self.project = ProjectContext.current() if project is None else project
        if (
            isinstance(gateway_project, ProjectContext)
            and gateway_project.root != self.project.root
        ):
            raise ValueError("notebook project must match the injected gateway project")
        self.session_id = session_id
        self.gateway = SessionGateway(project=self.project) if gateway is None else gateway

    def chat(self, prompt: str) -> NotebookViewModel:
        """Submit one message and return the current session projection."""
        return self._handle(CommandKind.CHAT, {"prompt": prompt})

    def model_settings(self) -> ModelSettingsResponse:
        """Return non-secret model profiles and presets."""
        return self.gateway.model_settings()

    def initialize_project(self) -> StartupPlanResponse:
        """Confirm the current directory and create private project state."""
        return self.gateway.initialize_project(interface="notebook")

    def project_readiness(self) -> ProjectReadinessResponse:
        """Return the shared project setup and compute readiness report."""
        return self.gateway.project_readiness()

    def startup_plan(self) -> StartupPlanResponse:
        """Return the shared notebook startup and recovery projection."""
        return self.gateway.startup_plan(interface="notebook")

    def platform_capabilities(self) -> PlatformCapabilitiesResponse:
        """Return capabilities for the detected execution environment."""
        return self.gateway.platform_capabilities()

    def configure_model_source(self, source_id: str) -> ModelSettingsResponse:
        """Prepare the same project model source used by terminal and browser clients."""
        return self.gateway.configure_model_source(source_id)

    def discover_models(
        self,
        connection_id: str,
        *,
        token: str | None = None,
        base_url: str | None = None,
        refresh: bool = False,
        remember: bool = False,
    ) -> ModelCatalogResponse:
        """Discover models through the shared authorized connection catalog."""
        return self.gateway.discover_models(
            connection_id,
            token=token,
            base_url=base_url,
            refresh=refresh,
            remember=remember,
        )

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
        """Select a discovered model through the shared connection workflow."""
        return self.gateway.connect_model(
            connection_id,
            model_id,
            token=token,
            base_url=base_url,
            manual=manual,
            remember=remember,
        )

    def credential_settings(self) -> CredentialSettingsResponse:
        """Return non-secret credential-store and binding status."""
        return self.gateway.credential_settings()

    def forget_credential(self, connection_id: str) -> CredentialSettingsResponse:
        """Forget a process or saved credential for one connection."""
        return self.gateway.forget_credential(connection_id)

    def save_model_profile(self, profile: ModelProfile) -> ModelSettingsResponse:
        """Add or update one non-secret model profile."""
        return self.gateway.save_model_profile(profile)

    def select_model_profile(self, profile_id: str) -> ModelSettingsResponse:
        """Select the model profile used by subsequent turns."""
        return self.gateway.select_model_profile(profile_id)

    def validate_model_profile(
        self,
        profile_id: str | None = None,
    ) -> ModelValidationResponse:
        """Validate credential availability and route authorization."""
        return self.gateway.validate_model_profile(profile_id)

    def model_artifacts(self) -> ModelArtifactsResponse:
        """Return default and user-selected Heartwood-managed model choices."""
        return self.gateway.model_artifacts()

    def inspect_model_repository(
        self,
        repository: str,
        *,
        revision: str | None = None,
    ) -> ModelRepositoryPlanResponse:
        """Inspect supported candidates from one Hugging Face model repository."""
        return self.gateway.inspect_model_repository(repository, revision=revision)

    def download_local_model(self, model_id: str) -> ModelDownloadResponse:
        """Start a recommended Heartwood-managed model download."""
        return self.gateway.download_local_model(model_id)

    def download_custom_local_model(
        self,
        repository: str,
        *,
        revision: str | None = None,
    ) -> ModelDownloadResponse:
        """Start one inspected user-selected Heartwood-managed model download."""
        return self.gateway.download_custom_local_model(
            repository,
            revision=revision,
        )

    def import_local_model(
        self,
        source: Path,
        *,
        source_repository: str,
        source_revision: str,
        license_posture: str,
        context_window: int = 32_768,
    ) -> LocalModelImportResponse:
        """Import and select a reviewed Heartwood-managed model through the shared gateway."""
        return self.gateway.import_local_model(
            source,
            source_repository=source_repository,
            source_revision=source_revision,
            license_posture=license_posture,
            context_window=context_window,
        )

    def action_settings(self) -> ActionSettingsResponse:
        """Return the shared action-confirmation settings."""
        return self.gateway.action_settings()

    def specialist_settings(self) -> SpecialistSettingsResponse:
        """Return the same bounded specialist catalog used by terminal and browser clients."""
        return self.gateway.specialist_settings()

    def select_action_confirmation_mode(self, mode: str) -> ActionSettingsResponse:
        """Select a deployment-allowed action-confirmation mode."""
        return self.gateway.select_action_confirmation_mode(mode)

    def browser_url(self, *, port: int = 8767) -> str | None:
        """Return the supported browser URL, or ``None`` on terminal-only platforms."""
        return self.gateway.startup_plan(interface="web", port=port)["access_url"]

    def files(
        self,
        path: str = ".",
        *,
        depth: int | None = None,
    ) -> WorkspaceTreeResponse:
        """Return the same bounded project tree used by terminal and browser clients."""
        return self.gateway.workspace_tree(path=path, depth=depth)

    def file(self, path: str) -> WorkspaceFileResponse:
        """Return one bounded read-only project file."""
        return self.gateway.workspace_file(path=path)

    def changes(self) -> WorkspaceChangesResponse:
        """Return Git or structured session-derived project changes."""
        return self.gateway.workspace_changes(session_id=self.session_id)

    def diff(self, path: str) -> WorkspaceDiffResponse:
        """Return one bounded project diff."""
        return self.gateway.workspace_diff(session_id=self.session_id, path=path)

    def close(self) -> None:
        """Release active conversations and process-scoped credentials."""
        self.gateway.stop()

    def __enter__(self) -> NotebookSession:
        """Return this session for a managed notebook context."""
        return self

    def __exit__(
        self,
        _exception_type: object,
        _exception: object,
        _traceback: object,
    ) -> None:
        """Release resources when leaving a managed notebook context."""
        self.close()

    def approve(self, *, group_id: str) -> NotebookViewModel:
        """Allow every action in the pending OpenHands action group once."""
        return self._handle(
            CommandKind.APPROVE,
            {"target_type": "action-set", "target_id": group_id},
        )

    def deny(self, *, group_id: str) -> NotebookViewModel:
        """Reject every action in the pending OpenHands action group."""
        return self._handle(
            CommandKind.DENY,
            {"target_type": "action-set", "target_id": group_id},
        )

    def pause(self) -> NotebookViewModel:
        """Pause the session."""
        return self._handle(CommandKind.PAUSE)

    def resume(self) -> NotebookViewModel:
        """Resume the session."""
        return self._handle(CommandKind.RESUME)

    def audit_export(self) -> AuditExportResponse:
        """Create and return the same scrubbed audit export used by the browser."""
        self._handle(CommandKind.AUDIT_EXPORT)
        return self.gateway.audit_export(self.session_id)

    def replay(self) -> NotebookViewModel:
        """Return the gateway projection reconstructed from persisted state."""
        self.project.initialize()
        return self._view_model()

    def _handle(
        self,
        kind: CommandKind,
        payload: dict[str, JsonValue] | None = None,
    ) -> NotebookViewModel:
        command = self._command(kind, payload)
        self.gateway.handle(command)
        return self._view_model()

    def _view_model(self) -> NotebookViewModel:
        return build_view_model(
            self.gateway.persisted_session_projection(session_id=self.session_id)
        )

    def _command(
        self,
        kind: CommandKind,
        payload: dict[str, JsonValue] | None,
    ) -> SessionCommand:
        return SessionCommand(
            command_id=new_command_id(self.session_id, kind),
            session_id=self.session_id,
            kind=kind,
            actor_id="human",
            created_at=_utc_now(),
            payload={} if payload is None else payload,
        )


def build_view_model(projection: SessionProjection) -> NotebookViewModel:
    """Wrap the gateway-owned projection without reducing session events."""
    return NotebookViewModel(projection=projection)


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
