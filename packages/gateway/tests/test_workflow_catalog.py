# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from pathlib import Path

import pytest

import heartwood.gateway._research_evaluation as research_evaluation
from heartwood.core_adapter.research_workflows import research_workflow
from heartwood.gateway import ProjectContext, RestGateway, RestRequest, SessionGateway


def test_workflow_discovery_is_shared_read_only_and_reports_missing_checks(tmp_path: Path) -> None:
    gateway = SessionGateway(project=ProjectContext(tmp_path), env={})
    try:
        catalog = gateway.research_workflows()
        choices = {entry.definition.workflow_id: entry for entry in catalog.workflows}
        assert choices["baseline-analysis"].available
        assert choices["dataset-readiness"].available
        assert choices["result-verification"].available
        assert choices["result-verification"].unavailable_checks == ()
        assert choices["baseline-analysis"].definition == research_workflow("baseline-analysis")
        response = RestGateway(gateway).handle(RestRequest("GET", "/research/workflows"))
        assert response.status_code == 200
        assert response.body == catalog.model_dump(mode="json")
        assert not gateway._services
        assert list(tmp_path.iterdir()) == []
    finally:
        gateway.stop()


def test_new_unimplemented_check_cannot_be_advertised_as_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    definition = research_workflow("dataset-readiness")
    stage = definition.stages[0]
    check = stage.checks[0].model_copy(update={"evaluator_id": "research.unimplemented"})
    changed = definition.model_copy(
        update={
            "stages": (
                stage.model_copy(update={"checks": (check, *stage.checks[1:])}),
                *definition.stages[1:],
            )
        }
    )
    monkeypatch.setattr(research_evaluation, "research_workflows", lambda: (changed,))
    gateway = SessionGateway(project=ProjectContext(tmp_path), env={})
    try:
        choice = gateway.research_workflows().workflows[0]
        assert not choice.available
        assert choice.unavailable_checks == ("research.unimplemented",)
    finally:
        gateway.stop()
