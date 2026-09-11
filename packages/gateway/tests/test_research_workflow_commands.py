# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Workflow transitions share normal command receipts, tools, and project evidence."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from openhands.sdk.llm import Message, MessageToolCall
from openhands.sdk.testing import TestLLM

from heartwood.compliance.research import research_tasks
from heartwood.core_adapter import (
    BackendAgentMessageEvent,
    BackendEvent,
    BackendExecutionSettledEvent,
    BackendLifecycle,
    BackendLifecycleEvent,
    BackendSubagent,
    BackendSubagentEvent,
    BackendSubagentStatus,
    BackendUsage,
    BackendUsageEvent,
    DeterministicAgentBackend,
    SessionService,
)
from heartwood.core_adapter._state import SessionRecoveryError
from heartwood.gateway import ModelProfile, OpenHandsSdkBackend, ProjectContext, SessionGateway
from heartwood.gateway import _openhands_sdk as sdk_module
from heartwood.gateway._research_evaluation import ResearchStageEvaluator
from heartwood.schemas import JsonValue
from heartwood.schemas.review import ResearchReviewRun, ReviewProposals, ReviewSubmission
from heartwood.schemas.workflows import WorkflowOutcomeStatus, WorkflowRun
from heartwood.session import CommandKind, EventKind, SessionCommand


def test_stage_controls_keep_review_bound_to_displayed_evidence(tmp_path: Path) -> None:
    backend = FinishedBackend()
    gateway = _gateway(tmp_path, backend)
    try:
        _start(gateway, _inputs(tmp_path))
        initial = _projected_command(gateway, "run")
        gateway.handle(initial)
        gateway.handle(initial.model_copy(update={"command_id": "stale-start"}))
        assert len(backend.prompts) == 1
        _readiness(tmp_path)
        gateway.handle(_projected_command(gateway, "evaluate"))
        gateway.handle(_projected_command(gateway, "run"))
        (tmp_path / "results/readiness.md").write_text("# Synthetic findings\n")
        gateway.handle(_projected_command(gateway, "evaluate"))
        displayed = _projected_command(gateway, "accept")
        gateway.handle(_projected_command(gateway, "evaluate"))
        response = gateway.handle(displayed)
        assert any(event.kind == EventKind.ERROR_RECORDED for event in response.events)
        assert _state(gateway).phase == "review"
        gateway.handle(_projected_command(gateway, "decline"))
        assert _state(gateway).phase == "blocked"
        gateway.handle(_projected_command(gateway, "cancel"))
        assert gateway.session_projection(session_id="research").workflow_controls == ()
    finally:
        gateway.stop()


def _projected_command(gateway: SessionGateway, control_id: str) -> SessionCommand:
    control = next(
        item
        for item in gateway.session_projection(session_id="research").workflow_controls
        if item.control_id == control_id
    )
    return _command(**control.request.model_dump(mode="json"))


class FinishedBackend(DeterministicAgentBackend):
    def __init__(self) -> None:
        super().__init__()
        self.prompts: list[str] = []
        self.outcome: WorkflowOutcomeStatus | None = "success"
        self.idle = True
        self.fail_submission = False
        self.model_calls = 0

    def submit_turn(self, *, session_id: str, prompt: str) -> tuple[BackendEvent, ...]:  # noqa: ARG002
        self.prompts.append(prompt)
        if self.fail_submission:
            raise RuntimeError("Synthetic interruption after dispatch")
        return (
            BackendUsageEvent(
                usage=BackendUsage(
                    usage_id="total",
                    model_name="test",
                    call_count=self.model_calls,
                    prompt_tokens=0,
                    completion_tokens=0,
                )
            ),
            BackendAgentMessageEvent(message="Stage ended", outcome_status=self.outcome),
            BackendLifecycleEvent(lifecycle=BackendLifecycle.FINISHED),
        )

    def wait_for_idle(self, timeout: float) -> bool:  # noqa: ARG002
        return self.idle


