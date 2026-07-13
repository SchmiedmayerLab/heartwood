# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Read-only deployment readiness and persistent setup configuration."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, assert_never

from pydantic import ValidationError

from heartwood.adapters.platform import select_platform_adapter
from heartwood.gateway._action_settings import ActionSettingsError, ActionSettingsStore
from heartwood.gateway._model_catalog import ModelCatalogError, load_model_connections
from heartwood.gateway._model_settings import (
    ModelProfile,
    ModelSettingsError,
    ModelSettingsStore,
)
from heartwood.model_policy import ModelPolicyEngine
from heartwood.schemas import PolicyProfile

ReadinessState = Literal["ready", "setup-required", "recovery-required"]
ModelSource = Literal["local", "stanford-ai-api-gateway"]

_STANFORD_ROOT = "https://aiapi-prod.stanford.edu/v1"


@dataclass(frozen=True, slots=True)
class ReadinessCheck:
    """One content-free deployment readiness result."""

    check_id: str
    status: Literal["pass", "warning", "fail"]
    summary: str


@dataclass(frozen=True, slots=True)
class DeploymentReadiness:
    """Read-only readiness projection shared by setup entrypoints."""

    state: ReadinessState
    platform_id: str
    evidence: tuple[str, ...]
    checks: tuple[ReadinessCheck, ...]

    def safe_dict(self) -> dict[str, object]:
        """Return serializable diagnostics without secrets."""
        return asdict(self)


def inspect_deployment(
    workspace: Path,
    env: Mapping[str, str] | None = None,
) -> DeploymentReadiness:
    """Inspect setup state without creating files or calling external services."""
    active_env = dict(os.environ if env is None else env)
    adapter = select_platform_adapter(active_env)
    detection = adapter.detect(active_env)
    state_root = workspace.parent
    checks: list[ReadinessCheck] = []

    existing_parent = _existing_parent(state_root)
    writable = os.access(existing_parent, os.W_OK)
    checks.append(
        ReadinessCheck(
            "state-storage",
            "pass" if writable else "fail",
            (
                f"State root can be created under {existing_parent}"
                if writable
                else f"State root is not writable under {existing_parent}"
            ),
        )
    )
    setup_path = state_root / "setup.json"
    setup = _load_setup_marker(setup_path)
    setup_matches_platform = setup is not None and setup["platform_id"] == adapter.adapter_id
    checks.append(
        ReadinessCheck(
            "setup",
            ("pass" if setup_matches_platform else "fail" if setup_path.exists() else "warning"),
            (
                "Setup is complete"
                if setup_matches_platform
                else "Setup platform does not match the detected platform"
                if setup is not None
                else "Setup state is invalid"
                if setup_path.exists()
                else "Setup is incomplete"
            ),
        )
    )
    model_path = state_root / "models.json"
    active_model = _active_model_profile(model_path)
    model_summary = (
        f"Active model profile: {active_model.profile_id}"
        if active_model
        else ("Model settings are invalid" if model_path.exists() else "No active model selected")
    )
    checks.append(
        ReadinessCheck(
            "model",
            "pass" if active_model else ("fail" if model_path.exists() else "warning"),
            model_summary,
        )
    )
    if active_model is not None:
        credential_status, credential_summary = _credential_readiness(active_model, active_env)
        checks.append(ReadinessCheck("model-credential", credential_status, credential_summary))
    policy: PolicyProfile | None = None
    if setup is not None:
        policy_path = state_root / "policy.json"
        policy = _load_policy(policy_path)
        policy_ready = policy is not None and policy.platform_id == adapter.adapter_id
        checks.append(
            ReadinessCheck(
                "policy",
                "pass" if policy_ready else "fail",
                (
                    "Deployment policy is valid"
                    if policy_ready
                    else "Deployment policy is invalid or belongs to another platform"
                ),
            )
        )
        action_mode = _action_confirmation_mode(state_root / "actions.json")
        checks.append(
            ReadinessCheck(
                "action-confirmation",
                "pass" if action_mode is not None else "fail",
                (
                    f"Action confirmation mode: {action_mode}"
                    if action_mode is not None
                    else "Action confirmation settings are invalid or missing"
                ),
            )
        )
        configuration_ready = _configuration_is_coherent(
            setup,
            active_model,
            policy,
            action_mode,
            state_root / "model-connections.json",
        )
        checks.append(
            ReadinessCheck(
                "configuration",
                "pass" if configuration_ready else "fail",
                (
                    "Setup, model, policy, and connection agree"
                    if configuration_ready
                    else "Setup, model, policy, or connection do not agree"
                ),
            )
        )
    if adapter.adapter_id == "carina":
        allocation = bool(active_env.get("SLURM_JOB_ID"))
        checks.append(
            ReadinessCheck(
                "slurm-allocation",
                "pass" if allocation else "fail",
                (
                    "Active Slurm allocation detected"
                    if allocation
                    else "Carina setup requires an active Slurm compute allocation"
                ),
            )
        )
        scratch = active_env.get("LOCAL_SCRATCH_JOB")
        scratch_ready = bool(scratch and Path(scratch).is_dir())
        checks.append(
            ReadinessCheck(
                "job-scratch",
                "pass" if scratch_ready else "warning",
                f"Job-local scratch detected at {scratch}" if scratch_ready else "No job scratch",
            )
        )
        gpu = _gpu_visible(active_env)
        checks.append(
            ReadinessCheck(
                "gpu",
                "pass" if gpu else "warning",
                "NVIDIA GPU is visible" if gpu else "No NVIDIA GPU evidence detected",
            )
        )
    if any(check.status == "fail" for check in checks):
        state: ReadinessState = "recovery-required"
    elif setup is None or active_model is None:
        state = "setup-required"
    else:
        state = "ready"
    return DeploymentReadiness(state, adapter.adapter_id, detection.evidence, tuple(checks))


