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
    summary: str

    def safe_dict(self) -> dict[str, object]:
        """Return the interface-safe isolation assessment."""
        return asdict(self)

    @property
    def unattended_actions_allowed(self) -> bool:
        """Return whether actions may run unreviewed beside this model credential.

        An unconfigured or unassessed route is not treated as isolated.
        """
        return self.status in {"not-required", "qualified"}


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
            summary="Choose a model before Heartwood evaluates credential isolation.",
        )
    if profile.credential_kind == "none":
        return CredentialIsolation(
            status="not-required",
            boundary="credential-free",
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
            summary=("The deployment isolates model authentication from the agent tool runtime."),
        )
    return CredentialIsolation(
        status="review-required",
        boundary="application-scrubbed",
        summary=(
            "Heartwood removes the model credential from tool inputs, but this deployment "
            "does not provide a separate process or platform identity boundary."
        ),
    )
