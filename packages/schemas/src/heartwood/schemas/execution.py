# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Shared bounded-work contracts for agent tasks and research evaluations."""

from __future__ import annotations

from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class NativeTaskExecution(BaseModel):
    """Observed native task lifetime, not queue time or provider request concurrency."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    clock_id: UUID
    started_seconds: float = Field(ge=0, strict=True)
    finished_seconds: float = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def ordered_interval(self) -> Self:
        """Reject an invalid interval rather than inventing a duration."""
        if self.finished_seconds < self.started_seconds:
            raise ValueError("Native task execution cannot finish before it starts")
        return self

    def overlap_seconds(self, other: NativeTaskExecution) -> float:
        """Only compare observations from the same process-local monotonic clock."""
        if self.clock_id != other.clock_id:
            raise ValueError("Native task observations use different clocks")
        return max(
            0.0,
            min(self.finished_seconds, other.finished_seconds)
            - max(self.started_seconds, other.started_seconds),
        )


class ExecutionBudget(BaseModel):
    """Observed admission limits, not a provider-side spending or preemption cap."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        json_schema_serialization_defaults_required=True,
    )

    maximum_seconds: float = Field(default=300, gt=0, le=3600)
    maximum_model_calls: int = Field(default=20, gt=0, le=100)
    maximum_tokens: int = Field(default=100_000, gt=0)
    maximum_reported_cost_usd: float = Field(default=1, gt=0)
    maximum_actions: int = Field(default=30, gt=0, le=100)


type ExecutionLimit = Literal["seconds", "model_calls", "tokens", "reported_cost_usd", "actions"]


class ExecutionUsage(BaseModel):
    """Observed consumption; unavailable provider measurements remain unknown."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    model_calls: int | None = Field(default=None, ge=0)
    reported_cost_usd: float | None = Field(default=None, ge=0)
    proposed_actions: int | None = Field(default=None, ge=0)
    elapsed_seconds: float = Field(ge=0)

    def since(self, baseline: ExecutionUsage) -> ExecutionUsage:
        """Subtract known counters; unknown measurements stay unknown and resets fail closed."""
        values: dict[str, int | float | None] = {}
        for name, current in self.model_dump().items():
            previous = getattr(baseline, name)
            if current is None or previous is None:
                values[name] = None
            elif current < previous:
                raise ValueError("Execution counters decreased; the measurement is unavailable")
            else:
                values[name] = current - previous
        return ExecutionUsage.model_validate(values)

    def exhausted_limits(self, budget: ExecutionBudget) -> tuple[ExecutionLimit, ...]:
        """Limits that prevent admitting more work, including exactly reached limits."""
        return self._limits(budget, include_equal=True)

    def exceeded_limits(self, budget: ExecutionBudget) -> tuple[ExecutionLimit, ...]:
        """Observed overruns; finishing exactly at a maximum is not an overrun."""
        return self._limits(budget, include_equal=False)

    def _limits(
        self, budget: ExecutionBudget, *, include_equal: bool
    ) -> tuple[ExecutionLimit, ...]:
        # A known component is a lower bound even when the other token count is unknown.
        tokens = (
            None
            if self.input_tokens is None and self.output_tokens is None
            else (self.input_tokens or 0) + (self.output_tokens or 0)
        )
        dimensions: tuple[tuple[ExecutionLimit, float | None, float], ...] = (
            ("seconds", self.elapsed_seconds, budget.maximum_seconds),
            ("model_calls", self.model_calls, budget.maximum_model_calls),
            ("tokens", tokens, budget.maximum_tokens),
            ("reported_cost_usd", self.reported_cost_usd, budget.maximum_reported_cost_usd),
            ("actions", self.proposed_actions, budget.maximum_actions),
        )
        return tuple(
            name
            for name, observed, maximum in dimensions
            if observed is not None
            and (observed > maximum or (include_equal and observed == maximum))
        )
