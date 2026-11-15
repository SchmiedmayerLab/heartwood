# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Read-only project readiness and persistent first-run configuration."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Literal, assert_never

import httpx

from heartwood.adapters import PlatformAdapter
from heartwood.adapters.platform import select_platform_adapter
from heartwood.gateway._action_presentation import action_mode_label
from heartwood.gateway._credential_isolation import assess_credential_isolation
from heartwood.gateway._diagnostics import diagnostic_for
from heartwood.gateway._managed_inference import managed_inference_client
from heartwood.gateway._model_catalog import (
    BUILT_IN_MODEL_CONNECTIONS,
    ModelCatalogError,
    ModelConnection,
    active_model_connections,
    model_connections_from_mapping,
    route_data_handling,
)
from heartwood.gateway._model_settings import ModelProfile, ModelSettingsError
from heartwood.gateway._project import ProjectContext, ProjectStateError
from heartwood.gateway._project_config import (
    LocalModelSelection,
    ProjectConfig,
    ProjectConfigError,
    ProjectConfigStore,
)
from heartwood.gateway._skill_settings import (
    BUNDLED_SKILL_DIGESTS,
    SkillSettingsError,
    pinned_bundled_manifests,
    unexpected_bundled_entries,
)
from heartwood.gateway._subscriptions import (
    OPENAI_SUBSCRIPTION_CONNECTION_ID,
    OPENAI_SUBSCRIPTION_CREDENTIAL_REFERENCE,
    OPENAI_SUBSCRIPTION_ENDPOINT,
    OpenHandsOpenAISubscription,
    SubscriptionError,
)
from heartwood.model_policy import ModelPolicyEngine
from heartwood.persistence import NativeLockUnavailableError
from heartwood.schemas import PolicyProfile
from heartwood.skills import SkillArtifactStore, SkillStoreError

ReadinessState = Literal["ready", "setup-required", "compute-required", "recovery-required"]
ModelSource = Literal[
    "anthropic",
    "custom",
    "heartwood",
    "openai",
    "openai-subscription",
    "stanford-ai-api-gateway",
]

_STANFORD_ROOT = "https://aiapi-prod.stanford.edu/v1"
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ReadinessCheck:
    """One content-free project readiness result."""

    check_id: str
    status: Literal["pass", "warning", "fail"]
    summary: str

    def safe_dict(self) -> dict[str, object]:
        """Return content-safe metadata, adding recovery guidance when needed."""
        if self.status == "pass":
            return asdict(self)
        diagnostic = diagnostic_for(self.check_id)
        return {
            **asdict(self),
            "code": diagnostic.code,
            "title": diagnostic.title,
            "next_action": diagnostic.next_action,
            "documentation_path": diagnostic.documentation_path,
        }


@dataclass(frozen=True, slots=True)
class ModelSourceOption:
    """One approachable setup choice backed by a gateway model connection."""

    source_id: ModelSource
    connection_id: str
    label: str
    description: str

    def safe_dict(self, *, selected: bool) -> dict[str, object]:
        """Return the non-secret setup representation shared by all interfaces."""
        return {**asdict(self), "selected": selected}


MODEL_SOURCE_OPTIONS: tuple[ModelSourceOption, ...] = (
    ModelSourceOption(
        source_id="heartwood",
        connection_id="heartwood",
        label="Run with Heartwood",
        description=("Let Heartwood download or import a model and run it in this environment."),
    ),
    ModelSourceOption(
        source_id="stanford-ai-api-gateway",
        connection_id="stanford-ai-api-gateway",
        label="Stanford AI API Gateway",
        description="Use the models available to a Stanford AI API Gateway key.",
    ),
    ModelSourceOption(
        source_id="openai-subscription",
        connection_id=OPENAI_SUBSCRIPTION_CONNECTION_ID,
        label="Sign in with ChatGPT",
        description="Use a ChatGPT Plus or Pro subscription through OpenHands.",
    ),
    ModelSourceOption(
        source_id="openai",
        connection_id="openai",
        label="OpenAI API",
        description="Use models billed through an OpenAI API key.",
    ),
    ModelSourceOption(
        source_id="anthropic",
        connection_id="anthropic",
        label="Anthropic",
        description="Use the models available to an Anthropic API key.",
    ),
    ModelSourceOption(
        source_id="custom",
        connection_id="custom-api",
        label="Other compatible service",
        description="Connect to an approved service that implements the OpenAI API format.",
    ),
)


