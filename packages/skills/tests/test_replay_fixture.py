# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Tests for synthetic replay fixtures."""

from __future__ import annotations

from pathlib import Path

from heartwood.skills import LocalSkillVerifier, load_replay_fixture


def test_synthetic_omop_replay_fixture_matches_verified_skills() -> None:
    fixture = load_replay_fixture(Path("evals/replay/synthetic-omop-workflow.json"))
    verifier = LocalSkillVerifier(Path("skills/verified"))
    verified_skill_ids = {
        verifier.load_manifest(Path("skills/verified") / name).skill_id
        for name in ("omop-cohort-summary", "aggregate-export", "baseline-model")
    }
    assert set(fixture.skills) == verified_skill_ids
    assert [call.tool_name for call in fixture.expected_tool_calls] == [
        "omop-cohort-summary",
        "aggregate-export",
        "baseline-model",
    ]
    assert "agent_message.emitted" in fixture.expected_audit_events
    assert "tool_call.proposed" in fixture.expected_audit_events
    assert "confirmation.resolved" in fixture.expected_audit_events
    assert fixture.expected_outputs["aggregate_export"] == {
        "exported": False,
        "suppressed": True,
        "aggregate_count_floor": 20,
        "suppressed_count_exported": False,
    }
