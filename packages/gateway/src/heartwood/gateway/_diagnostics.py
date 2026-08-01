# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Stable, content-safe diagnostics shared by Heartwood interfaces."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class DiagnosticDefinition:
    """Recovery metadata for one stable readiness check."""

    code: str
    title: str
    next_action: str
    documentation_path: str
    sensitivity: Literal["public", "path-sensitive"] = "public"

    def safe_dict(self) -> dict[str, str]:
        """Return the serializable diagnostic definition."""
        return asdict(self)


# Codes are sequential within an owning area; 900-999 are generic fallbacks.
# Retired codes remain reserved so support records keep one durable meaning.
_NO_MODEL_SELECTION = DiagnosticDefinition(
    "HW-MODEL-001",
    "No model is selected",
    "Choose an available model connection and model.",
    "/models/connections/",
)

_DIAGNOSTICS: dict[str, DiagnosticDefinition] = {
    "project-storage": DiagnosticDefinition(
        "HW-PROJECT-001",
        "Project storage is unavailable",
        "Enter a writable project directory and run Heartwood again.",
        "/reference/troubleshooting/#project-storage",
        "path-sensitive",
    ),
    "project-state": DiagnosticDefinition(
        "HW-PROJECT-002",
        "Project setup needs attention",
        "Run Heartwood from the intended project and complete the guided setup.",
        "/start/project/#heartwood-project-state",
        "path-sensitive",
    ),
    "project-boundary": DiagnosticDefinition(
        "HW-PROJECT-003",
        "Choose a dedicated project directory",
        "Create and enter a folder that contains only the files Heartwood may access.",
        "/start/project/#choose-the-project-boundary",
        "path-sensitive",
    ),
    "configuration": DiagnosticDefinition(
        "HW-SETUP-001",
        "Project configuration needs attention",
        "Open setup and select a model connection for this environment.",
        "/start/#choose-a-model-connection",
    ),
    "model-source": _NO_MODEL_SELECTION,
    "model": _NO_MODEL_SELECTION,
    "model-credential": DiagnosticDefinition(
        "HW-CREDENTIAL-001",
        "Model credential is unavailable",
        "Provide the credential through setup or the deployment secret mechanism.",
        "/models/connections/#credentials",
    ),
    "credential-isolation": DiagnosticDefinition(
        "HW-CREDENTIAL-002",
        "Model credential isolation is unavailable",
        "Use Review Every Action or select a credential-free or platform-isolated model route.",
        "/operate/security/#model-credential-isolation",
    ),
    "local-model-artifact": DiagnosticDefinition(
        "HW-MODEL-002",
        "Heartwood-managed model files are unavailable",
        "Choose, download, or import a compatible Heartwood-managed model.",
        "/models/choose-managed/",
        "path-sensitive",
    ),
    "configuration-coherence": DiagnosticDefinition(
        "HW-SETUP-002",
        "Model and policy settings do not agree",
        "Open setup and select the model connection again.",
        "/reference/troubleshooting/#configuration",
    ),
    "agent-runtime": DiagnosticDefinition(
        "HW-AGENT-001",
        "Agent runtime is unavailable",
        "Repair or reinstall Heartwood, then run `heartwood doctor` again.",
        "/reference/troubleshooting/#agent-runtime",
    ),
    "agent-action": DiagnosticDefinition(
        "HW-AGENT-002",
        "An agent action failed",
        "Review the action set and model connection, then try the task again.",
        "/reference/troubleshooting/#hw-agent-002-an-agent-action-failed",
    ),
    "agent-conversation": DiagnosticDefinition(
        "HW-AGENT-003",
        "The agent conversation stopped",
        "Review Activity & audit and the model connection, then start the task again.",
        "/reference/troubleshooting/#hw-agent-003-the-agent-conversation-stopped",
    ),
    "agent-worker": DiagnosticDefinition(
        "HW-AGENT-004",
        "The agent worker stopped",
        "Review Activity & audit, run `heartwood doctor`, and try the task again.",
        "/reference/troubleshooting/#hw-agent-004-the-agent-worker-stopped",
    ),
    "agent-state": DiagnosticDefinition(
        "HW-AGENT-005",
        "The agent cannot perform that operation in its current state",
        "Review the current task or action set before trying the operation again.",
        "/reference/troubleshooting/#hw-agent-005-the-agent-cannot-perform-that-operation",
    ),
    "agent-action-outcome": DiagnosticDefinition(
        "HW-AGENT-006",
        "An approved action has an unknown outcome",
        "Verify the project files and continue in a new session; do not repeat the action blindly.",
        "/reference/troubleshooting/#hw-agent-006-an-approved-action-has-an-unknown-outcome",
    ),
    "agent-turn-outcome": DiagnosticDefinition(
        "HW-AGENT-007",
        "An agent turn has an unknown outcome",
        "Inspect the session and continue in a new session; do not repeat the task blindly.",
        "/reference/troubleshooting/#hw-agent-007-an-agent-turn-has-an-unknown-outcome",
    ),
    "agent-unknown": DiagnosticDefinition(
        "HW-AGENT-999",
        "The agent runtime reported an error",
        "Review Activity & audit, run `heartwood doctor`, and try the task again.",
        "/reference/troubleshooting/#hw-agent-999-the-agent-runtime-reported-an-error",
    ),
    "slurm-allocation": DiagnosticDefinition(
        "HW-COMPUTE-001",
        "A compute allocation may be required",
        "Review the proposed allocation when Heartwood starts the managed model.",
        "/platforms/carina/#heartwood-managed-gpu-model",
    ),
    "job-scratch": DiagnosticDefinition(
        "HW-COMPUTE-002",
        "Allocation scratch storage is unavailable",
        "Use an allocation that provides writable job scratch storage.",
        "/platforms/carina/#heartwood-managed-gpu-model",
        "path-sensitive",
    ),
    "gpu": DiagnosticDefinition(
        "HW-COMPUTE-003",
        "A compatible GPU is unavailable",
        "Select a hosted connection, a CPU-compatible model, or GPU-enabled compute.",
        "/models/run-with-heartwood/#hardware",
    ),
    "terra-project-storage": DiagnosticDefinition(
        "HW-TERRA-001",
        "Choose a dedicated Terra project directory",
        "Create and enter a project below /home/jupyter before starting Heartwood.",
        "/platforms/terra/#create-a-project-directory",
        "path-sensitive",
    ),
    "terra-gpu": DiagnosticDefinition(
        "HW-TERRA-002",
        "Terra GPU support is unavailable",
        "Use hosted inference or attach a supported GPU before selecting a GPU model.",
        "/platforms/terra/#choose-the-image-and-compute",
    ),
    "gateway-ingress": DiagnosticDefinition(
        "HW-INGRESS-001",
        "Gateway ingress configuration is unsafe",
        "Use loopback or configure one exact trusted proxy route.",
        "/operate/platform-integration/#gateway-ingress",
    ),
    "gateway-request": DiagnosticDefinition(
        "HW-INGRESS-002",
        "Gateway request does not match the configured route",
        "Use the exact gateway URL supplied by this deployment.",
        "/reference/troubleshooting/#hw-ingress-002-gateway-request-does-not-match-the-configured-route",
    ),
}

_FALLBACK_DIAGNOSTIC = DiagnosticDefinition(
    "HW-ENV-999",
    "Environment check needs attention",
    "Run `heartwood doctor` and review the failed check.",
    "/reference/troubleshooting/#hw-env-999-environment-check-needs-attention",
)


def diagnostic_for(check_id: str) -> DiagnosticDefinition:
    """Return a stable diagnostic, including a conservative generic fallback."""
    return _DIAGNOSTICS.get(check_id, _FALLBACK_DIAGNOSTIC)


def diagnostic_catalog() -> tuple[DiagnosticDefinition, ...]:
    """Return the unique public diagnostic catalog ordered by code."""
    return tuple(sorted({*_DIAGNOSTICS.values(), _FALLBACK_DIAGNOSTIC}, key=lambda item: item.code))
