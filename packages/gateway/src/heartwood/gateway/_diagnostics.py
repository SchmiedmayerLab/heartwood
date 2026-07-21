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