def model_source_options(
    env: Mapping[str, str] | None = None,
) -> tuple[ModelSourceOption, ...]:
    """Return model-source choices appropriate for the detected environment."""
    active_env = dict(os.environ if env is None else env)
    adapter = select_platform_adapter(active_env)
    available = set(adapter.capabilities().model_sources)
    return tuple(option for option in MODEL_SOURCE_OPTIONS if option.source_id in available)


def model_source_for_connection(connection_id: str) -> ModelSource:
    """Return the public setup source represented by one connection id."""
    for option in MODEL_SOURCE_OPTIONS:
        if option.connection_id == connection_id:
            return option.source_id
    raise ProjectConfigError(f"model connection has no setup source: {connection_id}")


type DeploymentControl = Literal["required", "configured", "not-configured"]


@dataclass(frozen=True, slots=True)
class EffectivePolicy:
    """The policy in force for a project and the trusted configuration that bounds it."""

    source: Literal["deployment", "platform"]
    policy: PolicyProfile
    manifest: str | None = None
    manifest_sha256: str | None = None
    isolated_worker: DeploymentControl = "not-configured"
    checkpoint_evidence: DeploymentControl = "not-configured"
    change_refused: str | None = None

    def safe_dict(self) -> dict[str, object]:
        """Return the non-secret policy dimensions every interface reports."""
        policy = self.policy
        signers = policy.allowed_checkpoint_signer_profiles
        return {
            "source": self.source,
            "manifest": self.manifest,
            "manifest_sha256": self.manifest_sha256,
            "policy_id": policy.policy_id,
            "platform_id": policy.platform_id,
            "action_review_modes": list(policy.allowed_action_review_modes),
            "model_endpoints": list(policy.allowed_model_endpoints),
            "model_catalog_endpoints": list(policy.allowed_model_catalog_endpoints),
            "capability_tiers": list(policy.allowed_capability_tiers),
            "credential_references": list(policy.credential_allowlist),
            "credential_backends": list(policy.allowed_credential_backends),
            "checkpoint_signer_profiles": None if signers is None else list(signers),
            "isolated_worker": self.isolated_worker,
            "checkpoint_evidence": self.checkpoint_evidence,
            "change_refused": self.change_refused,
        }


@dataclass(frozen=True, slots=True)
class DeploymentReadiness:
    """Read-only readiness projection shared by every setup entrypoint."""

    state: ReadinessState
    platform_id: str
    project_root: str
    state_root: str
    evidence: tuple[str, ...]
    checks: tuple[ReadinessCheck, ...]
    policy: EffectivePolicy

    def safe_dict(self) -> dict[str, object]:
        """Return serializable diagnostics without secrets."""
        return {
            "state": self.state,
            "platform_id": self.platform_id,
            "project_root": self.project_root,
            "state_root": self.state_root,
            "evidence": list(self.evidence),
            "checks": [check.safe_dict() for check in self.checks],
            "policy": self.policy.safe_dict(),
        }


def prepare_openhands_sdk(env: Mapping[str, str]) -> None:
    """Validate the full OpenHands import only when runtime resources are available."""
    from heartwood.gateway._openhands_sdk import prepare_openhands_sdk as prepare

    prepare(env)


def _is_carina_login_node(env: Mapping[str, str], *, platform_id: str) -> bool:
    return platform_id == "carina" and not env.get("SLURM_JOB_ID")


