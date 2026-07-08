# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Synthetic replay fixture schema and loader."""

from __future__ import annotations

import json
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class _ReplayRecord(BaseModel):
    """Base model for immutable replay fixture records."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)


class ExpectedToolCall(_ReplayRecord):
    """Expected tool call in a replay fixture."""

    skill_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    output_ref: str = Field(min_length=1)


class ExpectedPolicyDecision(_ReplayRecord):
    """Expected policy decision in a replay fixture."""

    decision_id: str = Field(min_length=1)
    decision: Literal["allow", "deny"]
    reason: str = Field(min_length=1)


class ReplayFixture(_ReplayRecord):
    """Golden synthetic workflow replay fixture."""

    schema_version: Literal["heartwood.replay-fixture.v1"] = "heartwood.replay-fixture.v1"
    fixture_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    skills: tuple[str, ...] = Field(min_length=1)
    expected_tool_calls: tuple[ExpectedToolCall, ...] = Field(min_length=1)
    expected_policy_decisions: tuple[ExpectedPolicyDecision, ...] = Field(min_length=1)
    expected_audit_events: tuple[str, ...] = Field(min_length=1)
    expected_outputs: dict[str, JsonValue] = Field(default_factory=dict)
    expected_attestations: tuple[str, ...] = Field(default_factory=tuple)


def load_replay_fixture(path: Path) -> ReplayFixture:
    """Load and validate a synthetic replay fixture."""
    return ReplayFixture.model_validate(json.loads(path.read_text(encoding="utf-8")))
