# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Model-credential isolation decisions shared by every interface."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass
from typing import Literal

from heartwood.adapters import PlatformCapabilities
from heartwood.gateway._model_catalog import ModelConnection, matching_model_connection
from heartwood.gateway._model_settings import ModelProfile
from heartwood.schemas import ActionConfirmationMode

type CredentialIsolationStatus = Literal[
    "not-configured",
    "not-required",
    "review-required",
    "qualified",
]
type CredentialIsolationBoundary = Literal[
    "none",
    "credential-free",
    "application-scrubbed",
    "platform-isolated",
]


@dataclass(frozen=True, slots=True)
class CredentialIsolation:
    """Content-safe assessment of model authentication relative to agent tools."""

    status: CredentialIsolationStatus
    boundary: CredentialIsolationBoundary
    unattended_actions_allowed: bool
    summary: str

    def safe_dict(self) -> dict[str, object]:
        """Return the interface-safe isolation assessment."""
        return asdict(self)

    def allows(self, mode: ActionConfirmationMode) -> bool:
        """Return whether the boundary permits the requested confirmation mode."""
        return mode == "always-confirm" or self.unattended_actions_allowed


def assess_credential_isolation(
    profile: ModelProfile | None,
    capabilities: PlatformCapabilities,
    *,
    model_source: str | None,
    model_connections: Iterable[ModelConnection] = (),
) -> CredentialIsolation:
    """Classify the active model route without resolving credential material."""
    if profile is None:
        return CredentialIsolation(
            status="not-configured",
            boundary="none",
            unattended_actions_allowed=True,
            summary="Choose a model before Heartwood evaluates credential isolation.",
        )
    if profile.credential_kind == "none":
        return CredentialIsolation(
            status="not-required",
            boundary="credential-free",
            unattended_actions_allowed=True,
            summary="The selected model route does not place a credential in Heartwood.",
        )
    matching_connection = matching_model_connection(profile, model_connections)
    if (
        profile.credential_kind == "managed-identity"
        and profile.profile_id == model_source
        and model_source in capabilities.platform_isolated_model_sources
        and matching_connection is not None
        and matching_connection.connection_id == model_source
    ):
        return CredentialIsolation(
            status="qualified",
            boundary="platform-isolated",
            unattended_actions_allowed=True,
            summary=("The deployment isolates model authentication from the agent tool runtime."),
        )
    return CredentialIsolation(
        status="review-required",
        boundary="application-scrubbed",
        unattended_actions_allowed=False,
        summary=(
            "Heartwood removes the model credential from tool inputs, but this deployment "
            "does not provide a separate process or platform identity boundary. Review every "
            "action."
        ),
    )


def credential_isolation_unavailable_reason(
    isolation: CredentialIsolation,
    mode: ActionConfirmationMode,
) -> str | None:
    """Return an approachable reason when a confirmation mode is unsafe."""
    if isolation.allows(mode):
        return None
    return (
        "Unavailable because the selected model credential is not isolated from the agent "
        "tool runtime. Use Review Every Action or a credential-free or platform-isolated route."
    )
