# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Shared local-model context limits and resource-aware planning."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

DEFAULT_LOCAL_CONTEXT_WINDOW = 32_768
MINIMUM_LOCAL_CONTEXT_WINDOW = 2_048
MINIMUM_AGENT_CONTEXT_WINDOW = 16_384
MINIMUM_AGENT_RUNTIME_CONTEXT_WINDOW = 18_432
MAXIMUM_LOCAL_CONTEXT_WINDOW = 1_048_576
LOCAL_CONTEXT_WINDOW_TIERS = (
    MINIMUM_AGENT_RUNTIME_CONTEXT_WINDOW,
    32_768,
    65_536,
    131_072,
    262_144,
    524_288,
    1_048_576,
)

_CONTEXT_BYTES_PER_TOKEN = 128 * 1024
_MEMORY_UTILIZATION = 0.8
_GIB = 1024**3

type LocalRuntimeKind = Literal["llama-cpp", "vllm"]


@dataclass(frozen=True, slots=True)
class LocalContextPlan:
    """One deterministic effective context choice for a managed runtime launch."""

    model_limit: int
    effective_window: int
    resource: Literal["RAM", "GPU memory"]
    available_bytes: int | None
    estimated_required_bytes: int | None
    reason: str


def plan_local_context_window(
    *,
    model_limit: int,
    model_size_bytes: int | None,
    runtime: LocalRuntimeKind,
    available_memory_bytes: int | None,
) -> LocalContextPlan:
    """Choose a stable context tier bounded by model capacity and available memory."""
    if not MINIMUM_LOCAL_CONTEXT_WINDOW <= model_limit <= MAXIMUM_LOCAL_CONTEXT_WINDOW:
        raise ValueError(
            f"model context limit must be between {MINIMUM_LOCAL_CONTEXT_WINDOW} and "
            f"{MAXIMUM_LOCAL_CONTEXT_WINDOW} tokens"
        )
    if model_limit < MINIMUM_AGENT_RUNTIME_CONTEXT_WINDOW:
        raise ValueError(
            "Heartwood agent sessions require a model context window of at least "
            f"{MINIMUM_AGENT_RUNTIME_CONTEXT_WINDOW:,} tokens; this model supports "
            f"{model_limit:,}"
        )
    resource: Literal["RAM", "GPU memory"] = "GPU memory" if runtime == "vllm" else "RAM"
    fallback = MINIMUM_AGENT_RUNTIME_CONTEXT_WINDOW
    if model_size_bytes is None or model_size_bytes <= 0 or available_memory_bytes is None:
        return LocalContextPlan(
            model_limit=model_limit,
            effective_window=fallback,
            resource=resource,
            available_bytes=available_memory_bytes,
            estimated_required_bytes=(
                estimate_local_runtime_memory(
                    context_window=fallback,
                    model_size_bytes=model_size_bytes,
                    runtime=runtime,
                )
                if model_size_bytes is not None and model_size_bytes > 0
                else None
            ),
            reason=(
                f"Selected the minimum {fallback:,}-token agent context because "
                f"{resource.lower()} or model-size information is unavailable."
            ),
        )

    usable_memory = int(available_memory_bytes * _MEMORY_UTILIZATION)
    fixed_bytes, model_multiplier = _runtime_memory_parameters(runtime)
    context_budget = usable_memory - int(model_size_bytes * model_multiplier) - fixed_bytes
    resource_limit = max(0, context_budget // _CONTEXT_BYTES_PER_TOKEN)
    bounded_limit = min(model_limit, resource_limit)
    if bounded_limit < MINIMUM_AGENT_RUNTIME_CONTEXT_WINDOW:
        minimum_required = estimate_local_runtime_memory(
            context_window=MINIMUM_AGENT_RUNTIME_CONTEXT_WINDOW,
            model_size_bytes=model_size_bytes,
            runtime=runtime,
        )
        raise ValueError(
            f"available {resource.lower()} cannot support the minimum "
            f"{MINIMUM_AGENT_RUNTIME_CONTEXT_WINDOW:,}-token agent context; "
            f"the runtime footprint estimate is {minimum_required / _GIB:.1f} GiB before "
            f"headroom and {available_memory_bytes / _GIB:.1f} GiB is available"
        )
    effective = _tier_at_or_below(
        bounded_limit,
        MINIMUM_AGENT_RUNTIME_CONTEXT_WINDOW,
    )
    required = estimate_local_runtime_memory(
        context_window=effective,
        model_size_bytes=model_size_bytes,
        runtime=runtime,
    )
    if effective == model_limit:
        reason = f"Selected the model's full {effective:,}-token context."
    else:
        reason = (
            f"Selected {effective:,} of {model_limit:,} supported tokens to retain "
            f"conservative {resource.lower()} headroom."
        )
    return LocalContextPlan(
        model_limit=model_limit,
        effective_window=effective,
        resource=resource,
        available_bytes=available_memory_bytes,
        estimated_required_bytes=required,
        reason=reason,
    )


def managed_model_token_budgets(context_window: int) -> tuple[int, int]:
    """Return OpenHands input capacity and output budget for one runtime window."""
    if not MINIMUM_AGENT_RUNTIME_CONTEXT_WINDOW <= context_window <= MAXIMUM_LOCAL_CONTEXT_WINDOW:
        raise ValueError(
            f"managed agent context window must be between "
            f"{MINIMUM_AGENT_RUNTIME_CONTEXT_WINDOW} and "
            f"{MAXIMUM_LOCAL_CONTEXT_WINDOW} tokens"
        )
    output_budget = min(4096, max(512, context_window // 8))
    output_budget = min(output_budget, context_window - MINIMUM_AGENT_CONTEXT_WINDOW)
    return context_window - output_budget, output_budget


def managed_model_request_body(model_type: str | None) -> dict[str, object]:
    """Return runtime-native request defaults for a managed model family."""
    if model_type == "qwen3":
        return {"chat_template_kwargs": {"enable_thinking": False}}
    return {}


def managed_model_native_tool_calling(tool_call_parser: str | None) -> bool:
    """Return whether OpenHands should use the runtime's structured tool calls."""
    return tool_call_parser in {"hermes", "openai", "qwen3_coder"}


def _tier_at_or_below(limit: int, fallback: int) -> int:
    eligible = [tier for tier in LOCAL_CONTEXT_WINDOW_TIERS if tier <= limit]
    return max(eligible) if eligible else fallback


def _runtime_memory_parameters(runtime: LocalRuntimeKind) -> tuple[int, float]:
    return (2 * _GIB, 1.15) if runtime == "vllm" else (4 * _GIB, 1.25)


def estimate_local_runtime_memory(
    *,
    context_window: int,
    model_size_bytes: int,
    runtime: LocalRuntimeKind,
) -> int:
    """Estimate model, runtime, and context memory with conservative headroom."""
    fixed_bytes, model_multiplier = _runtime_memory_parameters(runtime)
    return (
        int(model_size_bytes * model_multiplier)
        + fixed_bytes
        + (context_window * _CONTEXT_BYTES_PER_TOKEN)
    )
