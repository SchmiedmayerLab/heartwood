# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Read-only project readiness and persistent first-run configuration."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Literal, assert_never

from heartwood.adapters.platform import select_platform_adapter
from heartwood.gateway._model_catalog import ModelConnection, model_connections_from_mapping
from heartwood.gateway._model_settings import ModelProfile, ModelSettingsError
from heartwood.gateway._project import ProjectContext, ProjectStateError
from heartwood.gateway._project_config import (
    ProjectConfig,
    ProjectConfigError,
    ProjectConfigStore,
)
from heartwood.model_policy import ModelPolicyEngine
from heartwood.schemas import PolicyProfile

ReadinessState = Literal["ready", "setup-required", "compute-required", "recovery-required"]
ModelSource = Literal["anthropic", "local", "openai", "stanford-ai-api-gateway"]

_STANFORD_ROOT = "https://aiapi-prod.stanford.edu/v1"


@dataclass(frozen=True, slots=True)
class ReadinessCheck:
    """One content-free project readiness result."""

    check_id: str
    status: Literal["pass", "warning", "fail"]
    summary: str


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
        source_id="local",
        connection_id="local",
        label="On this device",
        description=(
            "Use a recommended model, another supported Hugging Face model, or an existing service."
        ),
    ),
    ModelSourceOption(
        source_id="openai",
        connection_id="openai",
        label="OpenAI",
        description="Use the models available to an OpenAI token.",
    ),
    ModelSourceOption(
        source_id="anthropic",
        connection_id="anthropic",
        label="Anthropic",
        description="Use the models available to an Anthropic token.",
    ),
    ModelSourceOption(
        source_id="stanford-ai-api-gateway",
        connection_id="stanford-ai-api-gateway",
        label="Stanford AI API Gateway",
        description="Use models authorized through Stanford's managed gateway.",
    ),
)


@dataclass(frozen=True, slots=True)
class DeploymentReadiness:
    """Read-only readiness projection shared by every setup entrypoint."""

    state: ReadinessState
    platform_id: str
    project_root: str
    state_root: str
    evidence: tuple[str, ...]
    checks: tuple[ReadinessCheck, ...]

    def safe_dict(self) -> dict[str, object]:
        """Return serializable diagnostics without secrets."""
        return asdict(self)


def inspect_deployment(
    project: ProjectContext,
    env: Mapping[str, str] | None = None,
) -> DeploymentReadiness:
    """Inspect project setup without creating files or calling external services."""
    active_env = dict(os.environ if env is None else env)
    adapter = select_platform_adapter(active_env)
    detection = adapter.detect(active_env)
    checks: list[ReadinessCheck] = []

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

    active_model = _active_model_profile(config)
    checks.append(
        ReadinessCheck(
            "model",
            "pass" if active_model else "warning",
            (
                f"Active model: {active_model.profile_id}"
                if active_model
                else "No active model selected"
            ),
        )
    )
    credential_ready = True
    if active_model is not None:
        credential_status, credential_summary, credential_ready = _credential_readiness(
            active_model, active_env
        )
        checks.append(ReadinessCheck("model-credential", credential_status, credential_summary))
    managed_local_available = False
    if config is not None and config.model_source == "local":
        selection = config.local_model
        managed_local_available = (
            selection is not None and selection.resolved_path(project).exists()
        )
        checks.append(
            ReadinessCheck(
                "local-model-artifact",
                "pass" if managed_local_available else "warning",
                (
                    f"Selected local model is available: {selection.artifact_id}"
                    if managed_local_available and selection is not None
                    else (
                        "No downloaded local model is selected; "
                        "an existing local service is required"
                    )
                ),
            )
        )

    if config is not None:
        checks.append(
            ReadinessCheck(
                "policy",
                "pass",
                "Deployment policy is valid",
            )
        )
        action_mode = str(config.action_settings.confirmation_mode)
        checks.append(
            ReadinessCheck(
                "action-confirmation",
                "pass",
                f"Action confirmation: {action_mode}",
            )
        )
        coherent = active_model is not None and _configuration_is_coherent(config)
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
    elif managed_local_available and active_env.get("HEARTWOOD_LOCAL_RUNTIME_ACTIVE") != "1":
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
    platform_policy = adapter.default_policy_profile()
    store = ProjectConfigStore(
        project,
        ProjectConfig(platform_id=adapter.adapter_id, policy=platform_policy),
    )
    configured_connections: tuple[ModelConnection, ...] = ()
    if model_source == "stanford-ai-api-gateway":
        all_connections = model_connections_from_mapping(_stanford_connection_manifest())
        configured_connections = tuple(
            connection for connection in all_connections if connection.source == "platform"
        )
        policy = PolicyProfile.model_validate(
            {
                "schema_version": "heartwood.policy-profile.v1",
                "policy_id": f"{adapter.adapter_id}-stanford-ai-api-gateway",
                "platform_id": adapter.adapter_id,
                "deny_egress_by_default": True,
                "allowed_model_endpoints": [f"{_STANFORD_ROOT}/chat/completions"],
                "allowed_model_catalog_endpoints": [f"{_STANFORD_ROOT}/models"],
                "allowed_capability_tiers": ["supervised", "experimental"],
                "allowed_action_confirmation_modes": list(
                    platform_policy.allowed_action_confirmation_modes
                ),
                "credential_allowlist": ["STANFORD_AI_API_KEY"],
                "aggregate_count_floor": 20,
                "notes": "Stanford gateway route; data eligibility requires deployment approval.",
            }
        )
    elif model_source in {"anthropic", "local", "openai"}:
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
        return ProjectConfig(
            platform_id=adapter.adapter_id,
            model_source=model_source,
            action_settings=current.action_settings,
            model_settings=model_settings,
            additional_connections=additional_connections,
            policy=policy,
            local_model=current.local_model,
        )

    store.update(apply)
    return project.config_path


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


