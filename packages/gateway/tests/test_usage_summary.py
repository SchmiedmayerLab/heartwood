# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""The content-free project usage summary against synthetic session and experiment records."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator

from heartwood.core_adapter import (
    BackendAgentMessageEvent,
    BackendEvent,
    BackendLifecycle,
    BackendLifecycleEvent,
    DeterministicAgentBackend,
    SessionService,
)
from heartwood.gateway import (
    ProjectContext,
    ProjectStateError,
    RestGateway,
    RestRequest,
    SessionGateway,
)
from heartwood.gateway._research_evaluation import ResearchStageEvaluator
from heartwood.gateway._usage_summary import _counts
from heartwood.gateway.experiments import ExperimentRecorder
from heartwood.schemas import JsonValue
from heartwood.schemas.usage import ProjectUsageSummary
from heartwood.session import CommandKind, EventKind, SessionCommand, SessionEvent

_MARKER = "synthetic-private-marker"


class _WorkflowBackend(DeterministicAgentBackend):
    def submit_turn(self, *, session_id: str, prompt: str) -> tuple[BackendEvent, ...]:  # noqa: ARG002
        return (
            BackendAgentMessageEvent(message=f"{_MARKER} model output", outcome_status="success"),
            BackendLifecycleEvent(lifecycle=BackendLifecycle.FINISHED),
        )


def test_summary_counts_match_synthetic_records_and_copy_no_content(tmp_path: Path) -> None:
    project = _synthetic_project(tmp_path)
    before = _record_snapshot(tmp_path)

    gateway = SessionGateway(project=project, env={})
    try:
        summary = gateway.usage_summary()
        repeated = gateway.usage_summary()
    finally:
        gateway.stop()

    # Two chat turns and one workflow stage turn.
    # The summary neither adds nor changes project, session, audit or experiment records.
    assert _record_snapshot(tmp_path) == before
    assert repeated == summary
    assert summary.audit_verification == "verified"
    assert summary.counts is not None
    counts = summary.counts.model_dump()
    assert counts == {
        "sessions": 3,
        "turns": 3,
        "action_sets": {"allowed": 1, "rejected": 1},
        "workflow_runs": {
            "ready": 0,
            "running": 0,
            "review": 0,
            "blocked": 0,
            "completed": 0,
            "cancelled": 1,
        },
        "reproductions": {"succeeded": 0, "failed": 0, "unsettled": 0},
        "experiment_runs": {
            "started": 0,
            "interrupted": 0,
            "resumed": 0,
            "succeeded": 1,
            "failed": 0,
            "cancelled": 1,
        },
    }
    reader = SessionGateway(project=project, env={})
    try:
        assert counts["sessions"] == len(reader.sessions()["sessions"])
        statuses = Counter(run.status for run in reader.experiment_records().runs)
        assert {
            status: count for status, count in counts["experiment_runs"].items() if count
        } == dict(statuses)
    finally:
        reader.stop()

    document = summary.model_dump(mode="json")
    Draft202012Validator(ProjectUsageSummary.model_json_schema(mode="serialization")).validate(
        document
    )
    rendered = json.dumps(document)
    assert _MARKER not in rendered
    assert set(_strings(document)) == {
        "heartwood.project-usage-summary.v1",
        summary.summary_id,
        summary.release_version,
        "generic",
        "verified",
    }
    assert summary.summary_id == project.usage_summary_id()


def test_summary_reports_failed_verification_without_partial_counts(tmp_path: Path) -> None:
    project = _synthetic_project(tmp_path)
    audit = project.sessions_dir / "alpha" / "audit.jsonl"
    lines = audit.read_text(encoding="utf-8").splitlines()
    tampered = json.loads(lines[0])
    tampered["payload"] = {"altered": True}
    audit.write_text("\n".join((json.dumps(tampered), *lines[1:])) + "\n", encoding="utf-8")

    gateway = SessionGateway(project=project, env={})
    try:
        summary = gateway.usage_summary()
    finally:
        gateway.stop()

    assert summary.audit_verification == "failed"
    assert summary.counts is None
    with pytest.raises(ValueError, match="audit history verifies"):
        ProjectUsageSummary.model_validate(
            {**summary.model_dump(), "counts": _empty_counts()}, strict=False
        )


def test_summary_requires_initialized_state_and_creates_none(tmp_path: Path) -> None:
    gateway = SessionGateway(project=ProjectContext(tmp_path), env={})
    try:
        with pytest.raises(ProjectStateError, match="not initialized"):
            gateway.usage_summary()
        response = RestGateway(gateway).handle(RestRequest("GET", "/project/usage-summary"))
        assert response.status_code == 422
    finally:
        gateway.stop()
    assert not (tmp_path / ".heartwood").exists()


