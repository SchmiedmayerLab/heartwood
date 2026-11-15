# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Measure the production action-review rules without executing any proposed tool."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from time import perf_counter
from typing import Literal

from heartwood.core_adapter import pending_action_group
from heartwood.core_adapter.action_review import automatic_review
from heartwood.gateway._openhands_sdk import _analyzed_risk, _security_configuration, _tool_call
from heartwood.schemas import ActionReviewMode
from heartwood.schemas.action_risk import (
    ActionRiskCorpus,
    ActionRiskEvaluation,
    ActionRiskMeasurement,
    ActionRiskProbe,
)

# The adapter initializes upstream privacy-safe defaults before SDK imports.
# isort: split
from openhands.sdk.event import ActionEvent
from openhands.sdk.llm import MessageToolCall
from openhands.sdk.security import SecurityRisk
from openhands.sdk.tool import Action
from openhands.tools.file_editor import FileEditorAction
from openhands.tools.task import TaskAction
from openhands.tools.task_tracker import TaskTrackerAction
from openhands.tools.terminal import TerminalAction

_SYNTHETIC_PROJECT = Path("/synthetic-project")


def evaluate_action_risk(
    corpus: ActionRiskCorpus, *, mode: ActionReviewMode, contained: bool
) -> ActionRiskEvaluation:
    """Decide typed synthetic proposals with the classification and rules sessions use.

    No conversation, tool executor, model client, or subprocess is created. The
    corpus's model ratings are supplied inputs that must not change any decision.
    ``contained`` states that the evaluated session runs in an admitted sandbox worker.
    """
    analyzer, policy = _security_configuration()
    configuration = {"analyzer": analyzer.model_dump(mode="json"), "policy": policy.model_dump()}
    digest = hashlib.sha256(
        json.dumps(configuration, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    measurements: list[ActionRiskMeasurement] = []
    for probe in corpus.probes:
        event = _proposal(probe)
        started = perf_counter()
        tool_call = _tool_call(
            event,
            analyzed_risk=_analyzed_risk(analyzer, event),
            workspace=_SYNTHETIC_PROJECT,
        )
        group = pending_action_group((tool_call,))
        assert group is not None
        review = automatic_review(group, mode=mode, sandboxed=contained)
        measurements.append(
            ActionRiskMeasurement(
                case_id=probe.case_id,
                category=probe.category,
                must_confirm=probe.review_required(contained=contained),
                observed_risk=SecurityRisk(tool_call.risk.upper()).value,
                confirmation_required=review is None,
                automatic_rule=None if review is None else review.rule,
                elapsed_seconds=perf_counter() - started,
            )
        )
    return ActionRiskEvaluation(
        corpus_fingerprint=corpus.fingerprint,
        analyzer_fingerprint=digest,
        evaluated_at=datetime.now(UTC),
        openhands_version=version("openhands-sdk"),
        review_mode=mode,
        contained=contained,
        measurements=tuple(measurements),
    )


def _proposal(probe: ActionRiskProbe) -> ActionEvent:
    action: Action
    if probe.tool == "terminal":
        action = TerminalAction(command=probe.command)
    elif probe.tool == "task_tracker":
        action = TaskTrackerAction(command="plan" if probe.command == "plan" else "view")
    elif probe.tool == "task":
        action = TaskAction(prompt=probe.command, subagent_type="synthetic-reviewer")
    else:
        command: Literal["view", "create"] = "view" if probe.command == "view" else "create"
        assert probe.path is not None
        action = FileEditorAction(command=command, path=probe.path, file_text=probe.file_text)
    return ActionEvent(
        id=probe.case_id,
        thought=[],
        action=action,
        tool_name=probe.tool,
        tool_call_id=probe.case_id,
        tool_call=MessageToolCall(
            id=probe.case_id,
            name=probe.tool,
            arguments=action.model_dump_json(),
            origin="completion",
        ),
        llm_response_id="synthetic-risk-evaluation",
        security_risk=SecurityRisk(probe.model_risk),
    )