def _agent_runtime_available(env: Mapping[str, str], *, platform_id: str) -> bool:
    if _is_carina_login_node(env, platform_id=platform_id):
        try:
            version("openhands-sdk")
            version("openhands-tools")
        except PackageNotFoundError:
            return False
        return True
    try:
        prepare_openhands_sdk(env)
    except Exception as error:
        _LOGGER.warning(
            "OpenHands runtime readiness probe failed (%s)",
            type(error).__name__,
        )
        return False
    return True


def inspect_deployment(
    project: ProjectContext,
    env: Mapping[str, str] | None = None,
    *,
    bundled_skills_dir: Path | None = None,
    bundled_skill_digests: Mapping[str, str] = BUNDLED_SKILL_DIGESTS,
    policy: EffectivePolicy | None = None,
) -> DeploymentReadiness:
    """Inspect project setup without creating files or calling external services."""
    active_env = dict(os.environ if env is None else env)
    adapter = select_platform_adapter(active_env)
    detection = adapter.detect(active_env)
    checks: list[ReadinessCheck] = []
    carina_login_node = _is_carina_login_node(
        active_env,
        platform_id=adapter.adapter_id,
    )

    if not _agent_runtime_available(active_env, platform_id=adapter.adapter_id):
        checks.append(
            ReadinessCheck(
                "agent-runtime",
                "fail",
                "OpenHands agent dependencies cannot be loaded",
            )
        )
    else:
        summary = (
            "OpenHands agent packages are installed; runtime validation follows in the allocation"
            if carina_login_node
            else "OpenHands agent dependencies are available"
        )
        checks.append(
            ReadinessCheck(
                "agent-runtime",
                "pass",
                summary,
            )
        )

    writable = os.access(project.root, os.W_OK)
    checks.append(
        ReadinessCheck(
            "project-storage",
            "pass" if writable else "fail",
            (
                f"Project is writable at {project.root}"
                if writable
                else f"Project is not writable at {project.root}"
            ),
        )
    )
    broad_project = project.root in {Path(project.root.anchor), Path.home().resolve()}
    checks.append(
        ReadinessCheck(
            "project-boundary",
            "fail" if broad_project else "pass",
            (
                "Choose a dedicated project directory instead of a filesystem or home root"
                if broad_project
                else "Project boundary is appropriately scoped"
            ),
        )
    )
    state_valid = False
    try:
        state_valid = project.state_exists()
    except ProjectStateError as error:
        checks.append(ReadinessCheck("project-state", "fail", str(error)))
    else:
        checks.append(
            ReadinessCheck(
                "project-state",
                "pass" if state_valid else "warning",
                "Project state is initialized" if state_valid else "Project setup is incomplete",
            )
        )

    if bundled_skills_dir is not None:
        checks.append(_bundled_skill_check(bundled_skills_dir, bundled_skill_digests))
    if state_valid:
        checks.append(_installed_skill_check(project))

    if adapter.adapter_id == "terra":
        checks.extend(_terra_environment_checks(project=project, env=active_env))

    config: ProjectConfig | None = None
    config_store = ProjectConfigStore(
        project,
        ProjectConfig(
            platform_id=adapter.adapter_id,
            policy=adapter.default_policy_profile(),
        ),
    )
    if state_valid and config_store.configured:
        try:
            config = config_store.load()
        except ProjectConfigError as error:
            checks.append(ReadinessCheck("configuration", "fail", str(error)))
        else:
            platform_matches = config.platform_id == adapter.adapter_id
            checks.append(
                ReadinessCheck(
                    "configuration",
                    "pass" if platform_matches else "fail",
                    (
                        "Project configuration is valid"
                        if platform_matches
                        else "Configured platform does not match the detected platform"
                    ),
                )
            )
    else:
        checks.append(ReadinessCheck("configuration", "warning", "Setup is incomplete"))

    configured_source = config.model_source if config is not None else None
    source_supported = configured_source in adapter.capabilities().model_sources
    checks.append(
        ReadinessCheck(
            "model-source",
            "pass" if source_supported else "warning" if configured_source is None else "fail",
            (
                "Model connection is selected"
                if source_supported
                else "No model connection selected"
                if configured_source is None
                else "Selected model connection is unavailable on this platform"
            ),
        )
    )
    active_model = _active_model_profile(config)
    checks.append(
        ReadinessCheck(
            "model",
            "pass" if active_model else "warning",
            (
                f"Active model: {_active_model_label(config, active_model)}"
                if active_model
                else "No active model selected"
            ),
        )
    )
    credential_ready = True
    model_connections = active_model_connections(
        BUILT_IN_MODEL_CONNECTIONS,
        () if config is None else config.additional_connections,
        allowed_connection_ids=(
            option.connection_id for option in model_source_options(active_env)
        ),
    )
    if active_model is not None:
        credential_status, credential_summary, credential_ready = _credential_readiness(
            active_model, active_env
        )
        checks.append(ReadinessCheck("model-credential", credential_status, credential_summary))
        isolation = assess_credential_isolation(
            active_model,
            adapter.capabilities(),
            model_source=None if config is None else config.model_source,
            model_connections=model_connections,
        )
        checks.append(
            ReadinessCheck(
                "credential-isolation",
                "pass" if isolation.status in {"not-required", "qualified"} else "warning",
                isolation.summary,
            )
        )
    managed_local_available = False
    managed_local_active = False
    if config is not None and config.model_source == "heartwood":
        selection = config.local_model
        managed_local_available = (
            selection is not None and selection.resolved_path(project).exists()
        )
        managed_local_active = selection is not None and managed_local_runtime_active(
            selection,
            active_env,
        )
        checks.append(
            ReadinessCheck(
                "local-model-artifact",
                "pass" if managed_local_available else "warning",
                (
                    f"Selected Heartwood-managed model is available: {selection.artifact_id}"
                    if managed_local_available and selection is not None
                    else (
                        "No downloaded Heartwood-managed model is selected; "
                        "an existing compatible service is required"
                    )
                ),
            )
        )

    if policy is None:
        policy = EffectivePolicy(
            "platform", config.policy if config is not None else config_store.default.policy
        )
    if policy.change_refused is not None:
        checks.append(ReadinessCheck("deployment-change", "fail", policy.change_refused))
    if config is not None:
        checks.append(
            ReadinessCheck(
                "policy",
                "pass",
                "Policy from the operator deployment manifest is valid"
                if policy.source == "deployment"
                else f"Policy from the {adapter.adapter_id} platform is valid",
            )
        )
        action_mode = config.action_settings.review_mode
        checks.append(
            ReadinessCheck(
                "action-review",
                "pass",
                "Action review: "
                + ("deployment default" if action_mode is None else action_mode_label(action_mode)),
            )
        )
        coherent = active_model is not None and _configuration_is_coherent(
            config, model_connections
        )
        checks.append(
            ReadinessCheck(
                "configuration-coherence",
                "pass" if coherent else "warning" if active_model is None else "fail",
                (
                    "Model, policy, connection, and action settings agree"
                    if coherent
                    else "Model selection is incomplete"
                    if active_model is None
                    else "Model, policy, connection, or action settings do not agree"
                ),
            )
        )

    local_compute_required = False
    if adapter.adapter_id == "carina":
        model_source = config.model_source if config is not None else None
        carina_checks, local_compute_required = _carina_compute_checks(
            model_source=model_source,
            env=active_env,
        )
        checks.extend(carina_checks)

    if any(check.status == "fail" for check in checks):
        state: ReadinessState = "recovery-required"
    elif managed_local_available and not managed_local_active:
        state = "compute-required"
    elif config is None or active_model is None or not credential_ready:
        state = "setup-required"
    elif local_compute_required:
        state = "compute-required"
    else:
        state = "ready"
    return DeploymentReadiness(
        state=state,
        platform_id=adapter.adapter_id,
        project_root=str(project.root),
        state_root=str(project.state_root),
        evidence=detection.evidence,
        checks=tuple(checks),
        policy=policy,
    )


