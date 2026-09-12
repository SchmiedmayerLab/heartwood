# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Shared budget boundaries distinguish observed completion from new admission."""

import pytest
from pydantic import ValidationError

from heartwood.schemas.execution import ExecutionBudget, ExecutionUsage


@pytest.mark.parametrize(
    ("field", "limit", "maximum"),
    [
        ("elapsed_seconds", "seconds", 300),
        ("model_calls", "model_calls", 20),
        ("input_tokens", "tokens", 100_000),
        ("reported_cost_usd", "reported_cost_usd", 1),
        ("proposed_actions", "actions", 30),
    ],
)
@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_measured_boundaries(field: str, limit: str, maximum: int, offset: int) -> None:
    usage = ExecutionUsage.model_validate({"elapsed_seconds": 0, field: maximum + offset})
    assert usage.exceeded_limits(ExecutionBudget()) == ((limit,) if offset > 0 else ())
    assert usage.exhausted_limits(ExecutionBudget()) == ((limit,) if offset >= 0 else ())


def test_unknown_measurements_remain_unknown_and_known_tokens_form_a_lower_bound() -> None:
    unknown = ExecutionUsage(elapsed_seconds=1)
    assert unknown.exhausted_limits(ExecutionBudget()) == ()
    assert unknown.input_tokens is unknown.output_tokens is unknown.reported_cost_usd is None
    partial = ExecutionUsage(elapsed_seconds=1, output_tokens=100_000)
    assert partial.exhausted_limits(ExecutionBudget()) == ("tokens",)
    assert partial.exceeded_limits(ExecutionBudget()) == ()
    combined = ExecutionUsage(elapsed_seconds=1, output_tokens=40_000, input_tokens=60_001)
    assert combined.exceeded_limits(ExecutionBudget()) == ("tokens",)


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf")])
@pytest.mark.parametrize(
    "field",
    ["elapsed_seconds", "reported_cost_usd", "input_tokens", "model_calls", "proposed_actions"],
)
def test_invalid_consumption_cannot_disable_a_limit(field: str, value: float) -> None:
    with pytest.raises(ValidationError):
        ExecutionUsage.model_validate({"elapsed_seconds": 0, field: value})


def test_stage_measurement_subtracts_only_known_counters() -> None:
    baseline = ExecutionUsage(elapsed_seconds=2, model_calls=3, input_tokens=10)
    observed = ExecutionUsage(
        elapsed_seconds=5,
        model_calls=5,
        input_tokens=24,
        output_tokens=40,
    )
    delta = observed.since(baseline)
    assert delta.model_calls == 2
    assert delta.input_tokens == 14
    assert delta.elapsed_seconds == 3
    assert delta.output_tokens is None
    assert delta.reported_cost_usd is None


@pytest.mark.parametrize(
    "field",
    ["elapsed_seconds", "model_calls", "input_tokens", "reported_cost_usd", "proposed_actions"],
)
def test_stage_measurement_rejects_reset_counters(field: str) -> None:
    baseline = ExecutionUsage.model_validate({"elapsed_seconds": 0, field: 4})
    observed = ExecutionUsage.model_validate({"elapsed_seconds": 0, field: 3})
    with pytest.raises(ValueError, match="decreased"):
        observed.since(baseline)
