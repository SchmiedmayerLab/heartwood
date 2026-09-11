# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Shared identifiers for research definitions and evidence references."""

from typing import Annotated

from pydantic import StringConstraints

type WorkflowIdentifier = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")]
