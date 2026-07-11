# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Session gateway orchestration and model-profile ownership."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from heartwood.adapters.platform import GenericPlatformAdapter
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
    ActionSettingsStore,
    action_settings_path,
)
from heartwood.gateway._model_artifacts import (
    ModelArtifactCatalog,
    ModelArtifactManager,
    load_model_artifact_catalog,
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
    model_connections_path,
)
from heartwood.gateway._model_settings import (
    MODEL_PRESETS,
    ModelProfile,
    ModelSettings,
    ModelSettingsError,
    ModelSettingsStore,
    model_profile_from_preset,
    model_settings_path,
)
from heartwood.gateway._openhands_sdk import OpenHandsSdkBackend
from heartwood.gateway._session_catalog import SessionCatalog, SessionCatalogError
from heartwood.gateway._skill_settings import SkillManager
from heartwood.gateway._stream import EventStreamHub, GatewayEventStream
from heartwood.model_policy import ModelPolicyEngine
from heartwood.schemas import PolicyProfile
from heartwood.session import SessionCommand, SessionEvent

SessionServiceFactory = Callable[[Path, str], SessionService]


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
        workspace: Path,
        service_factory: SessionServiceFactory | None = None,
        env: Mapping[str, str] | None = None,
        settings_store: ModelSettingsStore | None = None,
        action_settings_store: ActionSettingsStore | None = None,
        artifact_catalog: ModelArtifactCatalog | None = None,
        model_connections: Sequence[ModelConnection] | None = None,
        model_catalog_service: ModelCatalogService | None = None,
    ) -> None:
        self.workspace = workspace
        self.env = dict(os.environ if env is None else env)
        self.settings_store = settings_store or ModelSettingsStore(
            model_settings_path(workspace, self.env)
        )
        self.action_settings_store = action_settings_store or ActionSettingsStore(
            action_settings_path(workspace, self.env)
        )
        loaded_connections = (
            tuple(model_connections)
            if model_connections is not None
            else load_model_connections(model_connections_path(self.env))
        )
        for connection in loaded_connections:
            connection.validate(configurable=connection.connection_id == "custom-api")
        connection_ids = [connection.connection_id for connection in loaded_connections]
        if len(connection_ids) != len(set(connection_ids)):
            raise ModelCatalogError("model connection ids must be unique")
        self._model_connections = {
            connection.connection_id: connection for connection in loaded_connections
        }
        self.model_catalog_service = model_catalog_service or ModelCatalogService()
        self._runtime_credentials: dict[str, str] = {}
        repository_root = _repository_root()
        catalog_path = Path(
            self.env.get(
                "HEARTWOOD_MODEL_CATALOG",
                str(
                    repository_root / "images" / "generic" / "local-runtime" / "model-catalog.toml"
                ),
            )
        )
        self.artifact_catalog = artifact_catalog or load_model_artifact_catalog(catalog_path)
        self.model_cache_dir = Path(
            self.env.get("HEARTWOOD_MODEL_CACHE", str(workspace.parent / "models"))
        )
        self.artifact_manager = ModelArtifactManager(
            catalog=self.artifact_catalog,
            cache_dir=self.model_cache_dir,
        )
        bundled_skills_dir = Path(
            self.env.get(
                "HEARTWOOD_SKILLS_DIR",
                str(repository_root / "skills" / "verified"),
            )
        )
        self.installed_skills_dir = Path(
            self.env.get(
                "HEARTWOOD_INSTALLED_SKILLS_DIR",
                str(workspace.parent / "skills"),
            )
        )
        self.skill_manager = SkillManager(
            bundled_dir=bundled_skills_dir,
            installed_dir=self.installed_skills_dir,
            audit_path=workspace.parent / "skill-installations.jsonl",
        )
        self._service_factory = service_factory
        self.session_catalog = SessionCatalog(workspace)
        self._services: dict[str, SessionService] = {}
        self._streams = EventStreamHub()

    def start(self) -> None:
        """Start gateway dependencies lazily with the first conversation."""

    def stop(self) -> None:
        """Close active OpenHands conversations."""
        for service in self._services.values():
            service.close()
        self._services.clear()
        self._runtime_credentials.clear()

    def handle(self, command: SessionCommand) -> SessionResult:
        """Handle one command and publish emitted events."""
        service = self._service(command.session_id)
        result = service.handle(command)
        self._streams.publish(session_id=command.session_id, events=result.events)
        return result

    def sessions(self) -> dict[str, object]:
        """Return persisted sessions ordered by recent activity."""
        return {"sessions": [summary.safe_dict() for summary in self.session_catalog.list()]}

    def create_session(self, title: str | None = None) -> dict[str, object]:
        """Create and return one empty session."""
        return self.session_catalog.create(title).safe_dict()

    def session(self, session_id: str) -> dict[str, object]:
        """Return one persisted session summary."""
        return self.session_catalog.get(session_id).safe_dict()

    def rename_session(self, session_id: str, title: str) -> dict[str, object]:
        """Rename one persisted session."""
        return self.session_catalog.rename(session_id, title).safe_dict()

    def audit_export(self, session_id: str) -> dict[str, object]:
        """Return a generated scrubbed audit export for browser delivery."""
        self.session_catalog.get(session_id)
        store = FileSessionStore(self.workspace, session_id)
        try:
            content = store.read_audit_export()
        except OSError as error:
            msg = f"audit export is not available for session: {session_id}"
            raise SessionCatalogError(msg) from error
        return {
            "filename": f"{session_id}-audit.jsonl",
            "content": content,
        }

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
        credential_env = self._credential_environment()
        return {
            **settings.safe_dict(credential_env),
            "connections": [
                connection.safe_dict(credential_env)
                for connection in self._model_connections.values()
            ],
            "presets": [preset.safe_dict() for preset in MODEL_PRESETS],
        }

    def discover_models(
        self,
        connection_id: str,
        *,
        token: str | None = None,
        base_url: str | None = None,
        refresh: bool = False,
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
            self._remember_runtime_credential(connection, token)
            refresh = True
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
                self._remember_runtime_credential(connection, token)
        elif token is not None:
            self._authorize_model_catalog(connection)
            self._remember_runtime_credential(connection, token)
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
        settings = self.settings_store.load().with_profile(profile).selecting(profile.profile_id)
        self.settings_store.save(settings)
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

    def save_model_profile(self, profile: ModelProfile) -> dict[str, object]:
        """Add or replace a non-secret profile and reset active services."""
        settings = self.settings_store.load().with_profile(profile)
        self.settings_store.save(settings)
        self._reset_services()
        return self.model_settings()

    def connect_model_provider(self, preset_id: str, model_name: str) -> dict[str, object]:
        """Configure and select a provider using gateway-owned preset defaults."""
        profile = model_profile_from_preset(preset_id, model_name)
        settings = self.settings_store.load().with_profile(profile).selecting(profile.profile_id)
        self.settings_store.save(settings)
        self._reset_services()
        return self.model_settings()

    def select_model_profile(self, profile_id: str) -> dict[str, object]:
        """Select a profile and reset active services."""
        settings = self.settings_store.load().selecting(profile_id)
        self.settings_store.save(settings)
        self._reset_services()
        return self.model_settings()

    def remove_model_profile(self, profile_id: str) -> dict[str, object]:
        """Remove a profile and reset active services."""
        settings = self.settings_store.load().without_profile(profile_id)
        self.settings_store.save(settings)
        self._reset_services()
        return self.model_settings()

    def validate_model_profile(self, profile_id: str | None = None) -> dict[str, object]:
        """Validate credential availability and platform route authorization."""
        profile = self.settings_store.load().profile(profile_id)
        action_settings = self.action_settings_store.load()
        policy_profile = self._policy_profile()
        decision = ModelPolicyEngine(policy_profile).evaluate(
            endpoint=profile.policy_endpoint,
            capability_tier=profile.capability_tier,
            action_confirmation_mode=action_settings.confirmation_mode,
            credential_reference=profile.credential_reference,
            decision_id=f"model-profile-{profile.profile_id}",
            purpose=f"model profile {profile.profile_id}",
        )
        return {
            "profile": profile.safe_dict(),
            "credential_status": profile.credential_status(self._credential_environment()),
            "action_confirmation_mode": action_settings.confirmation_mode,
            "policy_decision": decision.model_dump(mode="json"),
        }

    def model_artifacts(self) -> dict[str, object]:
        """Return reviewed artifacts and background download status."""
        return {
            **self.artifact_catalog.safe_dict(),
            "downloads": [status.safe_dict() for status in self.artifact_manager.statuses()],
        }

    def download_model_artifact(self, artifact_id: str) -> dict[str, object]:
        """Start a reviewed artifact download into the configured cache."""
        return self.artifact_manager.start(artifact_id).safe_dict()

    def download_model_artifact_now(
        self,
        artifact_id: str,
        *,
        cache_dir: Path | None = None,
    ) -> Path:
        """Download and verify an artifact synchronously for CLI and automation."""
        artifact = self.artifact_catalog.artifact(artifact_id)
        return download_artifact(
            artifact,
            cache_dir=self.model_cache_dir if cache_dir is None else cache_dir,
        )

    def skill_settings(self) -> dict[str, object]:
        """Return bundled and explicitly installed Skills."""
        return {"skills": [summary.safe_dict() for summary in self.skill_manager.summaries()]}

    def inspect_skill(self, source: Path) -> dict[str, object]:
        """Verify a mounted Skill source without installing it."""
        return self.skill_manager.inspect(source).safe_dict()

    def install_skill(self, source: Path, *, approved: bool) -> dict[str, object]:
        """Install one extension after an explicit trust decision."""
        self.skill_manager.install(source, approved=approved)
        self._reset_services()
        return self.skill_settings()

    def remove_skill(self, name: str) -> dict[str, object]:
        """Remove one installed extension and reset active conversations."""
        self.skill_manager.remove(name)
        self._reset_services()
        return self.skill_settings()

    def _service(self, session_id: str) -> SessionService:
        self.session_catalog.ensure(session_id)
        service = self._services.get(session_id)
        if service is None:
            if self._service_factory is not None:
                service = self._service_factory(self.workspace, session_id)
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
            self.workspace,
            session_id=session_id,
            backend=backend,
            policy_profile=_policy_profile(self.env),
            env=self.env,
        )

    def _backend(
        self,
        *,
        model_settings: ModelSettings,
        action_settings: ActionSettings,
        session_id: str,
    ) -> AgentBackend:
        backend_id = self.env.get("HEARTWOOD_AGENT_BACKEND", "auto")
        if backend_id in {"deterministic", "deterministic-local"}:
            return DeterministicAgentBackend(
                action_confirmation_mode=action_settings.confirmation_mode
            )
        if backend_id not in {"auto", "openhands", "openhands-sdk"}:
            msg = f"unsupported HEARTWOOD_AGENT_BACKEND: {backend_id}"
            raise ValueError(msg)
        try:
            profile = model_settings.profile()
        except ModelSettingsError:
            return _UnconfiguredAgentBackend(action_settings.confirmation_mode)
        return OpenHandsSdkBackend(
            profile=profile,
            workspace=self.workspace.parent / "workspaces" / session_id,
            skills_dir=self.skill_manager.bundled_dir,
            additional_skills_dirs=(self.installed_skills_dir,),
            persistence_dir=self.workspace.parent / "openhands",
            credential_environment_names=tuple(
                configured_profile.api_key_env
                for configured_profile in model_settings.profiles
                if configured_profile.credential_kind == "environment"
                and configured_profile.api_key_env is not None
            ),
            action_confirmation_mode=action_settings.confirmation_mode,
            env=self._credential_environment(),
        )

    def _policy_profile(self) -> PolicyProfile:
        return _policy_profile(self.env) or GenericPlatformAdapter().default_policy_profile()

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
            self._runtime_credentials.get(credential_name)
            if connection.base_url == normalized_base_url
            else None
        )
        has_token = bool(token) or bool(self.env.get(credential_name)) or bool(runtime_token)
        dynamic = custom_model_connection(base_url, has_token=has_token)
        if connection != dynamic:
            if connection.base_url != dynamic.base_url:
                self._runtime_credentials.pop(credential_name, None)
            self._model_connections[connection_id] = dynamic
            self.model_catalog_service.invalidate(connection_id)
        return dynamic

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

    def _remember_runtime_credential(self, connection: ModelConnection, token: str) -> None:
        if connection.credential_kind != "environment" or connection.api_key_env is None:
            raise ModelCatalogError("this model connection does not accept an API token")
        if not token.strip():
            raise ModelCatalogError("API token must not be empty")
        self._runtime_credentials[connection.api_key_env] = token

    def _credential_environment(self) -> dict[str, str]:
        return {**self.env, **self._runtime_credentials}

    def _reset_services(self) -> None:
        for service in self._services.values():
            service.close()
        self._services.clear()


def _policy_profile(env: Mapping[str, str]) -> PolicyProfile | None:
    configured = env.get("HEARTWOOD_POLICY_PROFILE")
    if not configured:
        return None
    path = Path(configured)
    try:
        return PolicyProfile.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        msg = f"unable to load policy profile {path}: {error}"
        raise ValueError(msg) from error


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[5]


def _catalog_entry(catalog: ModelCatalog, model_id: str) -> ModelCatalogEntry:
    selected = model_id.strip()
    for entry in catalog.models:
        if selected in {entry.model_id, entry.execution_model}:
            return entry
    raise ModelCatalogError(f"model is not present in the discovered catalog: {model_id}")
