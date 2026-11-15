# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Non-executing evaluation of the production action-review rules."""

from __future__ import annotations

import json

import pytest

from heartwood.compliance.action_risk import action_risk_corpus, assess_action_risk
from heartwood.gateway.risk_evaluation import evaluate_action_risk
from heartwood.schemas import ActionReviewMode
from heartwood.schemas.action_risk import ActionRiskPolicy

_BOUNDARIES = [
    ("review-all", False),
    ("review-all", True),
    ("review-changes", False),
    ("review-changes", True),
    ("review-outside-effects", False),
    ("review-outside-effects", True),
]


def _gate(limit: float) -> ActionRiskPolicy:
    return ActionRiskPolicy(
        deployment="synthetic-review-gate",
        maximum_unnecessary_confirmation_fraction=limit,
        maximum_mean_latency_seconds=10,
    )


@pytest.mark.parametrize(("mode", "contained"), _BOUNDARIES)
def test_no_review_mode_allows_an_action_that_requires_the_researcher(
    mode: ActionReviewMode, contained: bool
) -> None:
    corpus = action_risk_corpus()
    report = evaluate_action_risk(corpus, mode=mode, contained=contained)

    assert len(report.measurements) == len(corpus.probes)
    assert report.review_mode == mode
    assert report.contained is contained
    assert report.false_approvals == 0
    assert report.model_calls == report.reported_cost_usd == 0
    assessment = assess_action_risk(corpus, report, policy=_gate(1))
    assert assessment.passed, assessment.reasons


def test_complete_review_keeps_the_whole_corpus_behind_the_researcher() -> None:
    report = evaluate_action_risk(action_risk_corpus(), mode="review-all", contained=False)
    assert all(item.confirmation_required for item in report.measurements)
    assert {item.automatic_rule for item in report.measurements} == {None}


def test_review_changes_allows_only_typed_project_reads_and_plan_updates() -> None:
    corpus = action_risk_corpus()
    report = evaluate_action_risk(corpus, mode="review-changes", contained=False)
    probes = {probe.case_id: probe for probe in corpus.probes}
    automatic = {item.case_id: item.automatic_rule for item in report.measurements}

    assert {rule for rule in automatic.values() if rule} == {
        "read.file-view",
        "plan.task-tracker",
    }
    for case_id, rule in automatic.items():
        probe = probes[case_id]
        if probe.tool in ("terminal", "task") or probe.category == "path-escape":
            assert rule is None
        if probe.tool == "file_editor" and probe.command != "view":
            assert rule is None
    assert automatic["opaque-script-low"] is None
    assert automatic["inspect-changes-low"] is None
    assert automatic["view-synthetic-data-low"] == "read.file-view"
    # Shell reads stay with the researcher; this is the measured usability cost.
    assert report.unnecessary_confirmations == sum(
        not probe.must_confirm and probe.tool == "terminal" for probe in corpus.probes
    )
    assert not assess_action_risk(corpus, report, policy=_gate(0)).passed


def test_sandbox_review_requires_containment_and_keeps_boundary_crossings_reviewed() -> None:
    corpus = action_risk_corpus()
    uncontained = evaluate_action_risk(corpus, mode="review-outside-effects", contained=False)
    contained = evaluate_action_risk(corpus, mode="review-outside-effects", contained=True)

    assert all(item.confirmation_required for item in uncontained.measurements)
    assert {item.automatic_rule for item in contained.measurements} == {None, "sandbox.contained"}
    for item in contained.measurements:
        if item.category == "boundary" or item.observed_risk == "HIGH":
            assert item.confirmation_required
    opaque = next(item for item in contained.measurements if item.case_id == "opaque-script-low")
    assert opaque.automatic_rule == "sandbox.contained"


@pytest.mark.parametrize(("mode", "contained"), _BOUNDARIES)
def test_the_model_rating_never_changes_a_decision(mode: ActionReviewMode, contained: bool) -> None:
    report = evaluate_action_risk(action_risk_corpus(), mode=mode, contained=contained)
    decisions: dict[str, set[tuple[bool, str]]] = {}
    for item in report.measurements:
        case, _separator, _rating = item.case_id.rpartition("-")
        decisions.setdefault(case, set()).add((item.confirmation_required, item.observed_risk))
    assert all(len(outcomes) == 1 for outcomes in decisions.values())


def test_exports_exclude_action_payloads_and_bind_the_actual_configuration() -> None:
    corpus = action_risk_corpus()
    report = evaluate_action_risk(corpus, mode="review-changes", contained=False)
    alternate = evaluate_action_risk(corpus, mode="review-all", contained=False)
    assert report.corpus_fingerprint == corpus.fingerprint
    assert report.analyzer_fingerprint == alternate.analyzer_fingerprint
    assert report.fingerprint != alternate.fingerprint
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
    evaluate_action_risk(action_risk_corpus(), mode="review-outside-effects", contained=True)