def persist_deployment_profile(
    workspace: Path,
    *,
    model_source: ModelSource,
    env: Mapping[str, str] | None = None,
) -> tuple[Path, Path, Path]:
    """Persist a non-secret connection, policy, and setup marker atomically."""
    active_env = dict(os.environ if env is None else env)
    adapter = select_platform_adapter(active_env)
    state_root = workspace.parent
    state_root.mkdir(parents=True, exist_ok=True)
    policy_path = state_root / "policy.json"
    connections_path = state_root / "model-connections.json"
    setup_path = state_root / "setup.json"
    platform_policy = adapter.default_policy_profile()
    if model_source == "stanford-ai-api-gateway":
        connections = _stanford_connection_manifest()
        policy: object = {
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
    else:
        connections = {"schema_version": "heartwood.model-connections.v1", "connections": []}
        policy = platform_policy.model_dump(mode="json")
    _atomic_json(connections_path, connections)
    _atomic_json(policy_path, policy)
    _atomic_json(
        setup_path,
        {
            "schema_version": "heartwood.setup.v1",
            "platform_id": adapter.adapter_id,
            "model_source": model_source,
        },
    )
    return setup_path, policy_path, connections_path


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


def _atomic_json(path: Path, value: object) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(value, file, indent=2, sort_keys=True)
            file.write("\n")
        temporary_path.chmod(0o600)
        temporary_path.replace(path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _existing_parent(path: Path) -> Path:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def _load_setup_marker(path: Path) -> dict[str, str] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "platform_id", "model_source"}
        or value.get("schema_version") != "heartwood.setup.v1"
        or not isinstance(value.get("platform_id"), str)
        or value.get("model_source") not in {"local", "stanford-ai-api-gateway"}
    ):
        return None
    return {
        "schema_version": "heartwood.setup.v1",
        "platform_id": str(value["platform_id"]),
        "model_source": str(value["model_source"]),
    }


def _active_model_profile(path: Path) -> ModelProfile | None:
    if not path.is_file():
        return None
    try:
        return ModelSettingsStore(path).load().profile()
    except ModelSettingsError:
        return None


def _credential_readiness(
    profile: ModelProfile, env: Mapping[str, str]
) -> tuple[Literal["pass", "warning", "fail"], str]:
    kind = profile.credential_kind
    if kind == "none":
        return "pass", "Selected model requires no credential"
    if kind == "environment":
        name = profile.api_key_env
        if not isinstance(name, str) or not name.strip():
            return "fail", "Selected model has an invalid environment credential reference"
        available = bool(env.get(name))
        summary = (
            f"Credential reference {name} is available"
            if available
            else f"Credential {name} is missing"
        )
        return (
            "pass" if available else "fail",
            summary,
        )
    if kind == "file":
        configured = profile.api_key_file
        available = isinstance(configured, str) and Path(configured).is_file()
        summary = (
            "Mounted credential file is available"
            if available
            else "Mounted credential file is missing"
        )
        return (
            "pass" if available else "fail",
            summary,
        )
    if kind == "managed-identity":
        return "warning", "Managed identity must be validated by the deployment"
    assert_never(kind)


def _load_policy(path: Path) -> PolicyProfile | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        return PolicyProfile.model_validate(value)
    except ValidationError:
        return None


def _action_confirmation_mode(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        return str(ActionSettingsStore(path).load().confirmation_mode)
    except ActionSettingsError:
        return None


def _configuration_is_coherent(
    setup: Mapping[str, str],
    model: ModelProfile | None,
    policy: PolicyProfile | None,
    action_mode: str | None,
    connections_path: Path,
) -> bool:
    if model is None or policy is None or action_mode is None:
        return False
    try:
        connections = load_model_connections(connections_path)
    except ModelCatalogError:
        return False
    source = setup["model_source"]
    if source == "local":
        source_matches = model.is_local and not any(
            item.connection_id == "stanford-ai-api-gateway" for item in connections
        )
    else:
        source_matches = (
            model.policy_endpoint == f"{_STANFORD_ROOT}/chat/completions"
            and model.api_key_env == "STANFORD_AI_API_KEY"
            and any(item.connection_id == "stanford-ai-api-gateway" for item in connections)
        )
    if not source_matches:
        return False
    decision = ModelPolicyEngine(policy).evaluate(
        endpoint=model.policy_endpoint,
        capability_tier=model.capability_tier,
        action_confirmation_mode=action_mode,
        credential_reference=model.credential_reference,
        decision_id="deployment-readiness",
        purpose="deployment readiness",
    )
    return decision.decision == "allow"


def _gpu_visible(env: Mapping[str, str]) -> bool:
    visible = env.get("NVIDIA_VISIBLE_DEVICES") or env.get("CUDA_VISIBLE_DEVICES")
    configured = bool(visible and visible.lower() not in {"none", "void", "-1"})
    return configured or Path("/dev/nvidia0").exists()
