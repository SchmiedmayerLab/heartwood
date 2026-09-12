# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Shared named project artifact locations, independent of observed file contents."""

from heartwood.schemas.experiments import ExperimentPath, ExperimentRecord
from heartwood.schemas.identifiers import WorkflowIdentifier


class ResearchArtifactPath(ExperimentRecord):
    """A resolved file location for a workflow or a proposed correction output."""

    artifact_id: WorkflowIdentifier
    path: ExperimentPath