def test_summary_is_served_over_rest_without_a_model_route(tmp_path: Path) -> None:
    project = _synthetic_project(tmp_path)
    # No model connection, credential or backend is configured for this gateway.
    gateway = SessionGateway(project=project, env={}, backend_id="auto")
    try:
        response = RestGateway(gateway).handle(RestRequest("GET", "/project/usage-summary"))
    finally:
        gateway.stop()
    assert response.status_code == 200
    summary = ProjectUsageSummary.model_validate_json(json.dumps(response.body))
    assert summary.counts is not None
    assert summary.counts.turns == 3


def test_reproduction_outcomes_count_settled_and_unsettled_preparations(tmp_path: Path) -> None:
    project = ProjectContext(tmp_path)
    project.initialize()
    statuses = ("prepared", "succeeded", "prepared", "failed", "prepared")
    events = tuple(
        SessionEvent(
            event_id=f"event-{index}",
            session_id="research",
            sequence=index,
            kind=EventKind.WORKFLOW_EXECUTION_RECORDED,
            occurred_at="2026-01-01T00:00:00Z",
            payload={"status": status},
        )
        for index, status in enumerate(statuses)
    )

    counts = _counts(project, (events,))

    assert counts.reproductions.model_dump() == {"succeeded": 1, "failed": 1, "unsettled": 1}


def _synthetic_project(root: Path) -> ProjectContext:
    project = ProjectContext(root)
    workflow_backend = _WorkflowBackend()
    gateway = SessionGateway(
        project=project,
        env={},
        service_factory=lambda sessions, session_id: SessionService.local_default(
            sessions,
            session_id=session_id,
            backend=(workflow_backend if session_id == "research" else DeterministicAgentBackend()),
            env={},
            workflow_evaluator=ResearchStageEvaluator(gateway.workspace_inspector),
        ),
    )
    try:
        for session_id, decision in (("alpha", CommandKind.APPROVE), ("beta", CommandKind.DENY)):
            gateway.handle(
                _command(session_id, CommandKind.CHAT, prompt=f"{_MARKER} prompt for {session_id}")
            )
            pending = gateway.session_projection(session_id=session_id).pending_approval
            assert pending is not None
            gateway.handle(
                _command(session_id, decision, target_type="action-set", target_id=pending.group_id)
            )
            gateway.rename_session(session_id, f"{_MARKER} title")
        (root / f"{_MARKER}-data.csv").write_text("person,x,y,split\na,1,3,train\n")
        (root / f"{_MARKER}-dictionary.json").write_text(
            json.dumps(
                {
                    "grouping_key": "person",
                    "primary_key": ["person"],
                    "outcome": "y",
                    "permitted_predictors": ["x"],
                    "split": {"column": "split", "train": "train", "test": "test"},
                }
            )
        )
        gateway.handle(
            _command(
                "research",
                CommandKind.WORKFLOW,
                action="start",
                workflow_id="dataset-readiness",
                inputs={
                    "data": f"{_MARKER}-data.csv",
                    "dictionary": f"{_MARKER}-dictionary.json",
                },
                output_directory="results",
            )
        )
        for action in ("run", "cancel"):
            run = gateway.persisted_session_projection(session_id="research").workflow
            assert run is not None
            gateway.handle(
                _command(
                    "research",
                    CommandKind.WORKFLOW,
                    action=action,
                    run_id=run.run_id,
                    revision=run.revision,
                )
            )
    finally:
        gateway.stop()
    (root / f"{_MARKER}_analysis.py").write_text("print('synthetic-private-marker output')\n")
    ExperimentRecorder(project).record_command(
        actor_ref="researcher", entry_point=f"{_MARKER}_analysis.py"
    )
    return project


def _command(session_id: str, kind: CommandKind, **payload: JsonValue) -> SessionCommand:
    return SessionCommand(
        command_id=uuid4().hex,
        session_id=session_id,
        kind=kind,
        actor_id="synthetic-user",
        created_at="2026-01-01T00:00:00Z",
        payload=payload,
    )


def _record_snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.name.endswith(".lock")
    }


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [item for child in value.values() for item in _strings(child)]
    if isinstance(value, list):
        return [item for child in value for item in _strings(child)]
    return []


def _empty_counts() -> dict[str, object]:
    zero_runs = dict.fromkeys(
        ("ready", "running", "review", "blocked", "completed", "cancelled"), 0
    )
    return cast(
        dict[str, object],
        {
            "sessions": 0,
            "turns": 0,
            "action_sets": {"allowed": 0, "rejected": 0},
            "workflow_runs": zero_runs,
            "reproductions": {"succeeded": 0, "failed": 0, "unsettled": 0},
            "experiment_runs": dict.fromkeys(
                ("started", "interrupted", "resumed", "succeeded", "failed", "cancelled"), 0
            ),
        },
    )