def _bundled_skill_check(bundled_dir: Path, digests: Mapping[str, str]) -> ReadinessCheck:
    """Report release-bundled Skills that differ from the release pin."""
    try:
        pinned_bundled_manifests(bundled_dir, digests)
    except SkillSettingsError as error:
        return ReadinessCheck("bundled-skills", "fail", str(error))
    if unexpected := unexpected_bundled_entries(bundled_dir, digests):
        return ReadinessCheck(
            "bundled-skills",
            "warning",
            "Unpinned entries in the bundled Skill directory are not loaded; remove: "
            + ", ".join(str(path) for path in unexpected),
        )
    return ReadinessCheck("bundled-skills", "pass", "Bundled Skills match this release")


def _installed_skill_check(project: ProjectContext) -> ReadinessCheck:
    """Report content that is stored beside an active installed Skill but never loaded."""
    store = SkillArtifactStore(project.skills_dir)
    try:
        unexpected = tuple(
            str(path)
            for record in store.records()
            if record.status == "active"
            for path in store.unexpected_artifact_entries(record)
        )
    except (NativeLockUnavailableError, SkillStoreError) as error:
        return ReadinessCheck("installed-skills", "fail", str(error))
    if unexpected:
        return ReadinessCheck(
            "installed-skills",
            "warning",
            "Unverified content beside installed Skills is not loaded; remove: "
            + ", ".join(unexpected),
        )
    return ReadinessCheck(
        "installed-skills", "pass", "Installed Skill storage holds only verified content"
    )


