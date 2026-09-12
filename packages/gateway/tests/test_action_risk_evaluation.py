# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Non-executing conformance evaluation of the real production analyzer ensemble."""

from __future__ import annotations

import json

import pytest

from heartwood.compliance.action_risk import action_risk_corpus, assess_action_risk
from heartwood.gateway.risk_evaluation import evaluate_action_risk
from heartwood.schemas.action_risk import ActionRiskPolicy


def test_review_every_action_keeps_the_whole_corpus_behind_confirmation() -> None:
    corpus = action_risk_corpus()
    report = evaluate_action_risk(corpus, mode="always-confirm")
    assert len(report.measurements) == len(corpus.probes)
    assert all(item.confirmation_required for item in report.measurements)
    assert report.false_approvals == 0
    assert report.unnecessary_confirmations == sum(
        not probe.must_confirm for probe in corpus.probes
    )
    assert report.model_calls == report.reported_cost_usd == 0
    assert assess_action_risk(
        corpus,
        report,
        policy=ActionRiskPolicy(
            deployment="synthetic-review-every-action",
            maximum_unnecessary_confirmation_fraction=1,
            maximum_mean_latency_seconds=10,
        ),
    ).passed


def test_unknown_and_known_threats_require_confirmation_with_production_policy() -> None:
    corpus = action_risk_corpus()
    report = evaluate_action_risk(corpus, mode="confirm-risky")
    for item in report.measurements:
        if item.case_id.endswith("-unknown"):
            assert item.observed_risk == "UNKNOWN"
            assert item.confirmation_required
        if item.category in ("injected", "network") or item.case_id.startswith("delete-results"):
            assert item.confirmation_required
    assert report.unknown_classifications >= len(corpus.probes) // 2
    assert any(
        not item.confirmation_required for item in report.measurements if not item.must_confirm
    )


def test_results_never_hide_review_required_actions_that_the_policy_permits() -> None:
    corpus = action_risk_corpus()
    report = evaluate_action_risk(corpus, mode="confirm-risky")
    assessment = assess_action_risk(
        corpus,
        report,
        policy=ActionRiskPolicy(
            deployment="synthetic-strict",
            maximum_unnecessary_confirmation_fraction=1,
            maximum_mean_latency_seconds=10,
        ),
    )
    # An upstream improvement may eliminate current misses. Until then, every
    # measured false approval must prevent a passing deployment assessment.
    assert ("false-approval" in assessment.reasons) == (report.false_approvals > 0)
    if report.false_approvals:
        assert not assessment.passed


def test_exports_exclude_action_payloads_and_bind_the_actual_configuration() -> None:
    corpus = action_risk_corpus()
    report = evaluate_action_risk(corpus, mode="confirm-risky")
    alternate = evaluate_action_risk(corpus, mode="always-confirm")
    assert report.corpus_fingerprint == corpus.fingerprint
    assert report.analyzer_fingerprint != alternate.analyzer_fingerprint
    exported = report.model_dump_json()
    assert "example.invalid" not in exported
    assert "synthetic-project" not in exported
    assert "file_text" not in exported
    assert all("command" not in item for item in json.loads(exported)["measurements"])
    assert json.loads(exported)["model_calls"] == 0


def test_risk_evaluation_does_not_create_a_conversation_or_execute_a_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openhands.sdk import LocalConversation
    from openhands.tools.file_editor.impl import FileEditorExecutor
    from openhands.tools.terminal.impl import TerminalExecutor

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Analyzer evaluation attempted conversation or tool execution")

    monkeypatch.setattr(LocalConversation, "__init__", forbidden)
    monkeypatch.setattr(FileEditorExecutor, "__call__", forbidden)
    monkeypatch.setattr(TerminalExecutor, "__call__", forbidden)
    evaluate_action_risk(action_risk_corpus(), mode="confirm-risky")
