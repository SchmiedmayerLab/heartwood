# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Measure the production analyzer policy without executing any proposed tool."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from importlib.metadata import version
from time import perf_counter
from typing import Literal

from heartwood.gateway._openhands_sdk import _security_configuration
from heartwood.schemas import ActionConfirmationMode
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
from openhands.tools.terminal import TerminalAction


def evaluate_action_risk(
    corpus: ActionRiskCorpus, *, mode: ActionConfirmationMode
) -> ActionRiskEvaluation:
    """Analyze typed synthetic proposals through the same factory as conversations.

    No conversation, tool executor, model client, or subprocess is created. The
    corpus's labels are supplied inputs, not live model risk predictions.
    """
    analyzer, policy = _security_configuration(mode)
    configuration = {"analyzer": analyzer.model_dump(mode="json"), "policy": policy.model_dump()}
    digest = hashlib.sha256(
        json.dumps(configuration, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    measurements: list[ActionRiskMeasurement] = []
    for probe in corpus.probes:
        event = _proposal(probe)
        started = perf_counter()
        risk = analyzer.security_risk(event)
        confirm = policy.should_confirm(risk)
        measurements.append(
            ActionRiskMeasurement(
                case_id=probe.case_id,
                category=probe.category,
                must_confirm=probe.must_confirm,
                observed_risk=risk.value,
                confirmation_required=confirm,
                elapsed_seconds=perf_counter() - started,
            )
        )
    return ActionRiskEvaluation(
        corpus_fingerprint=corpus.fingerprint,
        analyzer_fingerprint=digest,
        evaluated_at=datetime.now(UTC),
        openhands_version=version("openhands-sdk"),
        confirmation_mode=mode,
        measurements=tuple(measurements),
    )


def _proposal(probe: ActionRiskProbe) -> ActionEvent:
    action: Action
    if probe.tool == "terminal":
        action = TerminalAction(command=probe.command)
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
