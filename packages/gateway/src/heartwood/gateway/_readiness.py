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
from typing import Literal

from pydantic import ValidationError

from heartwood.adapters.platform import select_platform_adapter
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
    checks.append(
        ReadinessCheck(
            "setup",
            "pass" if setup is not None else ("fail" if setup_path.exists() else "warning"),
            (
                "Setup is complete"
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
        f"Active model profile: {active_model['profile_id']}"
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
    if setup is not None:
        policy_path = state_root / "policy.json"
        policy_ready = _valid_policy(policy_path)
        checks.append(
            ReadinessCheck(
                "policy",
                "pass" if policy_ready else "fail",
                "Deployment policy is valid" if policy_ready else "Deployment policy is invalid",
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
            "allowed_action_confirmation_modes": ["always-confirm"],
            "credential_allowlist": ["STANFORD_AI_API_KEY"],
            "aggregate_count_floor": 20,
            "notes": "Stanford gateway route; data eligibility requires deployment approval.",
        }
    else:
        connections = {"schema_version": "heartwood.model-connections.v1", "connections": []}
        policy = adapter.default_policy_profile().model_dump(mode="json")
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


def _load_setup_marker(path: Path) -> dict[str, object] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or value.get("schema_version") != "heartwood.setup.v1":
        return None
    return value


def _active_model_profile(path: Path) -> dict[str, object] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    active = value.get("active_profile")
    profiles = value.get("profiles")
    if not isinstance(active, str) or not isinstance(profiles, list):
        return None
    for profile in profiles:
        if isinstance(profile, dict) and profile.get("profile_id") == active:
            return profile
    return None


def _credential_readiness(
    profile: Mapping[str, object], env: Mapping[str, str]
) -> tuple[Literal["pass", "warning", "fail"], str]:
    kind = profile.get("credential_kind")
    if kind == "none":
        return "pass", "Selected model requires no credential"
    if kind == "environment":
        name = profile.get("api_key_env")
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
        configured = profile.get("api_key_file")
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
    return "fail", "Selected model has an invalid credential reference"


def _valid_policy(path: Path) -> bool:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    try:
        PolicyProfile.model_validate(value)
    except ValidationError:
        return False
    return True


def _gpu_visible(env: Mapping[str, str]) -> bool:
    visible = env.get("NVIDIA_VISIBLE_DEVICES") or env.get("CUDA_VISIBLE_DEVICES")
    configured = bool(visible and visible.lower() not in {"none", "void", "-1"})
    return configured or Path("/dev/nvidia0").exists()
