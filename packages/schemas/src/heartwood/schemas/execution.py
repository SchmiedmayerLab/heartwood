# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Shared bounded-work contracts for agent tasks and research evaluations."""

from pydantic import BaseModel, ConfigDict, Field


class ExecutionBudget(BaseModel):
    """Observed admission limits, not a provider-side spending or preemption cap."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    maximum_seconds: float = Field(default=300, gt=0, le=3600)
    maximum_model_calls: int = Field(default=20, gt=0, le=100)
    maximum_tokens: int = Field(default=100_000, gt=0)
    maximum_reported_cost_usd: float = Field(default=1, gt=0)
    maximum_actions: int = Field(default=30, gt=0, le=100)
