# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Session gateway orchestration and model-profile ownership."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from pathlib import Path

from heartwood.adapters.platform import GenericPlatformAdapter
from heartwood.core_adapter import (
    AgentBackend,
    BackendEvent,
    BackendEventKind,
    DeterministicAgentBackend,
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
from heartwood.gateway._model_settings import (
    MODEL_PRESETS,
    ModelProfile,
    ModelSettings,
    ModelSettingsError,
    ModelSettingsStore,
    model_settings_path,
)
from heartwood.gateway._openhands_sdk import OpenHandsSdkBackend
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
            "no active model profile; configure one with `heartwood models add` "
            "and `heartwood models select`"
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

    def restore_pending(self, tool_call: ProposedToolCall | None) -> None:  # noqa: ARG002
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
    ) -> None:
        self.workspace = workspace
        self.env = dict(os.environ if env is None else env)
        self.settings_store = settings_store or ModelSettingsStore(
            model_settings_path(workspace, self.env)
        )
        self.action_settings_store = action_settings_store or ActionSettingsStore(
            action_settings_path(workspace, self.env)
        )
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
        self._services: dict[str, SessionService] = {}
        self._streams = EventStreamHub()

    def start(self) -> None:
        """Start gateway dependencies lazily with the first conversation."""

    def stop(self) -> None:
        """Close active OpenHands conversations."""
        for service in self._services.values():
            service.close()
        self._services.clear()

    def handle(self, command: SessionCommand) -> SessionResult:
        """Handle one command and publish emitted events."""
        service = self._service(command.session_id)
        result = service.handle(command)
        self._streams.publish(session_id=command.session_id, events=result.events)
        return result

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
        """Return API-safe settings and common provider presets."""
        settings = self.settings_store.load()
        return {
            **settings.safe_dict(self.env),
            "presets": [preset.safe_dict() for preset in MODEL_PRESETS],
        }

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
            "credential_status": profile.credential_status(self.env),
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
            action_confirmation_mode=action_settings.confirmation_mode,
            env=self.env,
        )

    def _policy_profile(self) -> PolicyProfile:
        return _policy_profile(self.env) or GenericPlatformAdapter().default_policy_profile()

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