def _inputs(root: Path) -> dict[str, str]:
    (root / "data.csv").write_text("person,x,y,split\na,1,3,train\nb,2,5,test\n")
    (root / "dictionary.json").write_text(
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
    return {"data": "data.csv", "dictionary": "dictionary.json"}


def _readiness(root: Path) -> None:
    (root / "results").mkdir(exist_ok=True)
    (root / "results/readiness.json").write_text(
        json.dumps(
            {
                "row_count": 2,
                "subject_count": 2,
                "duplicate_rows": 0,
                "missing_by_column": {},
                "invalid_by_column": {},
                "arm_counts": {},
                "leakage_columns": [],
                "ready_for_analysis": True,
            }
        )
    )


def _gateway(root: Path, backend: FinishedBackend) -> SessionGateway:
    gateway = SessionGateway(
        project=ProjectContext(root),
        env={},
        service_factory=lambda sessions, session_id: SessionService.local_default(
            sessions,
            session_id=session_id,
            backend=backend,
            env={},
            workflow_evaluator=ResearchStageEvaluator(gateway.workspace_inspector),
        ),
    )
    return gateway


def _command(**payload: object) -> SessionCommand:
    return SessionCommand(
        command_id=uuid4().hex,
        session_id="research",
        kind=CommandKind.WORKFLOW,
        created_at="2026-09-11T00:00:00Z",
        payload=cast(dict[str, JsonValue], payload),
    )


def _state(gateway: SessionGateway) -> WorkflowRun:
    state = gateway.persisted_session_projection(session_id="research").workflow
    assert state is not None
    return state


def _transition(gateway: SessionGateway, action: str, **extra: object) -> SessionCommand:
    state = _state(gateway)
    return _command(action=action, run_id=state.run_id, revision=state.revision, **extra)


def _start(gateway: SessionGateway, inputs: dict[str, str]) -> SessionCommand:
    command = _command(
        action="start",
        workflow_id="dataset-readiness",
        inputs=inputs,
        output_directory="results",
    )
    gateway.handle(command)
    assert _state(gateway).phase == "ready"
    return command


class ReviewBackend(FinishedBackend):
    def __init__(self, mode: str = "complete") -> None:
        super().__init__()
        self.mode = mode
        self.proposals = ReviewProposals(candidates=())
        self.pending_review_events: tuple[BackendEvent, ...] = ()
        self.defer_review = True

    def submit_turn(self, *, session_id: str, prompt: str) -> tuple[BackendEvent, ...]:
        events = super().submit_turn(session_id=session_id, prompt=prompt)
        if not prompt.startswith("Review the following bound analysis evidence"):
            return events
        specialist = BackendSubagent(
            invocation_id="review-call",
            task_id="native-review",
            agent_name="statistical-reviewer",
            role_label="Statistical Reviewer",
            status=BackendSubagentStatus.PROPOSED,
            parent_session_id=session_id,
            parent_action_id="review-action",
        )
        from dataclasses import replace

        completed = replace(
            specialist,
            status=BackendSubagentStatus.COMPLETED,
            parent_action_id="other-action" if self.mode == "unpaired" else "review-action",
            parent_session_id="other-session" if self.mode == "other-session" else session_id,
            review_proposals=None if self.mode == "missing" else self.proposals,
        )
        review_events = (
            events[0],
            BackendSubagentEvent(subagent=specialist),
            BackendSubagentEvent(subagent=completed),
            *events[1:],
        )
        review_events = tuple(
            event
            if isinstance(event, BackendExecutionSettledEvent)
            else replace(event, source_event_id=f"synthetic-review:{index}")
            for index, event in enumerate(review_events)
        )
        self.pending_review_events = review_events[2:]
        if not self.defer_review:
            return review_events
        self.idle = False
        return (*review_events[:2], BackendLifecycleEvent(lifecycle=BackendLifecycle.RUNNING))

    def finish_review(self) -> None:
        self.idle = True
        self._event_sink(self.pending_review_events)


def test_synchronous_review_settlement_is_part_of_the_original_receipt(tmp_path: Path) -> None:
    backend = ReviewBackend()
    backend.defer_review = False
    gateway = _gateway(tmp_path, backend)
    try:
        _start(gateway, _inputs(tmp_path))
        gateway.handle(_transition(gateway, "run"))
        _readiness(tmp_path)
        command = _projected_command(gateway, "request-review")
        result = gateway.handle(command)
        (assessed,) = [
            event for event in result.events if event.payload.get("transition") == "assess-review"
        ]
        assert assessed.payload["command_id"] == command.command_id
        assert assessed.payload["actor_id"] == "gateway"
        assert gateway.handle(command).events == result.events
        assert len(backend.prompts) == 2
    finally:
        gateway.stop()


def test_review_waits_for_execution_boundary_despite_terminal_progress(tmp_path: Path) -> None:
    backend = ReviewBackend()
    gateway = _gateway(tmp_path, backend)
    try:
        _start(gateway, _inputs(tmp_path))
        gateway.handle(_transition(gateway, "run"))
        _readiness(tmp_path)
        gateway.handle(_projected_command(gateway, "request-review"))
        backend._event_sink(backend.pending_review_events)
        pending = _state(gateway).research_review
        assert pending is not None
        assert pending.status == "pending"
        before = len(gateway._services["research"].replay_events())
        service = gateway._services["research"]
        assert (
            service._translate_backend_events((BackendExecutionSettledEvent(),), live=False) == []
        )
        assert _state(gateway).research_review == pending
        # A final callback runs on its own worker and therefore cannot join itself.
        assert not backend.idle
        backend._event_sink((BackendExecutionSettledEvent(),))
        assessed = _state(gateway).research_review
        assert assessed is not None
        assert assessed.status == "assessed"
        events = gateway._services["research"].replay_events()
        assert len(events) == before + 1
        assert events[-1].payload["transition"] == "assess-review"
        backend._event_sink((BackendExecutionSettledEvent(),))
        assert gateway._services["research"].replay_events() == events
        assert len(backend.prompts) == 2
    finally:
        gateway.stop()


@pytest.mark.parametrize("finish", ["error", "missing-outcome", "cancelled", "pause-resume"])
def test_automatic_review_respects_terminal_and_interruption_states(
    tmp_path: Path, finish: str
) -> None:
    backend = ReviewBackend()
    gateway = _gateway(tmp_path, backend)
    try:
        _start(gateway, _inputs(tmp_path))
        gateway.handle(_transition(gateway, "run"))
        _readiness(tmp_path)
        gateway.handle(_projected_command(gateway, "request-review"))
        if finish == "cancelled":
            backend.idle = True
            gateway.handle(_transition(gateway, "cancel"))
        elif finish == "error":
            backend.pending_review_events = (
                BackendLifecycleEvent(lifecycle=BackendLifecycle.ERROR),
            )
        elif finish == "missing-outcome":
            backend.pending_review_events = tuple(
                event
                for event in backend.pending_review_events
                if not isinstance(event, BackendAgentMessageEvent)
            )
        else:
            for state in (BackendLifecycle.PAUSED, BackendLifecycle.RUNNING):
                backend._event_sink((BackendLifecycleEvent(lifecycle=state),))
                review = _state(gateway).research_review
                assert review is not None
                assert review.status == "pending"
        backend.finish_review()
        review = _state(gateway).research_review
        assert review is not None
        if finish == "cancelled":
            assert review.status == "cancelled"
            assert review.assessment is None
        elif finish == "pause-resume":
            assert review.status == "assessed"
        else:
            assert review.status == "unavailable"
            assert review.unavailable_reason == "no-structured-outcome"
        assert len(backend.prompts) == 2
    finally:
        gateway.stop()


@pytest.mark.parametrize(
    "boundary", ["intent", "audit-before", "audit-after", "events-before", "events-after"]
)
def test_automatic_review_recovers_each_append_boundary_without_model_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    from heartwood.core_adapter import _state as state_module

    backend = ReviewBackend()
    gateway = _gateway(tmp_path, backend)
    try:
        _start(gateway, _inputs(tmp_path))
        gateway.handle(_transition(gateway, "run"))
        _readiness(tmp_path)
        command = _projected_command(gateway, "request-review")
        gateway.handle(command)
        store = gateway._services["research"].store
        append = state_module._append_private_json_line
        write = state_module._write_private_json_atomic

        def interrupt_append(path: Path, text: str) -> None:
            payload = json.loads(text)
            target = store.audit_path if boundary.startswith("audit") else store.events_path
            if path != target or payload.get("payload", {}).get("transition") != "assess-review":
                append(path, text)
                return
            if boundary.endswith("after"):
                append(path, text)
            raise OSError("Synthetic review append interruption")

        def interrupt_intent(path: Path, value: dict[str, object]) -> None:
            event = value.get("session_event")
            if (
                isinstance(event, dict)
                and event.get("payload", {}).get("transition") == "assess-review"
            ):
                raise OSError("Synthetic review append interruption")
            write(path, value)

        with monkeypatch.context() as patches:
            if boundary == "intent":
                patches.setattr(state_module, "_write_private_json_atomic", interrupt_intent)
            else:
                patches.setattr(state_module, "_append_private_json_line", interrupt_append)
            with pytest.raises(OSError, match="Synthetic review append interruption"):
                backend.finish_review()
    finally:
        gateway.stop()
    replacement = ReviewBackend()
    restored = _gateway(tmp_path, replacement)
    try:
        assert restored.handle(command).replayed
        view = restored.session_projection(session_id="research")
        assert view.workflow is not None
        assert view.workflow.research_review is not None
        assert view.workflow.research_review.status == "assessed"
        service = restored._services["research"]
        service.reconcile()
        service.reconcile()
        events = service.replay_events()
        assert sum(event.payload.get("transition") == "assess-review" for event in events) == 1
        assert not replacement.prompts
    finally:
        restored.stop()


@pytest.mark.parametrize("mode", ["complete", "unpaired", "other-session", "missing"])
def test_research_review_binds_native_tasks_and_replays_without_dispatch(
    tmp_path: Path, mode: str
) -> None:
    backend = ReviewBackend(mode)
    gateway = _gateway(tmp_path, backend)
    try:
        _start(gateway, _inputs(tmp_path))
        gateway.handle(_transition(gateway, "run"))
        _readiness(tmp_path)
        command = _projected_command(gateway, "request-review")
        gateway.handle(command)
        pending = _state(gateway)
        assert pending.research_review is not None
        assert pending.research_review.status == "pending"
        assert {item.artifact_id for item in pending.research_review.snapshot.artifacts} == {
            "data",
            "dictionary",
            "readiness",
        }
        assert gateway.handle(command).replayed
        assert len(backend.prompts) == 2
        events = gateway._services["research"].replay_events()
        preparation = next(
            event for event in events if event.sequence == pending.research_review.started_sequence
        )
        assert preparation.kind == EventKind.WORKFLOW_UPDATED
        recorded = WorkflowRun.model_validate(preparation.payload["run"])
        assert recorded.research_review is not None
        assert recorded.research_review.status == "pending"
        assert any(
            event.sequence > preparation.sequence and event.kind == EventKind.SUBAGENT_UPDATED
            for event in events
        )
        denied = gateway.handle(_transition(gateway, "evaluate"))
        assert any(event.kind == EventKind.ERROR_RECORDED for event in denied.events)
        backend.finish_review()
        settled = _state(gateway)
        backend.finish_review()
        assert _state(gateway) == settled
    finally:
        gateway.stop()
    fresh_backend = ReviewBackend()
    restored = _gateway(tmp_path, fresh_backend)
    try:
        assert restored.handle(command).replayed
        assessed = _state(restored)
        assert assessed.research_review is not None
        assert assessed.research_review.status == (
            "assessed" if mode == "complete" else "unavailable"
        )
        assert assessed.research_review.assessment is not None
        assert assessed.research_review.assessment.findings == ()
        assert not fresh_backend.prompts
        from heartwood.cli._interactive import format_workflow_lines
        from heartwood.notebook import build_view_model, build_widget_spec

        projection = restored.session_projection(session_id="research")
        label = f"Research review: {assessed.research_review.status}"
        assert label in format_workflow_lines(projection)
        section = next(
            item
            for item in build_widget_spec(build_view_model(projection))
            if item.title == "Research Workflow"
        )
        assert label in section.items
        if mode != "complete":
            limitation = "Review limitation: incomplete review"
            assert limitation in format_workflow_lines(projection)
            assert limitation in section.items
        restored.handle(_projected_command(restored, "evaluate"))
        assert _state(restored).stage_id == "report"
        restored.handle(
            SessionCommand(
                command_id="review-export",
                session_id="research",
                kind=CommandKind.AUDIT_EXPORT,
                created_at="2026-09-11T00:00:00Z",
            )
        )
        audit = (restored.sessions_root / "research/audit-export.jsonl").read_text()
        assert "research_review_fingerprint" in audit
        assert "readiness.json" not in audit
        assert "candidates" not in audit
    finally:
        restored.stop()


def test_research_review_missing_evidence_does_not_start_model_work(tmp_path: Path) -> None:
    backend = ReviewBackend()
    gateway = _gateway(tmp_path, backend)
    try:
        _start(gateway, _inputs(tmp_path))
        gateway.handle(_transition(gateway, "run"))
        result = gateway.handle(_projected_command(gateway, "request-review"))
        assert any(event.kind == EventKind.ERROR_RECORDED for event in result.events)
        assert len(backend.prompts) == 1
        assert _state(gateway).research_review is None
    finally:
        gateway.stop()


def test_interrupted_research_review_is_not_redispatched(tmp_path: Path) -> None:
    backend = ReviewBackend()
    gateway = _gateway(tmp_path, backend)
    try:
        _start(gateway, _inputs(tmp_path))
        gateway.handle(_transition(gateway, "run"))
        _readiness(tmp_path)
        command = _projected_command(gateway, "request-review")
        backend.fail_submission = True
        with pytest.raises(RuntimeError, match="Synthetic interruption"):
            gateway.handle(command)
        review = _state(gateway).research_review
        assert review is not None
        assert review.status == "pending"
    finally:
        gateway.stop()
    replacement = ReviewBackend()
    restored = _gateway(tmp_path, replacement)
    try:
        with pytest.raises(SessionRecoveryError):
            restored.handle(command)
        assert not replacement.prompts
        assert _state(restored).research_review == review
    finally:
        restored.stop()


def test_research_review_can_be_assessed_after_budget_expiry(tmp_path: Path) -> None:
    from datetime import timedelta

    backend = ReviewBackend()
    gateway = _gateway(tmp_path, backend)
    try:
        _start(gateway, _inputs(tmp_path))
        gateway.handle(_transition(gateway, "run"))
        _readiness(tmp_path)
        gateway.handle(_projected_command(gateway, "request-review"))
        before = _state(gateway)
        gateway._services["research"].clock = lambda: (
            before.created_at + timedelta(hours=2)
        ).isoformat()
        backend.finish_review()
        review = _state(gateway).research_review
        assert review is not None
        assert review.status == "assessed"
        assert len(backend.prompts) == 2
        denied = gateway.handle(_transition(gateway, "evaluate"))
        assert any("budget reached" in str(event.payload.get("reason")) for event in denied.events)
    finally:
        gateway.stop()


@pytest.mark.parametrize("change", [None, "edited", "removed", "steered"])
def test_bound_review_independently_verifies_a_seeded_code_defect(
    tmp_path: Path, change: str | None
) -> None:
    task = next(item for item in research_tasks() if item.case.case_id == "baseline-analysis")
    for name, content in task.inputs.items():
        (tmp_path / name).write_text(content)
    backend = ReviewBackend()
    backend.proposals = ReviewProposals.model_validate(
        {
            "candidates": [
                {
                    "candidate_id": "syntax",
                    "condition": "python-source-invalid",
                    "category": "coding",
                    "severity": "critical",
                    "summary": "Untrusted proposed diagnosis",
                    "artifact_ids": ["program"],
                }
            ]
        }
    )
    gateway = _gateway(tmp_path, backend)
    try:
        gateway.handle(
            _command(
                action="start",
                workflow_id="baseline-analysis",
                output_directory="results",
                inputs={
                    "data": "data.csv",
                    "dictionary": "dictionary.json",
                    "question": "Predict response",
                },
            )
        )
        gateway.handle(_projected_command(gateway, "run"))
        (tmp_path / "results").mkdir()
        (tmp_path / "results/plan.json").write_text(
            json.dumps(
                {
                    "question": "Predict response",
                    "estimand": "Held-out prediction error",
                    "outcome": "response",
                    "features": ["measurement"],
                    "group_column": "subject_id",
                    "split_column": "partition",
                    "assumptions": ["Prespecified split"],
                    "limitations": ["Synthetic data"],
                }
            )
        )
        gateway.handle(_projected_command(gateway, "evaluate"))
        gateway.handle(_projected_command(gateway, "accept"))
        assert _state(gateway).stage_id == "execute"
        gateway.handle(_projected_command(gateway, "run"))
        source = tmp_path / "results/analysis.py"
        source.write_text("def broken(\n")
        (tmp_path / "results/metrics.json").write_text("{}")
        (tmp_path / "results/predictions.csv").write_text("prediction\n")
        gateway.handle(_projected_command(gateway, "request-review"))
        if change == "edited":
            source.write_text("print('changed')\n")
        elif change == "removed":
            source.unlink()
        elif change == "steered":
            gateway.handle(
                SessionCommand(
                    command_id="steer-review",
                    session_id="research",
                    kind=CommandKind.CHAT,
                    created_at="2026-09-11T00:00:00Z",
                    payload={"prompt": "Review something else"},
                )
            )
        backend.finish_review()
        review = _state(gateway).research_review
        assert review is not None
        if change == "steered":
            assert review.status == "unavailable"
            assert review.unavailable_reason == "invalid-review"
            return
        assert review.assessment is not None
        (finding,) = review.assessment.findings
        assert (
            finding.verification
            == {None: "verified", "edited": "stale", "removed": "unavailable"}[change]
        )
        assert finding.verified_claim == (
            "The Python source is empty or syntactically invalid." if change is None else None
        )
        assert finding.severity == "high"
        assert len(backend.prompts) == 3
        gateway.handle(_projected_command(gateway, "evaluate"))
        assert _state(gateway).stage_id == "execute"
        assert _state(gateway).phase == "blocked"
    finally:
        gateway.stop()


def test_run_review_and_restart_share_one_authoritative_sequence(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    backend = FinishedBackend()
    gateway = _gateway(tmp_path, backend)
    try:
        start = _start(gateway, inputs)
        assert not backend.prompts
        run = _transition(gateway, "run")
        gateway.handle(run)
        assert _state(gateway).phase == "running"
        assert len(backend.prompts) == 1
        assert "results/readiness.json" in backend.prompts[0]
        _readiness(tmp_path)
        gateway.handle(_transition(gateway, "evaluate"))
        assert _state(gateway).phase == "ready"
        assert _state(gateway).stage_id == "report"
        gateway.handle(_transition(gateway, "run"))
        (tmp_path / "results/readiness.md").write_text("# Readiness\nSynthetic data only.\n")
        gateway.handle(_transition(gateway, "evaluate"))
        state = _state(gateway)
        assert state.phase == "review"
        assert state.evaluation is not None
        review = _transition(
            gateway,
            "review",
            approved=True,
            evidence_fingerprint=state.evaluation.assessment.evidence_fingerprint,
        )
        before = gateway.persisted_session_projection(session_id="research")
    finally:
        gateway.stop()
    fresh_backend = FinishedBackend()
    reopened = _gateway(tmp_path, fresh_backend)
    try:
        for command in (start, run):
            assert reopened.handle(command).replayed
        restored = reopened.persisted_session_projection(session_id="research")
        assert restored.model_dump(
            exclude={"stream_epoch", "stream_revision"}
        ) == before.model_dump(exclude={"stream_epoch", "stream_revision"})
        assert fresh_backend.prompts == []
        reopened.handle(review)
        assert reopened.handle(review).replayed
        assert _state(reopened).phase == "completed"
        assert fresh_backend.prompts == []
        reopened.handle(
            SessionCommand(
                command_id="export",
                session_id="research",
                kind=CommandKind.AUDIT_EXPORT,
                created_at="2026-09-11T00:00:00Z",
            )
        )
        audit = (reopened.sessions_root / "research/audit-export.jsonl").read_text()
        assert "workflow.updated" in audit
        assert "data.csv" not in audit
        assert "Synthetic data only" not in audit
        decisions = [
            entry["payload"]
            for line in audit.splitlines()
            if (entry := json.loads(line)).get("payload", {}).get("transition") == "review"
        ]
        assert len(decisions) == 1
        assert decisions[0]["approved"] is True
        assert decisions[0]["assessed_stage_id"] == "report"
        assert decisions[0]["evidence_fingerprint"] == review.payload["evidence_fingerprint"]
    finally:
        reopened.stop()


@pytest.mark.parametrize("outcome", [None, "failed", "blocked", "partial_success", "unknown"])
def test_valid_artifacts_do_not_override_model_outcome(
    tmp_path: Path, outcome: WorkflowOutcomeStatus | None
) -> None:
    backend = FinishedBackend()
    backend.outcome = outcome
    gateway = _gateway(tmp_path, backend)
    try:
        _start(gateway, _inputs(tmp_path))
        gateway.handle(_transition(gateway, "run"))
        _readiness(tmp_path)
        gateway.handle(_transition(gateway, "evaluate"))
        assert _state(gateway).phase in {"running", "blocked"}
        assert not _state(gateway).completed
    finally:
        gateway.stop()


@pytest.mark.parametrize(
    "damage", ["missing", "incorrect", "input-changed", "busy", "stale", "repeat"]
)
def test_stage_gates_fail_closed(tmp_path: Path, damage: str) -> None:
    backend = FinishedBackend()
    gateway = _gateway(tmp_path, backend)
    try:
        _start(gateway, _inputs(tmp_path))
        run = _transition(gateway, "run")
        gateway.handle(run)
        if damage != "missing":
            _readiness(tmp_path)
        if damage == "incorrect":
            (tmp_path / "results/readiness.json").write_text("{}")
        if damage == "input-changed":
            (tmp_path / "data.csv").write_text("changed")
        if damage == "busy":
            backend.idle = False
        command = _transition(gateway, "evaluate")
        if damage == "stale":
            command = command.model_copy(update={"payload": {**command.payload, "revision": 0}})
        if damage == "repeat":
            command = _transition(gateway, "run")
        gateway.handle(command)
        assert _state(gateway).phase in {"running", "blocked"}
        assert not _state(gateway).completed
        assert len(backend.prompts) == 1
    finally:
        gateway.stop()


@pytest.mark.parametrize("changed", ["artifact", "fingerprint", "rejected"])
def test_review_binds_exact_current_evidence(tmp_path: Path, changed: str) -> None:
    gateway = _gateway(tmp_path, FinishedBackend())
    try:
        _start(gateway, _inputs(tmp_path))
        gateway.handle(_transition(gateway, "run"))
        _readiness(tmp_path)
        gateway.handle(_transition(gateway, "evaluate"))
        gateway.handle(_transition(gateway, "run"))
        (tmp_path / "results/readiness.md").write_text("# Readiness\nSynthetic data only.\n")
        gateway.handle(_transition(gateway, "evaluate"))
        state = _state(gateway)
        assert state.evaluation is not None
        fingerprint = state.evaluation.assessment.evidence_fingerprint
        if changed == "artifact":
            path = tmp_path / "results/readiness.md"
            path.write_text(path.read_text() + "\n")
        if changed == "fingerprint":
            fingerprint = "0" * 64
        gateway.handle(
            _transition(
                gateway,
                "review",
                approved=changed != "rejected",
                evidence_fingerprint=fingerprint,
            )
        )
        assert len(_state(gateway).completed) == 1
        assert _state(gateway).phase in {"review", "blocked"}
    finally:
        gateway.stop()


def test_interrupted_dispatch_is_never_automatically_repeated(tmp_path: Path) -> None:
    backend = FinishedBackend()
    gateway = _gateway(tmp_path, backend)
    try:
        _start(gateway, _inputs(tmp_path))
        backend.fail_submission = True
        command = _transition(gateway, "run")
        with pytest.raises(RuntimeError, match="Synthetic interruption"):
            gateway.handle(command)
        assert _state(gateway).phase == "running"
        recorded = gateway.session_projection(session_id="research").experiments
        assert len(recorded) == 1
        assert recorded[0].status == "started"
        assert recorded[0].outputs == ()
    finally:
        gateway.stop()
    replacement = FinishedBackend()
    fresh = _gateway(tmp_path, replacement)
    try:
        with pytest.raises(SessionRecoveryError):
            fresh.handle(command)
        assert not replacement.prompts
        assert fresh.session_projection(session_id="research").experiments == recorded
    finally:
        fresh.stop()


def _tool_message(name: str, **arguments: object) -> Message:
    return Message(
        role="assistant",
        content=[],
        tool_calls=[
            MessageToolCall(
                id=uuid4().hex,
                name=name,
                arguments=json.dumps(arguments),
                origin="completion",
            )
        ],
    )


def _sdk_gateway(
    root: Path,
    llm: TestLLM,
    monkeypatch: pytest.MonkeyPatch,
    *,
    specialists: bool = False,
) -> SessionGateway:
    from heartwood.gateway._specialists import load_specialist_catalog

    monkeypatch.setattr(sdk_module, "LLM", lambda **_options: llm)
    repository = Path(__file__).resolve().parents[3]
    catalog = (
        load_specialist_catalog(
            repository / "agents/verified", repository / "vendor/heartwood-skills/skills"
        )
        if specialists
        else None
    )

    def service_factory(sessions: Path, session_id: str) -> SessionService:
        backend = OpenHandsSdkBackend(
            profile=ModelProfile(
                profile_id="heartwood",
                model="openai/local-model",
                base_url="http://127.0.0.1:8765/v1",
                policy_endpoint="http://127.0.0.1:8765/v1/chat/completions",
                credential_kind="none",
            ),
            workspace=root,
            skills_dir=root / ".heartwood/skills",
            persistence_dir=sessions / session_id / "openhands",
            conversation_key=f"{root}#{session_id}",
            structured_task_outcomes=True,
            specialist_catalog=catalog,
            env={},
        )
        return SessionService.local_default(
            sessions,
            session_id=session_id,
            backend=backend,
            env={},
            workflow_evaluator=ResearchStageEvaluator(gateway.workspace_inspector),
        )

    gateway = SessionGateway(project=ProjectContext(root), service_factory=service_factory, env={})
    return gateway


def test_real_sdk_review_settles_after_approval_and_reopens_without_model_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = _inputs(tmp_path)
    llm = TestLLM.from_messages(
        [
            _tool_message(
                "finish", message="Inspection settled.", status="success", outcome_summary="Done."
            ),
            _tool_message(
                "task",
                description="Review synthetic readiness",
                prompt="Review the supplied synthetic readiness evidence. Return no findings.",
                subagent_type="statistical-reviewer",
            ),
            _tool_message("finish", message="No candidate findings.", candidates=[]),
            _tool_message(
                "finish", message="Review settled.", status="success", outcome_summary="Done."
            ),
        ]
    )
    gateway = _sdk_gateway(tmp_path, llm, monkeypatch, specialists=True)
    try:
        _start(gateway, inputs)
        gateway.handle(_projected_command(gateway, "run"))
        assert gateway.wait_for_session_idle(session_id="research", timeout=30)
        _readiness(tmp_path)
        request = _projected_command(gateway, "request-review")
        gateway.handle(request)
        assert gateway.wait_for_session_idle(session_id="research", timeout=30)
        projection = gateway.session_projection(session_id="research")
        assert projection.pending_approval is not None
        pending_review = _state(gateway).research_review
        assert pending_review is not None
        assert pending_review.status == "pending"
        approval = SessionCommand(
            command_id=uuid4().hex,
            session_id="research",
            kind=CommandKind.APPROVE,
            created_at="2026-09-11T00:00:00Z",
            payload={"target_id": projection.pending_approval.group_id},
        )
        gateway.handle(approval)
        assert gateway.wait_for_session_idle(session_id="research", timeout=30)
        review = _state(gateway).research_review
        assert review is not None
        assert review.status == "assessed"
        assert len(review.submissions) == 1
        assert review.submissions[0].reviewer_id == "statistical-reviewer"
        assert review.assessment is not None
        assert review.assessment.findings == ()
        calls = llm.call_count
        assert gateway.handle(request).replayed
        assert gateway.handle(approval).replayed
        assert llm.call_count == calls
    finally:
        gateway.stop()
    unused = TestLLM.from_messages([])
    restored = _sdk_gateway(tmp_path, unused, monkeypatch, specialists=True)
    try:
        assert restored.handle(request).replayed
        assert restored.handle(approval).replayed
        assert _state(restored).research_review == review
        assert unused.call_count == 0
    finally:
        restored.stop()


def test_real_sdk_runs_reviewed_stages_and_restores_structured_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _inputs(tmp_path)
    _readiness(tmp_path)
    readiness = (tmp_path / "results/readiness.json").read_text()
    (tmp_path / "results/readiness.json").unlink()
    (tmp_path / "results").rmdir()
    report = "# Readiness\nSynthetic data only; not a scientific validity claim.\n"
    mkdir = _tool_message("terminal", command="mkdir results")
    create = _tool_message(
        "file_editor",
        command="create",
        path=str(tmp_path / "results/readiness.json"),
        file_text=readiness,
    )
    assert mkdir.tool_calls is not None
    assert create.tool_calls is not None
    llm = TestLLM.from_messages(
        [
            Message(
                role="assistant", content=[], tool_calls=[*mkdir.tool_calls, *create.tool_calls]
            ),
            _tool_message(
                "finish",
                message="Inspection complete.",
                status="success",
                outcome_summary="Checks remain independent.",
            ),
            _tool_message(
                "file_editor",
                command="create",
                path=str(tmp_path / "results/readiness.md"),
                file_text=report,
            ),
            _tool_message(
                "finish",
                message="",
                status="success",
                outcome_summary="Ready for review.",
            ),
        ]
    )
    gateway = _sdk_gateway(tmp_path, llm, monkeypatch)
    try:
        _start(gateway, inputs)
        for path in ("readiness.json", "readiness.md"):
            command = _transition(gateway, "run")
            gateway.handle(command)
            assert gateway.wait_for_session_idle(session_id="research", timeout=30)
            projection = gateway.session_projection(session_id="research")
            assert projection.pending_approval is not None
            assert not (tmp_path / "results" / path).exists()
            if path == "readiness.json":
                assert not (tmp_path / "results").exists()
                assert len(projection.pending_approval.actions) == 2
            calls = llm.call_count
            assert gateway.handle(command).replayed
            assert llm.call_count == calls
            gateway.handle(
                SessionCommand(
                    command_id=uuid4().hex,
                    session_id="research",
                    kind=CommandKind.APPROVE,
                    created_at="2026-09-11T00:00:00Z",
                    payload={"target_id": projection.pending_approval.group_id},
                )
            )
            assert gateway.wait_for_session_idle(session_id="research", timeout=30)
            assert (tmp_path / "results" / path).is_file()
            gateway.handle(_transition(gateway, "evaluate"))
        assert _state(gateway).phase == "review"
        assert llm.call_count == 4
        before = _state(gateway)
    finally:
        gateway.stop()
    unused = TestLLM.from_messages([])
    restored = _sdk_gateway(tmp_path, unused, monkeypatch)
    try:
        assert before.evaluation is not None
        restored.handle(
            _transition(
                restored,
                "review",
                approved=True,
                evidence_fingerprint=before.evaluation.assessment.evidence_fingerprint,
            )
        )
        assert _state(restored).phase == "completed"
        assert unused.call_count == 0
        experiments = restored.experiment_records().runs
        assert len(experiments) == 2
        assert [
            item.definition.stage.stage_id for item in experiments if item.definition.stage
        ] == ["inspect", "report"]
        assert all(item.status == "succeeded" for item in experiments)
        assert all(item.exit_code is None for item in experiments)
        assert any(item.kind == "tool.execution.recorded" for item in experiments[0].evidence)
        assert any(item.kind == "approval.recorded" for item in experiments[0].evidence)
        assert experiments == restored.session_projection(session_id="research").experiments
        export = restored.export_experiments()
        assert restored.export_experiments() == export
        assert "subject_id" not in export.jsonl
        assert '"content"' not in export.jsonl
        assert '"command"' not in export.jsonl
        assert unused.call_count == 0
        assert (tmp_path / "results/readiness.md").read_text() == report
    finally:
        restored.stop()


def test_real_sdk_correction_keeps_reviewed_source_and_requires_action_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "analysis.py"
    source.write_text("def broken(\n")
    corrected_path = tmp_path / "correction-one/program-analysis.py"
    mkdir = _tool_message("terminal", command="mkdir correction-one")
    create = _tool_message(
        "file_editor", command="create", path=str(corrected_path), file_text="print('synthetic')\n"
    )
    assert mkdir.tool_calls is not None
    assert create.tool_calls is not None
    llm = TestLLM.from_messages(
        [
            Message(
                role="assistant", content=[], tool_calls=[*mkdir.tool_calls, *create.tool_calls]
            ),
            _tool_message(
                "finish",
                message="Correction proposed.",
                status="success",
                outcome_summary="Independent verification required.",
            ),
        ]
    )
    gateway = _sdk_gateway(tmp_path, llm, monkeypatch)
    try:
        snapshot = gateway.prepare_research_review({"program": "analysis.py"})
        proposals = ReviewProposals.model_validate(
            {
                "candidates": [
                    {
                        "candidate_id": "syntax",
                        "condition": "python-source-invalid",
                        "category": "coding",
                        "severity": "high",
                        "summary": "The source is incomplete.",
                        "artifact_ids": ["program"],
                    }
                ]
            }
        )
        submission = ReviewSubmission.associate(
            proposals, review_id="native-review", reviewer_id="coding-reviewer", snapshot=snapshot
        )
        review = ResearchReviewRun(
            review_id="review-one",
            snapshot=snapshot,
            reviewer_ids=("coding-reviewer",),
            started_sequence=0,
            status="assessed",
            submissions=(submission,),
            assessment=gateway.assess_research_review(snapshot, (submission,)),
        )
        plan = gateway.prepare_research_correction(review, output_directory="correction-one")
        request = SessionCommand(
            command_id="correct-source",
            session_id="research",
            kind=CommandKind.CHAT,
            created_at="2026-09-11T00:00:00Z",
            payload={
                "prompt": "Create the declared synthetic correction; preserve the original. "
                + plan.model_dump_json()
            },
        )
        gateway.handle(request)
        assert gateway.wait_for_session_idle(session_id="research", timeout=30)
        pending = gateway.session_projection(session_id="research").pending_approval
        assert pending is not None
        assert len(pending.actions) == 2
        assert not corrected_path.exists()
        assert source.read_text() == "def broken(\n"
        approval = SessionCommand(
            command_id="approve-correction",
            session_id="research",
            kind=CommandKind.APPROVE,
            created_at="2026-09-11T00:00:00Z",
            payload={"target_id": pending.group_id},
        )
        gateway.handle(approval)
        assert gateway.wait_for_session_idle(session_id="research", timeout=30)
        result = gateway.assess_research_correction(review, plan)
        assert result.checks[0].status == "not_observed"
        assert source.read_text() == "def broken(\n"
        calls = llm.call_count
        assert gateway.handle(approval).replayed
        assert gateway.handle(request).replayed
        assert llm.call_count == calls
    finally:
        gateway.stop()
    unused = TestLLM.from_messages([])
    restored = _sdk_gateway(tmp_path, unused, monkeypatch)
    try:
        assert restored.handle(approval).replayed
        assert restored.handle(request).replayed
        assert restored.assess_research_correction(review, plan) == result
        restored.handle(
            SessionCommand(
                command_id="export-correction",
                session_id="research",
                kind=CommandKind.AUDIT_EXPORT,
                created_at="2026-09-11T00:00:00Z",
            )
        )
        audit = (restored.sessions_root / "research/audit-export.jsonl").read_text()
        assert "approval.recorded" in audit
        assert "analysis.py" not in audit
        assert "synthetic" not in audit
        assert unused.call_count == 0
    finally:
        restored.stop()


@pytest.mark.parametrize(
    "tamper", ["fingerprint", "input", "output", "stage", "links", "missing-start"]
)
def test_provenance_rejects_changed_or_incomplete_source_records(
    tmp_path: Path, tamper: str
) -> None:
    from heartwood.core_adapter.workflow_provenance import (
        experiment_event_fingerprint,
        workflow_experiment_events,
    )
    from heartwood.schemas.experiments import ExperimentEvent, ExperimentEvidence
    from heartwood.session import SessionEvent

    gateway = _gateway(tmp_path, FinishedBackend())
    try:
        _start(gateway, _inputs(tmp_path))
        gateway.handle(_transition(gateway, "run"))
        _readiness(tmp_path)
        gateway.handle(_transition(gateway, "evaluate"))
        events = list(gateway._services["research"].replay_events())
        originals = workflow_experiment_events(events)
        assert len(originals) == 2
        target = next(
            index
            for index, source in enumerate(events)
            if source.payload.get("experiment") is not None
            and ExperimentEvent.model_validate(source.payload["experiment"]).status
            == ("started" if tamper in {"input", "stage", "missing-start"} else "succeeded")
        )
        source = events[target]
        payload = dict(source.payload)
        event = ExperimentEvent.model_validate(payload["experiment"])
        if tamper == "missing-start":
            del payload["experiment"]
        else:
            data = event.model_dump(mode="json")
            if tamper == "input":
                data["definition"]["inputs"][0]["sha256"] = "0" * 64
            elif tamper == "output":
                data["outputs"][0]["sha256"] = "0" * 64
            elif tamper == "stage":
                data["definition"]["stage"]["stage_id"] = "report"
            elif tamper == "links":
                data["evidence"] = [
                    ExperimentEvidence(
                        event_id="fabricated", event_sha256="0" * 64, kind="tool.execution.recorded"
                    ).model_dump(mode="json")
                ]
            changed = ExperimentEvent.model_validate(data)
            payload["experiment"] = cast(dict[str, JsonValue], changed.model_dump(mode="json"))
            payload["experiment_fingerprint"] = (
                "0" * 64 if tamper == "fingerprint" else experiment_event_fingerprint(changed)
            )
        events[target] = SessionEvent.model_validate({**source.model_dump(), "payload": payload})
        with pytest.raises(ValueError, match=r"[Ee]xperiment"):
            workflow_experiment_events(events)
        assert (
            workflow_experiment_events(gateway._services["research"].replay_events()) == originals
        )
    finally:
        gateway.stop()


def test_project_provenance_rebuild_does_not_read_new_files_or_repeat_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from heartwood.gateway import RestGateway, RestRequest
    from heartwood.gateway._experiment_store import ExperimentStore
    from heartwood.schemas.experiments import ExperimentEvent

    backend = FinishedBackend()
    gateway = _gateway(tmp_path, backend)
    try:
        _start(gateway, _inputs(tmp_path))
        gateway.handle(_transition(gateway, "run"))
        _readiness(tmp_path)
        gateway.handle(_transition(gateway, "evaluate"))
        expected = gateway.session_projection(session_id="research").experiments
        (tmp_path / "results/readiness.json").write_text("Changed after acceptance")
        original = ExperimentStore.append
        calls = 0

        def fail_after_first(self: ExperimentStore, event: ExperimentEvent) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("synthetic materialization failure")
            original(self, event)

        with monkeypatch.context() as patched:
            patched.setattr(ExperimentStore, "append", fail_after_first)
            with pytest.raises(OSError, match="synthetic materialization failure"):
                gateway.experiment_records()
        assert gateway.experiment_records().runs == expected
        assert len(backend.prompts) == 1
        response = RestGateway(gateway).handle(
            RestRequest(method="GET", path="/research/experiments")
        )
        assert response.status_code == 200
        assert response.body["retention"] == "project-local"
        exported = RestGateway(gateway).handle(
            RestRequest(method="GET", path="/research/experiments/export")
        )
        assert exported.body == gateway.export_experiments().model_dump(mode="json")
        assert "Changed after acceptance" not in str(exported.body)
    finally:
        gateway.stop()


def test_empty_project_provenance_queries_do_not_create_state(tmp_path: Path) -> None:
    from heartwood.gateway import RestGateway, RestRequest

    gateway = _gateway(tmp_path, FinishedBackend())
    try:
        assert gateway.experiment_records().runs == ()
        assert gateway.export_experiments().jsonl == ""
        for path in ("/research/experiments", "/research/experiments/export"):
            assert (
                RestGateway(gateway).handle(RestRequest(method="GET", path=path)).status_code == 200
            )
        assert list(tmp_path.iterdir()) == []
    finally:
        gateway.stop()


def test_project_provenance_export_rejects_corrupt_records(tmp_path: Path) -> None:
    from heartwood.gateway import RestGateway, RestRequest

    backend = FinishedBackend()
    gateway = _gateway(tmp_path, backend)
    try:
        gateway.project.initialize()
        (gateway.project.state_root / "experiments.jsonl").write_text("private-corrupt-record\n")
        for path in ("/research/experiments", "/research/experiments/export"):
            result = RestGateway(gateway).handle(RestRequest(method="GET", path=path))
            assert result.status_code == 409
            assert "private-corrupt-record" not in str(result.body)
        assert not backend.prompts
    finally:
        gateway.stop()


@pytest.mark.parametrize("mutation", [None, "input", "program", "destination"])
def test_real_sdk_baseline_reproduces_through_journaled_actions(
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str | None,
) -> None:
    monkeypatch.setenv("PATH", f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}")
    task = next(task for task in research_tasks() if task.case.case_id == "baseline-analysis")
    for name, content in task.inputs.items():
        (tmp_path / name).write_text(content)
    source = (
        Path(__file__).parents[2] / "compliance/tests/fixtures/research/reference_analysis.py"
    ).read_text()
    question = "Does measurement predict response for held-out subjects?"
    plan = json.dumps(
        {
            "question": question,
            "estimand": "Held-out visit prediction error",
            "outcome": "response",
            "features": ["measurement"],
            "group_column": "subject_id",
            "split_column": "partition",
            "assumptions": ["Prespecified subject-disjoint split"],
            "limitations": ["Small synthetic dataset"],
        }
    )

    def create(name: str, content: str) -> Message:
        return _tool_message(
            "file_editor",
            command="create",
            path=str(tmp_path / "results" / name),
            file_text=content,
        )

    def finish() -> Message:
        return _tool_message(
            "finish",
            message="Stage complete",
            status="success",
            outcome_summary="Independent checks still required.",
        )

    mkdir = _tool_message("terminal", command="mkdir results")
    plan_message = create("plan.json", plan)
    assert mkdir.tool_calls
    assert plan_message.tool_calls
    llm = TestLLM.from_messages(
        [
            Message(
                role="assistant",
                content=[],
                tool_calls=[*mkdir.tool_calls, *plan_message.tool_calls],
            ),
            finish(),
            create("analysis.py", source),
            _tool_message(
                "terminal",
                command="python results/analysis.py --data data.csv --output-dir results",
            ),
            finish(),
            _tool_message(
                "terminal",
                command=(
                    "python results/analysis.py --data data.csv --output-dir results/reproduced"
                ),
            ),
            create(
                "verification.json",
                json.dumps(
                    {
                        "status": "reproduced",
                        "matching_artifacts": ["metrics.json", "predictions.csv"],
                        "mismatched_artifacts": [],
                    }
                ),
            ),
            finish(),
            create(
                "report.md",
                "# Baseline\nReproduced synthetic analysis; not scientific validation.\n",
            ),
            finish(),
        ]
    )
    gateway = _sdk_gateway(tmp_path, llm, monkeypatch)
    try:
        gateway.handle(
            _command(
                action="start",
                workflow_id="baseline-analysis",
                inputs={
                    "data": "data.csv",
                    "dictionary": "dictionary.json",
                    "question": question,
                },
                output_directory="results",
            )
        )
        for stage_id in ("plan", "execute", "verify", "report"):
            assert _state(gateway).stage_id == stage_id
            gateway.handle(_projected_command(gateway, "run"))
            for _ in range(5):
                assert gateway.wait_for_session_idle(session_id="research", timeout=30)
                projection = gateway.session_projection(session_id="research")
                group = projection.pending_approval
                if group is None:
                    assert projection.lifecycle.status == "finished"
                    break
                assert projection.workflow_controls == ()
                if stage_id == "verify" and group.actions[0].tool_name == "terminal":
                    assert not (tmp_path / "results/reproduced").exists()
                    if mutation in {"input", "program"}:
                        path = tmp_path / (
                            "data.csv" if mutation == "input" else "results/analysis.py"
                        )
                        path.write_text(path.read_text() + "\n")
                    elif mutation == "destination":
                        (tmp_path / "results/reproduced").mkdir()
                approval = SessionCommand(
                    command_id=uuid4().hex,
                    session_id="research",
                    kind=CommandKind.APPROVE,
                    created_at="2026-09-11T00:00:00Z",
                    payload={"target_id": group.group_id},
                )
                gateway.handle(approval)
                assert gateway.handle(approval).replayed
            else:
                pytest.fail("Synthetic stage did not settle within its bounded action count")
            gateway.handle(_projected_command(gateway, "evaluate"))
            state = _state(gateway)
            if stage_id == "verify" and mutation is not None:
                assert not any(item.assessment.stage_id == "verify" for item in state.completed)
                assert not any(
                    event.kind == EventKind.WORKFLOW_EXECUTION_RECORDED
                    for event in gateway._services["research"].replay_events()
                )
                assert state.phase != "completed"
                return
            assert state.phase != "blocked", state
            if stage_id == "plan":
                assert state.evaluation is not None
                gateway.handle(_projected_command(gateway, "accept"))
        assert _state(gateway).phase == "review"
        events = gateway._services["research"].replay_events()
        proof = [event for event in events if event.kind == EventKind.WORKFLOW_EXECUTION_RECORDED]
        assert [event.payload["status"] for event in proof] == ["prepared", "succeeded"]
        assert proof[1].payload["preparation_event_id"] == proof[0].event_id
        assert (tmp_path / "results/metrics.json").read_bytes() == (
            tmp_path / "results/reproduced/metrics.json"
        ).read_bytes()
        assert llm.call_count == 10
        before = _state(gateway)
    finally:
        gateway.stop()
    unused = TestLLM.from_messages([])
    restored = _sdk_gateway(tmp_path, unused, monkeypatch)
    try:
        assert before.evaluation is not None
        restored.handle(
            _transition(
                restored,
                "review",
                approved=True,
                evidence_fingerprint=before.evaluation.assessment.evidence_fingerprint,
            )
        )
        assert _state(restored).phase == "completed"
        assert unused.call_count == 0
        experiments = restored.experiment_records().runs
        assert len(experiments) == 4
        assert [
            item.definition.stage.stage_id for item in experiments if item.definition.stage
        ] == ["plan", "execute", "verify", "report"]
        assert all(item.status == "succeeded" for item in experiments)
        execution, verification = experiments[1:3]
        assert execution.definition.code_output_paths == ("results/analysis.py",)
        program = next(item for item in execution.outputs if item.path == "results/analysis.py")
        assert verification.definition.code == (program,)
        assert any(item.kind == "workflow.execution.recorded" for item in verification.evidence)
        assert any(item.kind == "approval.recorded" for item in verification.evidence)
        assert experiments == restored.session_projection(session_id="research").experiments
        assert restored.export_experiments() == restored.export_experiments()
        assert unused.call_count == 0
        from heartwood.cli._interactive import InteractiveSession
        from heartwood.notebook import build_view_model, build_widget_spec

        terminal = InteractiveSession(restored, session_id="research").submit("/experiments")
        assert terminal.message is not None
        assert str(verification.run_id) in terminal.message
        assert program.sha256 in terminal.message
        assert not terminal.events
        notebook = build_view_model(restored.session_projection(session_id="research"))
        assert notebook.experiments == experiments
        section = next(
            item for item in build_widget_spec(notebook) if item.title == "Experiment Records"
        )
        assert any(str(verification.run_id) in item for item in section.items)
        assert unused.call_count == 0
        restored.handle(
            SessionCommand(
                command_id="export",
                session_id="research",
                kind=CommandKind.AUDIT_EXPORT,
                created_at="2026-09-11T00:00:00Z",
            )
        )
        audit = (restored.sessions_root / "research/audit-export.jsonl").read_text()
        assert "workflow.execution.recorded" in audit
        assert "observation_fingerprint" in audit
        assert "results/analysis.py" not in audit
        assert str(tmp_path) not in audit
        assert "subject_id" not in audit
    finally:
        restored.stop()
    _checkpoint_research_project(tmp_path, tmp_path_factory.mktemp("research-checkpoint"))
    assert unused.call_count == 0


def _checkpoint_research_project(root: Path, deployment: Path) -> None:
    from heartwood.audit import (
        LocalEd25519CheckpointSigner,
        initialize_local_checkpoint_signer,
        load_checkpoint_signer_registry,
    )

    setup = initialize_local_checkpoint_signer(directory=deployment / "signer")
    gateway = SessionGateway(
        project=ProjectContext(root),
        backend_id="deterministic",
        checkpoint_signer_registry=load_checkpoint_signer_registry(setup.registry),
        checkpoint_signer_factory=lambda profile: LocalEd25519CheckpointSigner(
            private_key=setup.private_key,
            signer_id=profile.signer_id,
            key_id=profile.key_id,
            key_version=profile.key_version,
        ),
    )
    try:
        snapshot = gateway.export_experiments()
        assert len(gateway.experiment_records().runs) == 4
        bundle = deployment / "baseline"
        created = gateway.create_audit_checkpoint(
            session_id="research",
            output=bundle,
            deployment_id="synthetic-research",
            retention_policy_id="research-audit-7y",
            retain_until="2033-08-02",
            include_experiments=True,
        )
        assert gateway.verify_audit_checkpoint(bundle=bundle) == created
        assert created.experiments is not None
        assert created.experiments.sha256 == snapshot.sha256
        assert (bundle / "experiments.jsonl").read_bytes() == snapshot.jsonl.encode()
    finally:
        gateway.stop()


@pytest.mark.parametrize("calls", [20, 21, 80])
def test_observed_stage_budget_blocks_more_work_but_allows_exact_completion(
    tmp_path: Path,
    calls: int,
) -> None:
    backend = FinishedBackend()
    backend.model_calls = calls
    gateway = _gateway(tmp_path, backend)
    try:
        _start(gateway, _inputs(tmp_path))
        gateway.handle(_transition(gateway, "run"))
        _readiness(tmp_path)
        response = gateway.handle(
            SessionCommand(
                command_id="extra-work",
                session_id="research",
                kind=CommandKind.CHAT,
                created_at="2026-09-11T00:00:00Z",
                payload={"prompt": "Do another analysis"},
            )
        )
        assert any(
            "budget reached" in str(event.payload.get("reason")) for event in response.events
        )
        assert len(backend.prompts) == 1
        # The declined user turn cannot borrow a previous structured completion.
        gateway.handle(_transition(gateway, "evaluate"))
        assert not _state(gateway).completed
    finally:
        gateway.stop()


@pytest.mark.parametrize(("calls", "phase"), [(20, "ready"), (21, "running")])
def test_budget_completion_boundary(tmp_path: Path, calls: int, phase: str) -> None:
    backend = FinishedBackend()
    backend.model_calls = calls
    gateway = _gateway(tmp_path, backend)
    try:
        _start(gateway, _inputs(tmp_path))
        gateway.handle(_transition(gateway, "run"))
        _readiness(tmp_path)
        gateway.handle(_transition(gateway, "evaluate"))
        assert _state(gateway).phase == phase
    finally:
        gateway.stop()


def test_expired_workflow_can_be_cancelled_without_more_model_work(tmp_path: Path) -> None:
    from datetime import timedelta

    backend = FinishedBackend()
    gateway = _gateway(tmp_path, backend)
    try:
        _start(gateway, _inputs(tmp_path))
        gateway.handle(_transition(gateway, "run"))
        service = gateway._services["research"]
        service.clock = lambda: (_state(gateway).created_at + timedelta(hours=2)).isoformat()
        _readiness(tmp_path)
        response = gateway.handle(_transition(gateway, "evaluate"))
        assert any(
            "budget reached" in str(event.payload.get("reason")) for event in response.events
        )
        gateway.handle(_transition(gateway, "cancel"))
        assert _state(gateway).phase == "cancelled"
        (record,) = gateway.session_projection(session_id="research").experiments
        assert record.status == "cancelled"
        assert record.outputs == ()
        assert len(backend.prompts) == 1
    finally:
        gateway.stop()


def test_changed_accepted_stage_cannot_feed_a_later_stage(tmp_path: Path) -> None:
    backend = FinishedBackend()
    gateway = _gateway(tmp_path, backend)
    try:
        _start(gateway, _inputs(tmp_path))
        gateway.handle(_transition(gateway, "run"))
        _readiness(tmp_path)
        gateway.handle(_transition(gateway, "evaluate"))
        assert _state(gateway).phase == "ready"
        (tmp_path / "results/readiness.json").write_text("{}")
        result = gateway.handle(_transition(gateway, "run"))
        assert any(event.kind == EventKind.ERROR_RECORDED for event in result.events)
        assert len(backend.prompts) == 1
    finally:
        gateway.stop()


def test_start_rejects_existing_sdk_state_without_modifying_it(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    gateway = _gateway(tmp_path, FinishedBackend())
    persistence = gateway.sessions_root / "research/openhands"
    gateway.project.initialize()
    persistence.mkdir(parents=True)
    marker = persistence / "existing"
    marker.write_text("retain me")
    try:
        result = gateway.handle(
            _command(
                action="start",
                workflow_id="dataset-readiness",
                inputs=inputs,
                output_directory="results",
            )
        )
        assert any(event.kind == EventKind.ERROR_RECORDED for event in result.events)
        assert gateway.persisted_session_projection(session_id="research").workflow is None
        assert marker.read_text() == "retain me"
    finally:
        gateway.stop()


def test_review_can_be_declined_after_its_budget_expires(tmp_path: Path) -> None:
    from datetime import timedelta

    gateway = _gateway(tmp_path, FinishedBackend())
    try:
        _start(gateway, _inputs(tmp_path))
        gateway.handle(_transition(gateway, "run"))
        _readiness(tmp_path)
        gateway.handle(_transition(gateway, "evaluate"))
        gateway.handle(_transition(gateway, "run"))
        (tmp_path / "results/readiness.md").write_text("# Findings\nSynthetic only.\n")
        gateway.handle(_transition(gateway, "evaluate"))
        state = _state(gateway)
        assert state.evaluation is not None
        gateway._services["research"].clock = lambda: (
            state.created_at + timedelta(hours=2)
        ).isoformat()
        gateway.handle(
            _transition(
                gateway,
                "review",
                approved=False,
                evidence_fingerprint=state.evaluation.assessment.evidence_fingerprint,
            )
        )
        assert _state(gateway).phase == "blocked"
    finally:
        gateway.stop()


@pytest.mark.parametrize(
    ("field", "value"),
    [("approved", "true"), ("approved", 1), ("revision", True), ("revision", 4.0)],
)
def test_review_does_not_coerce_ambiguous_permission_values(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    gateway = _gateway(tmp_path, FinishedBackend())
    try:
        _start(gateway, _inputs(tmp_path))
        gateway.handle(_transition(gateway, "run"))
        _readiness(tmp_path)
        gateway.handle(_transition(gateway, "evaluate"))
        gateway.handle(_transition(gateway, "run"))
        (tmp_path / "results/readiness.md").write_text("# Findings\nSynthetic only.\n")
        gateway.handle(_transition(gateway, "evaluate"))
        state = _state(gateway)
        assert state.evaluation is not None
        command = _transition(
            gateway,
            "review",
            approved=True,
            evidence_fingerprint=state.evaluation.assessment.evidence_fingerprint,
        )
        command = command.model_copy(update={"payload": {**command.payload, field: value}})
        result = gateway.handle(command)
        assert any(event.kind == EventKind.ERROR_RECORDED for event in result.events)
        assert _state(gateway) == state
    finally:
        gateway.stop()