def _credential_readiness(
    profile: ModelProfile, env: Mapping[str, str]
) -> tuple[Literal["pass", "warning", "fail"], str, bool]:
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


def _configuration_is_coherent(config: ProjectConfig) -> bool:
    model = _active_model_profile(config)
    if model is None or config.model_source is None:
        return False
    if config.model_source == "local":
        source_matches = model.is_local
    elif config.model_source == "stanford-ai-api-gateway":
        source_matches = (
            model.policy_endpoint == f"{_STANFORD_ROOT}/chat/completions"
            and model.api_key_env == "STANFORD_AI_API_KEY"
            and any(
                connection.connection_id == "stanford-ai-api-gateway"
                for connection in config.additional_connections
            )
        )
    else:
        source_matches = model.profile_id == config.model_source
    if not source_matches:
        return False
    decision = ModelPolicyEngine(config.policy).evaluate(
        endpoint=model.policy_endpoint,
        capability_tier=model.capability_tier,
        action_confirmation_mode=config.action_settings.confirmation_mode,
        credential_reference=model.credential_reference,
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
    local_model = model_source == "local"
    if not allocation:
        return (
            (
                ReadinessCheck(
                    "slurm-allocation",
                    "warning",
                    (
                        "Local model is configured; request a Carina allocation to start it"
                        if local_model
                        else "No Slurm allocation; one is required only for local inference"
                    ),
                ),
                ReadinessCheck(
                    "job-scratch",
                    "warning",
                    "Job-local scratch will be checked after allocation",
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
                    f"Writable job-local scratch detected at {scratch_path}"
                    if scratch_ready
                    else "Active local-model allocation has no writable job scratch"
                    if local_model
                    else "No writable job-local scratch detected"
                ),
            ),
            ReadinessCheck(
                "gpu",
                "pass" if gpu else requirement_status,
                (
                    "NVIDIA GPU is visible"
                    if gpu
                    else "Active local-model allocation has no visible NVIDIA GPU"
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
    persistent_root = Path(env.get("HEARTWOOD_PLATFORM_HOME") or "/home/jupyter").resolve()
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
                "terra-gpu-runtime",
                "pass" if visible else "warning",
                (
                    "Terra NVIDIA runtime and attached GPU detected"
                    if visible
                    else "Terra NVIDIA runtime detected; attach a GPU before local GPU inference"
                ),
            )
        )
    elif gpu_runtime == "none":
        checks.append(
            ReadinessCheck(
                "terra-gpu-runtime",
                "pass",
                "Portable Terra runtime selected; local models use CPU inference",
            )
        )
    return tuple(checks)


def gpu_visible(env: Mapping[str, str]) -> bool:
    """Return whether the current process has evidence of an attached NVIDIA GPU."""
    visible = env.get("NVIDIA_VISIBLE_DEVICES") or env.get("CUDA_VISIBLE_DEVICES")
    configured = bool(visible and visible.lower() not in {"none", "void", "-1"})
    return configured or Path("/dev/nvidia0").exists()