def managed_local_runtime_active(
    selection: LocalModelSelection,
    env: Mapping[str, str],
) -> bool:
    """Confirm the selected model through its explicit private launch binding."""
    if env.get("HEARTWOOD_LOCAL_RUNTIME_ARTIFACT_ID") != selection.artifact_id:
        return False
    try:
        with managed_inference_client(env, timeout=0.5) as client:
            response = client.get("models")
            response.raise_for_status()
            payload = response.json()
    except (OSError, ValueError, httpx.HTTPError):
        return False
    models = payload.get("data", []) if isinstance(payload, dict) else []
    return isinstance(models, list) and any(
        isinstance(model, dict) and model.get("id") == selection.model_id for model in models
    )


def persist_deployment_profile(
    project: ProjectContext,
    *,
    model_source: ModelSource,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Persist the selected platform, route policy, and connection in config.toml."""
    active_env = dict(os.environ if env is None else env)
    adapter = select_platform_adapter(active_env)
    capabilities = adapter.capabilities()
    if model_source not in capabilities.model_sources:
        raise ProjectConfigError(
            f"{capabilities.display_name} does not provide the {model_source} model source"
        )
    platform_policy = adapter.default_policy_profile()
    store = ProjectConfigStore(
        project,
        ProjectConfig(platform_id=adapter.adapter_id, policy=platform_policy),
    )
    configured_connections: tuple[ModelConnection, ...] = ()
    if model_source == "stanford-ai-api-gateway":
        if not capabilities.managed_model_connections:
            raise ProjectConfigError(
                "the detected environment does not provide a managed research connection"
            )
        all_connections = model_connections_from_mapping(_stanford_connection_manifest())
        configured_connections = tuple(
            connection for connection in all_connections if connection.source == "platform"
        )
        policy = _stanford_route_policy(platform_policy)
    elif model_source in {
        "anthropic",
        "custom",
        "heartwood",
        "openai",
        "openai-subscription",
    }:
        policy = platform_policy
    else:  # pragma: no cover - protected by the public type and CLI choices
        raise ProjectConfigError(f"unsupported model source: {model_source}")

    def apply(current: ProjectConfig) -> ProjectConfig:
        model_settings = current.model_settings
        if current.model_source != model_source:
            model_settings = replace(model_settings, active_profile=None)
        additional_connections = (
            configured_connections
            if model_source == "stanford-ai-api-gateway"
            else current.additional_connections
        )
        # Selecting a model source never lifts an existing data-destination restriction.
        restriction = current.policy.allowed_data_destinations or policy.allowed_data_destinations
        return ProjectConfig(
            platform_id=adapter.adapter_id,
            model_source=model_source,
            action_settings=current.action_settings,
            model_settings=model_settings,
            additional_connections=additional_connections,
            policy=policy.model_copy(update={"allowed_data_destinations": restriction}),
            local_model=current.local_model,
        )

    store.update(apply)
    return project.config_path


def _stanford_route_policy(platform_policy: PolicyProfile) -> PolicyProfile:
    return PolicyProfile(
        policy_id=f"{platform_policy.platform_id}-stanford-ai-api-gateway",
        platform_id=platform_policy.platform_id,
        deny_egress_by_default=True,
        allowed_model_endpoints=(
            f"{_STANFORD_ROOT}/chat/completions",
            f"{_STANFORD_ROOT}/responses",
        ),
        allowed_model_catalog_endpoints=(f"{_STANFORD_ROOT}/models",),
        allowed_capability_tiers=("supervised", "experimental"),
        allowed_action_review_modes=platform_policy.allowed_action_review_modes,
        credential_allowlist=("STANFORD_AI_API_KEY",),
        allowed_credential_backends=platform_policy.allowed_credential_backends,
        aggregate_count_floor=20,
        notes="Stanford gateway route; data eligibility requires deployment approval.",
    )


def platform_policy_ceiling(adapter: PlatformAdapter, requested: PolicyProfile) -> PolicyProfile:
    """Return the widest route policy this platform's own setup could select.

    A platform that offers Custom API lets its setup add any compatible endpoint and
    credential name, so only platforms without that source restrict those lists.
    """
    platform_policy = adapter.default_policy_profile()
    capabilities = adapter.capabilities()
    routes = [platform_policy]
    if "stanford-ai-api-gateway" in capabilities.model_sources:
        routes.append(_stanford_route_policy(platform_policy))
    if "custom" in capabilities.model_sources:
        routes.append(requested)

    def union(field: str) -> tuple[str, ...]:
        return tuple(dict.fromkeys(value for route in routes for value in getattr(route, field)))

    return platform_policy.model_copy(
        update={
            "allowed_model_endpoints": union("allowed_model_endpoints"),
            "allowed_model_catalog_endpoints": union("allowed_model_catalog_endpoints"),
            "credential_allowlist": tuple(
                dict.fromkeys((*union("credential_allowlist"), *adapter.credential_allowlist()))
            ),
            "allowed_credential_backends": capabilities.credential_backends,
        }
    )


def _stanford_connection_manifest() -> dict[str, object]:
    return {
        "schema_version": "heartwood.model-connections.v1",
        "connections": [
            {
                "connection_id": "stanford-ai-api-gateway",
                "label": "Stanford AI API Gateway",
                "protocol": "openai-compatible",
                "model_prefix": "openai/",
                "source": "platform",
                "credential_kind": "environment",
                "api_key_env": "STANFORD_AI_API_KEY",
                "base_url": _STANFORD_ROOT,
                "catalog_endpoint": f"{_STANFORD_ROOT}/models",
                "policy_endpoint": f"{_STANFORD_ROOT}/chat/completions",
                "description": "Models authorized for the supplied Stanford gateway key.",
                "static_models": [],
                "data_handling": {
                    "destination": "institution-managed",
                    "retention_authority": "institution",
                    "route": "Stanford AI API Gateway and the model providers it routes to",
                },
            }
        ],
    }


def _active_model_profile(config: ProjectConfig | None) -> ModelProfile | None:
    if config is None:
        return None
    try:
        return config.model_settings.profile()
    except ModelSettingsError:
        return None


def _active_model_label(config: ProjectConfig | None, profile: ModelProfile) -> str:
    if profile.profile_id == "heartwood" and config is not None and config.local_model is not None:
        return config.local_model.display_name or profile.description or profile.model
    return profile.description or profile.model


def _credential_readiness(
    profile: ModelProfile, env: Mapping[str, str]
) -> tuple[Literal["pass", "warning", "fail"], str, bool]:
    if profile.auth_type == "subscription":
        try:
            available = OpenHandsOpenAISubscription().credential_available()
        except SubscriptionError:
            return "warning", "OpenHands could not inspect ChatGPT sign-in", False
        return (
            ("pass", "ChatGPT sign-in is available through OpenHands", True)
            if available
            else ("warning", "Sign in with ChatGPT to use the selected model", False)
        )
    kind = profile.credential_kind
    if kind == "none":
        return "pass", "Selected model requires no credential", True
    if kind == "environment":
        name = profile.api_key_env
        if not isinstance(name, str) or not name.strip():
            return "fail", "Selected model has an invalid platform credential binding", False
        return (
            ("pass", "Platform credential is available", True)
            if env.get(name)
            else ("warning", "A provider credential is required for this process", False)
        )
    if kind == "file":
        configured = profile.api_key_file
        available = isinstance(configured, str) and Path(configured).is_file()
        return (
            ("pass", "Platform credential file is available", True)
            if available
            else ("warning", "The configured credential file is unavailable", False)
        )
    if kind == "managed-identity":
        return "warning", "Managed identity must be validated by the deployment", True
    assert_never(kind)


def _configuration_is_coherent(
    config: ProjectConfig, model_connections: tuple[ModelConnection, ...]
) -> bool:
    model = _active_model_profile(config)
    if model is None or config.model_source is None:
        return False
    if config.model_source == "heartwood":
        source_matches = model.is_local
    elif config.model_source == "stanford-ai-api-gateway":
        connection = next(
            (
                connection
                for connection in config.additional_connections
                if connection.connection_id == "stanford-ai-api-gateway"
            ),
            None,
        )
        try:
            request_endpoint = (
                connection.request_endpoint(model.model) if connection is not None else None
            )
        except ModelCatalogError:
            request_endpoint = None
        source_matches = (
            connection is not None
            and model.policy_endpoint == request_endpoint
            and model.api_key_env == "STANFORD_AI_API_KEY"
        )
    elif config.model_source == "custom":
        source_matches = model.profile_id == "custom-api"
    elif config.model_source == "openai-subscription":
        source_matches = (
            model.profile_id == OPENAI_SUBSCRIPTION_CONNECTION_ID
            and model.auth_type == "subscription"
            and model.subscription_vendor == "openai"
            and model.policy_endpoint == OPENAI_SUBSCRIPTION_ENDPOINT
            and model.credential_reference == OPENAI_SUBSCRIPTION_CREDENTIAL_REFERENCE
        )
    else:
        source_matches = model.profile_id == config.model_source
    if not source_matches:
        return False
    decision = ModelPolicyEngine(config.policy).evaluate(
        endpoint=model.policy_endpoint,
        capability_tier=model.capability_tier,
        credential_reference=model.credential_reference,
        data_handling=route_data_handling(model, model_connections),
        decision_id="deployment-readiness",
        purpose="deployment readiness",
    )
    return decision.decision == "allow"


def _carina_compute_checks(
    *,
    model_source: str | None,
    env: Mapping[str, str],
) -> tuple[tuple[ReadinessCheck, ...], bool]:
    allocation = bool(env.get("SLURM_JOB_ID"))
    if model_source == "stanford-ai-api-gateway":
        return (
            (
                ReadinessCheck(
                    "slurm-allocation",
                    "pass",
                    "Selected managed model route does not require a Slurm allocation",
                ),
            ),
            False,
        )
    local_model = model_source == "heartwood"
    if not allocation:
        return (
            (
                ReadinessCheck(
                    "slurm-allocation",
                    "warning",
                    (
                        "Heartwood-managed model is configured; request a Carina "
                        "allocation to start it"
                        if local_model
                        else "No Slurm allocation; one is required only for "
                        "Heartwood-managed inference"
                    ),
                ),
                ReadinessCheck(
                    "job-scratch",
                    "warning",
                    "Allocation scratch will be checked after allocation",
                ),
                ReadinessCheck(
                    "gpu",
                    "warning",
                    "NVIDIA GPU visibility will be checked after allocation",
                ),
            ),
            local_model,
        )
    requirement_status: Literal["warning", "fail"] = "fail" if local_model else "warning"
    scratch = env.get("LOCAL_SCRATCH_JOB")
    scratch_path = Path(scratch) if scratch else None
    scratch_ready = bool(
        scratch_path and scratch_path.is_dir() and os.access(scratch_path, os.W_OK)
    )
    gpu = gpu_visible(env)
    return (
        (
            ReadinessCheck("slurm-allocation", "pass", "Active Slurm allocation detected"),
            ReadinessCheck(
                "job-scratch",
                "pass" if scratch_ready else requirement_status,
                (
                    f"Writable allocation scratch detected at {scratch_path}"
                    if scratch_ready
                    else "Active managed-model allocation has no writable job scratch"
                    if local_model
                    else "No writable allocation scratch detected"
                ),
            ),
            ReadinessCheck(
                "gpu",
                "pass" if gpu else requirement_status,
                (
                    "NVIDIA GPU is visible"
                    if gpu
                    else "Active managed-model allocation has no visible NVIDIA GPU"
                    if local_model
                    else "No NVIDIA GPU evidence detected"
                ),
            ),
        ),
        False,
    )


def _terra_environment_checks(
    *,
    project: ProjectContext,
    env: Mapping[str, str],
) -> tuple[ReadinessCheck, ...]:
    """Validate the persistent project boundary and optional GPU attachment on Terra."""
    persistent_root = Path(
        env.get("HEARTWOOD_PLATFORM_HOME", "").strip() or "/home/jupyter"
    ).resolve()
    project_root = project.root.resolve()
    if project_root == persistent_root:
        storage = ReadinessCheck(
            "terra-project-storage",
            "fail",
            (
                f"Create and enter a dedicated project directory under {persistent_root}; "
                "the persistent-disk root is too broad for an agent project"
            ),
        )
    elif project_root.is_relative_to(persistent_root):
        storage = ReadinessCheck(
            "terra-project-storage",
            "pass",
            f"Project is inside Terra persistent storage at {persistent_root}",
        )
    else:
        storage = ReadinessCheck(
            "terra-project-storage",
            "fail",
            (
                f"Move the project under Terra persistent storage at {persistent_root}; "
                "files outside that mount can be lost when the Cloud Environment is replaced"
            ),
        )

    checks = [storage]
    gpu_runtime = env.get("HEARTWOOD_GPU_RUNTIME", "").strip().lower()
    if gpu_runtime == "vllm":
        visible = gpu_visible(env)
        checks.append(
            ReadinessCheck(
                "terra-gpu",
                "pass" if visible else "warning",
                (
                    "Terra NVIDIA runtime and attached GPU detected"
                    if visible
                    else "Terra NVIDIA runtime detected; attach a GPU before managed GPU inference"
                ),
            )
        )
    elif gpu_runtime == "none":
        checks.append(
            ReadinessCheck(
                "terra-gpu",
                "pass",
                "Portable Terra runtime selected; Heartwood-managed models use CPU inference",
            )
        )
    return tuple(checks)


def gpu_visible(env: Mapping[str, str]) -> bool:
    """Return whether the current process has evidence of an attached NVIDIA GPU."""
    visible = env.get("NVIDIA_VISIBLE_DEVICES") or env.get("CUDA_VISIBLE_DEVICES")
    configured = bool(visible and visible.lower() not in {"none", "void", "-1"})
    return configured or Path("/dev/nvidia0").exists()
