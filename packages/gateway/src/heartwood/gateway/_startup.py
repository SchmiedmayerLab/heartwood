# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Shared startup planning for terminal, browser, and notebook interfaces."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Literal

from heartwood.adapters import PlatformCapabilities
from heartwood.adapters.platform import select_platform_adapter
from heartwood.gateway._project import ProjectContext
from heartwood.gateway._readiness import DeploymentReadiness, inspect_deployment

type InterfaceKind = Literal["terminal", "web", "notebook"]
type SetupPhase = Literal[
    "project-review",
    "connection-required",
    "credential-required",
    "model-required",
    "compute-required",
    "ready",
    "recovery-required",
]


@dataclass(frozen=True, slots=True)
class StartupPlan:
    """One content-safe plan for opening Heartwood in the requested interface."""

    phase: SetupPhase
    interface: InterfaceKind
    platform_id: str
    project_root: str
    state_root: str
    summary: str
    next_action: str
    access_url: str | None
    requires_compute: bool
    requires_confirmation: bool
    interface_supported: bool
    readiness: DeploymentReadiness
    capabilities: PlatformCapabilities

    def safe_dict(self) -> dict[str, object]:
        """Return the complete serializable startup projection."""
        return {
            **asdict(self),
            "readiness": self.readiness.safe_dict(),
            "capabilities": self.capabilities.safe_dict(),
        }


def plan_startup(
    project: ProjectContext,
    *,
    interface: InterfaceKind = "terminal",
    port: int = 8767,
    env: Mapping[str, str] | None = None,
) -> StartupPlan:
    """Inspect a project and return the next product-level startup action."""
    if port < 1 or port > 65_535:
        raise ValueError("port must be between 1 and 65535")
    active_env = dict(os.environ if env is None else env)
    adapter = select_platform_adapter(active_env)
    capabilities = adapter.capabilities()
    readiness = inspect_deployment(project, active_env)
    supported = interface in capabilities.interfaces
    phase = _setup_phase(readiness)
    if not supported:
        phase = "recovery-required"
        summary = f"{capabilities.display_name} does not provide the {interface} interface."
        next_action = f"Use the {capabilities.interfaces[0]} interface in this environment."
    else:
        summary, next_action = _phase_copy(phase, interface=interface)
    return StartupPlan(
        phase=phase,
        interface=interface,
        platform_id=adapter.adapter_id,
        project_root=str(project.root),
        state_root=str(project.state_root),
        summary=summary,
        next_action=next_action,
        access_url=_access_url(
            interface=interface,
            platform_id=adapter.adapter_id,
            port=port,
        ),
        requires_compute=phase == "compute-required",
        requires_confirmation=phase == "compute-required" and capabilities.scheduler == "slurm",
        interface_supported=supported,
        readiness=readiness,
        capabilities=capabilities,
    )


def _setup_phase(readiness: DeploymentReadiness) -> SetupPhase:
    if readiness.state == "recovery-required":
        return "recovery-required"
    if readiness.state == "compute-required":
        return "compute-required"
    if readiness.state == "ready":
        return "ready"
    statuses = {check.check_id: check.status for check in readiness.checks}
    if statuses.get("project-state") == "warning":
        return "project-review"
    if statuses.get("model-credential") == "warning":
        return "credential-required"
    if statuses.get("model") == "warning":
        return "connection-required" if statuses.get("model-source") != "pass" else "model-required"
    return "model-required"


def _phase_copy(phase: SetupPhase, *, interface: InterfaceKind) -> tuple[str, str]:
    if phase == "project-review":
        return (
            "Review this project before Heartwood creates private project state.",
            "Confirm the project and choose a model connection.",
        )
    if phase == "connection-required":
        return "Choose where the model runs.", "Select a model connection in setup."
    if phase == "credential-required":
        return "The selected model needs a credential.", "Provide the credential in setup."
    if phase == "model-required":
        return "Choose a model for this project.", "Select an available model in setup."
    if phase == "compute-required":
        return (
            "The selected Heartwood-managed model is ready to start.",
            "Review the compute and runtime plan, then start Heartwood.",
        )
    if phase == "ready":
        return f"Heartwood is ready in the {interface} interface.", "Start or resume a session."
    return (
        "Heartwood found a problem that needs attention.",
        "Review the failed check and its suggested next action.",
    )


def _access_url(
    *,
    interface: InterfaceKind,
    platform_id: str,
    port: int,
) -> str | None:
    if interface != "web":
        return None
    if platform_id in {"carina", "terra"}:
        return None
    return f"http://127.0.0.1:{port}/"
