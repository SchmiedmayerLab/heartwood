# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Workflow transitions share normal command receipts, tools, and project evidence."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest
from openhands.sdk.llm import Message, MessageToolCall
from openhands.sdk.testing import TestLLM

from heartwood.compliance.research import ResearchTask, research_tasks
from heartwood.compliance.review_benchmarks import (
    planning_review_suite,
    planning_review_tasks,
    verify_planning_review,
)
from heartwood.compliance.review_runner import run_planning_review_trial
from heartwood.compliance.review_trials import ReservedReviewTrial, reserve_planning_review_trial
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
from heartwood.core_adapter.research_workflows import research_workflow
from heartwood.gateway import ModelProfile, OpenHandsSdkBackend, ProjectContext, SessionGateway
from heartwood.gateway import _openhands_sdk as sdk_module
from heartwood.gateway._research_evaluation import ParallelReviewPreparer, ResearchStageEvaluator
from heartwood.schemas import JsonValue
from heartwood.schemas.evaluation import EvaluationRuntimeObservation
from heartwood.schemas.parallel_reviews import (
    ParallelReviewPlan,
    ReviewDispatchAction,
    ReviewExecutionPlan,
    ReviewQualifications,
)
from heartwood.schemas.review import (
    ResearchReviewRun,
    ReviewProposals,
    ReviewSnapshot,
    ReviewSubmission,
    review_digest,
)
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


def _gateway(
    root: Path,
    backend: FinishedBackend,
    *,
    parallel_review_preparer: ParallelReviewPreparer | None = None,
) -> SessionGateway:
    gateway = SessionGateway(
        project=ProjectContext(root),
        env={},
        parallel_review_preparer=parallel_review_preparer,
        service_factory=lambda sessions, session_id: SessionService.local_default(
            sessions,
            session_id=session_id,
            backend=backend,
            env={},
            workflow_evaluator=ResearchStageEvaluator(
                gateway.workspace_inspector,
                parallel_review_preparer=parallel_review_preparer,
            ),
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
        reviewer = json.loads(prompt.split("\n", 1)[1])["reviewers"][0]
        specialist = BackendSubagent(
            invocation_id="review-call",
            task_id="native-review",
            agent_name=reviewer,
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
@pytest.mark.parametrize("operation", ["review", "correction", "correction-retry"])
def test_automatic_review_recovers_each_append_boundary_without_model_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str, operation: str
) -> None:
    from heartwood.core_adapter import _state as state_module

    backend = ReviewBackend()
    gateway = _gateway(tmp_path, backend)
    transition = "assess-review" if operation == "review" else "assess-correction"
    try:
        if operation == "review":
            _start(gateway, _inputs(tmp_path))
            gateway.handle(_transition(gateway, "run"))
            _readiness(tmp_path)
            command = _projected_command(gateway, "request-review")
            gateway.handle(command)
            finish = backend.finish_review
        else:
            _begin_seeded_code_review(gateway, backend, tmp_path)
            backend.finish_review()
            submit = backend.submit_turn
            held: tuple[BackendEvent, ...] = ()

            def defer(*, session_id: str, prompt: str) -> tuple[BackendEvent, ...]:
                nonlocal held
                held = submit(session_id=session_id, prompt=prompt)
                backend.idle = False
                return (BackendLifecycleEvent(lifecycle=BackendLifecycle.RUNNING),)

            monkeypatch.setattr(backend, "submit_turn", defer)
            command = _projected_command(gateway, "correct")
            gateway.handle(command)
            attempt = _state(gateway).corrections[-1].attempts[-1]
            output = tmp_path / attempt.plan.outputs[0].path
            output.parent.mkdir()
            output.write_text(
                "def still_broken(\n" if operation == "correction-retry" else "print('corrected')\n"
            )

            def finish() -> None:
                backend.idle = True
                backend._event_sink(held)

        store = gateway._services["research"].store
        append = state_module._append_private_json_line
        write = state_module._write_private_json_atomic

        def interrupt_append(path: Path, text: str) -> None:
            payload = json.loads(text)
            target = store.audit_path if boundary.startswith("audit") else store.events_path
            if path != target or payload.get("payload", {}).get("transition") != transition:
                append(path, text)
                return
            if boundary.endswith("after"):
                append(path, text)
            raise OSError("Synthetic review append interruption")

        def interrupt_intent(path: Path, value: dict[str, object]) -> None:
            event = value.get("session_event")
            if isinstance(event, dict) and event.get("payload", {}).get("transition") == transition:
                raise OSError("Synthetic review append interruption")
            write(path, value)

        with monkeypatch.context() as patches:
            if boundary == "intent":
                patches.setattr(state_module, "_write_private_json_atomic", interrupt_intent)
            else:
                patches.setattr(state_module, "_append_private_json_line", interrupt_append)
            with pytest.raises(OSError, match="Synthetic review append interruption"):
                finish()
    finally:
        gateway.stop()
    replacement = ReviewBackend()
    restored = _gateway(tmp_path, replacement)
    try:
        assert restored.handle(command).replayed
        view = restored.session_projection(session_id="research")
        assert view.workflow is not None
        if operation == "review":
            assert view.workflow.research_review is not None
            assert view.workflow.research_review.status == "assessed"
        elif operation == "correction":
            assert view.workflow.corrections[-1].stop_reason == "corrected"
            assert restored.experiment_records().runs[-1].status == "succeeded"
        else:
            assert view.workflow.corrections[-1].stop_reason is None
            assert len(view.workflow.corrections[-1].attempts) == 1
            assert restored.experiment_records().runs[-1].status == "failed"
        service = restored._services["research"]
        service.reconcile()
        service.reconcile()
        events = service.replay_events()
        assert sum(event.payload.get("transition") == transition for event in events) == 1
        assert not replacement.prompts
        if operation == "correction-retry":
            before = _state(restored)
            submit = replacement.submit_turn

            def correct_again(*, session_id: str, prompt: str) -> tuple[BackendEvent, ...]:
                attempt = _state(restored).corrections[-1].attempts[-1]
                path = tmp_path / attempt.plan.outputs[0].path
                path.parent.mkdir()
                path.write_text("print('corrected')\n")
                return submit(session_id=session_id, prompt=prompt)

            monkeypatch.setattr(replacement, "submit_turn", correct_again)
            resume = _projected_command(restored, "correct")
            restored.handle(resume)
            continued = _state(restored)
            assert continued.corrections[-1].stop_reason == "corrected"
            assert len(continued.corrections[-1].attempts) == 2
            assert continued.corrections[-1].attempts[0] == before.corrections[-1].attempts[0]
            assert continued.stage_usage_baseline == before.stage_usage_baseline
            assert continued.stage_started_at == before.stage_started_at
            assert restored.handle(resume).replayed
            assert len(replacement.prompts) == 1
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
    backend = ReviewBackend()
    gateway = _gateway(tmp_path, backend)
    try:
        source = _begin_seeded_code_review(gateway, backend, tmp_path)
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


def _begin_seeded_code_review(
    gateway: SessionGateway,
    backend: ReviewBackend,
    root: Path,
) -> Path:
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
    _begin_baseline_execution(gateway, root)
    source = root / "results/analysis.py"
    source.write_text("def broken(\n")
    (root / "results/metrics.json").write_text("{}")
    (root / "results/predictions.csv").write_text("prediction\n")
    gateway.handle(_projected_command(gateway, "request-review"))
    return source


def _begin_baseline_plan(
    gateway: SessionGateway, root: Path, *, question: str = "Predict response"
) -> None:
    task = next(item for item in research_tasks() if item.case.case_id == "baseline-analysis")
    for name, content in task.inputs.items():
        (root / name).write_text(content)
    gateway.handle(
        _command(
            action="start",
            workflow_id="baseline-analysis",
            output_directory="results",
            inputs={
                "data": "data.csv",
                "dictionary": "dictionary.json",
                "question": question,
            },
        )
    )
    gateway.handle(_projected_command(gateway, "run"))
    (root / "results").mkdir()
    (root / "results/plan.json").write_text(
        json.dumps(
            {
                "question": question,
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


def _begin_baseline_execution(gateway: SessionGateway, root: Path) -> None:
    _begin_baseline_plan(gateway, root)
    gateway.handle(_projected_command(gateway, "evaluate"))
    gateway.handle(_projected_command(gateway, "accept"))
    assert _state(gateway).stage_id == "execute"
    gateway.handle(_projected_command(gateway, "run"))


@pytest.mark.parametrize(
    "result", ["corrected", "attempt-limit", "missing", "changed-source", "changed-history"]
)
def test_bounded_workflow_correction_journals_and_rechecks_attempts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    result: str,
) -> None:
    backend = ReviewBackend()
    gateway = _gateway(tmp_path, backend)
    try:
        source = _begin_seeded_code_review(gateway, backend, tmp_path)
        backend.finish_review()
        before = _state(gateway)
        submit = backend.submit_turn
        directories: list[Path] = []

        def correct(*, session_id: str, prompt: str) -> tuple[BackendEvent, ...]:
            events = submit(session_id=session_id, prompt=prompt)
            if not prompt.startswith("Correct only the independently verified findings"):
                return events
            series = _state(gateway).corrections[-1]
            attempt = series.attempts[-1]
            assert attempt.status == "pending"
            assert (
                attempt.plan.model_dump(mode="json") == json.loads(prompt.split("\n", 1)[1])["plan"]
            )
            assert gateway.experiment_records().runs[-1].status == "started"
            directory = tmp_path / attempt.plan.output_directory
            assert not directory.exists()
            directories.append(directory)
            if result != "missing":
                directory.mkdir()
                (tmp_path / attempt.plan.outputs[0].path).write_text(
                    "print('corrected')\n"
                    if result == "corrected" and len(directories) == 2
                    else "def still_broken(\n"
                )
            if result == "changed-source":
                source.write_text("print('changed source')\n")
            if result == "changed-history" and len(series.attempts) == 2:
                (tmp_path / series.attempts[0].plan.outputs[0].path).write_text(
                    "print('changed earlier attempt')\n"
                )
            return events

        monkeypatch.setattr(backend, "submit_turn", correct)
        command = _projected_command(gateway, "correct")
        response = gateway.handle(command)
        current = _state(gateway)
        series = current.corrections[-1]
        assert series.stop_reason == (
            result if result in {"corrected", "attempt-limit"} else "unavailable"
        )
        from heartwood.cli._interactive import format_workflow_lines
        from heartwood.notebook import build_view_model, build_widget_spec

        projection = gateway.session_projection(session_id="research")
        terminal = "\n".join(format_workflow_lines(projection))
        notebook = "\n".join(
            item
            for section in build_widget_spec(build_view_model(projection))
            for item in section.items
        )
        for attempt in series.attempts:
            for output in attempt.plan.outputs:
                assert output.path in terminal
                assert output.path in notebook
        assert series.stop_reason.replace("-", " ") in terminal
        assert series.stop_reason.replace("-", " ") in notebook
        assert len(series.attempts) == (1 if result in {"missing", "changed-source"} else 2)
        assert current.stage_started_at == before.stage_started_at
        assert current.stage_usage_baseline == before.stage_usage_baseline
        assert current.completed == before.completed
        if result != "changed-source":
            assert source.read_text() == "def broken(\n"
        assert current.binding.inputs == before.binding.inputs
        if result == "corrected":
            assert (
                current.binding.artifact_path("program") == series.attempts[-1].plan.outputs[0].path
            )
            assert series.attempts[-1].assessment is not None
            assert series.attempts[-1].assessment.checks[0].status == "not_observed"
            assert (
                tmp_path / series.attempts[0].plan.outputs[0].path
            ).read_text() == "def still_broken(\n"
            gateway.handle(_projected_command(gateway, "evaluate"))
            assert _state(gateway).phase == "blocked"  # Other baseline outputs are still invalid.
        else:
            assert current.binding == before.binding
        experiments = gateway.experiment_records().runs
        assert [item.status for item in experiments] == [
            "succeeded",
            "failed",
            *(["failed"] * (len(series.attempts) - 1)),
            "succeeded" if result == "corrected" else "failed",
        ]
        assert all(item.definition.stage is not None for item in experiments)
        assert experiments[-1].definition.stage is not None
        assert experiments[-1].definition.stage.correction_id == series.attempts[-1].attempt_id
        calls = len(backend.prompts)
        assert gateway.handle(command).events == response.events
        assert len(backend.prompts) == calls
        state = _state(gateway)
        export = gateway.export_experiments()
    finally:
        gateway.stop()
    restored_backend = FinishedBackend()
    restored = _gateway(tmp_path, restored_backend)
    try:
        assert _state(restored) == state
        assert restored.export_experiments() == export
        assert restored.handle(command).replayed
        assert not restored_backend.prompts
    finally:
        restored.stop()


@pytest.mark.parametrize("action", ["cancel", "steer", "pause", "denied", "budget"])
def test_correction_respects_control_and_admission_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    backend = ReviewBackend()
    gateway = _gateway(tmp_path, backend)
    try:
        source = _begin_seeded_code_review(gateway, backend, tmp_path)
        backend.finish_review()
        submit = backend.submit_turn
        held: tuple[BackendEvent, ...] = ()

        def defer(*, session_id: str, prompt: str) -> tuple[BackendEvent, ...]:
            nonlocal held
            if prompt.startswith("Correct only the independently verified findings"):
                if action == "budget":
                    backend.model_calls = 100
                held = submit(session_id=session_id, prompt=prompt)
                backend.idle = False
                return (BackendLifecycleEvent(lifecycle=BackendLifecycle.RUNNING),)
            return submit(session_id=session_id, prompt=prompt)

        monkeypatch.setattr(backend, "submit_turn", defer)
        if action == "denied":
            monkeypatch.setattr(
                type(backend),
                "configuration_error",
                property(lambda _self: "Synthetic unavailable model"),
            )
        gateway.handle(_projected_command(gateway, "correct"))
        if action == "denied":
            series = _state(gateway).corrections[-1]
            assert series.stop_reason == "unavailable"
            assert series.attempts[-1].status == "unavailable"
            assert len(backend.prompts) == 3
            return
        assert len(backend.prompts) == 4
        pending = _state(gateway).corrections[-1]
        assert pending.attempts[-1].status == "pending"
        backend.idle = True
        if action == "cancel":
            backend._event_sink((BackendLifecycleEvent(lifecycle=BackendLifecycle.PAUSED),))
            gateway.handle(_projected_command(gateway, "cancel"))
            backend._event_sink(held)
            assert _state(gateway).phase == "cancelled"
            assert _state(gateway).corrections[-1].stop_reason == "cancelled"
            assert gateway.experiment_records().runs[-1].status == "cancelled"
        elif action == "steer":
            gateway.handle(
                SessionCommand(
                    command_id="steer-correction",
                    session_id="research",
                    kind=CommandKind.CHAT,
                    created_at="2026-09-11T00:00:00Z",
                    payload={"prompt": "Stop correcting and explain the limitations."},
                )
            )
            backend._event_sink(held)
            series = _state(gateway).corrections[-1]
            assert series.stop_reason == "unavailable"
            assert series.attempts[-1].unavailable_reason == "changed-context"
        else:
            attempt = pending.attempts[-1]
            if action == "pause":
                backend._event_sink((BackendLifecycleEvent(lifecycle=BackendLifecycle.PAUSED),))
                assert _state(gateway).corrections[-1] == pending
            output = tmp_path / attempt.plan.outputs[0].path
            output.parent.mkdir()
            output.write_text(
                "def still_broken(\n" if action == "budget" else "print('corrected')\n"
            )
            backend._event_sink(held)
            assert _state(gateway).corrections[-1].stop_reason == (
                "budget" if action == "budget" else "corrected"
            )
            assert len(backend.prompts) == 4
        assert source.read_text() == "def broken(\n"
        assert len(_state(gateway).corrections[-1].attempts) == 1
        before = _state(gateway)
        backend._event_sink((BackendExecutionSettledEvent(),))
        assert _state(gateway) == before
    finally:
        gateway.stop()


def test_redirected_conversation_cannot_start_correction_from_an_old_review(tmp_path: Path) -> None:
    backend = ReviewBackend()
    gateway = _gateway(tmp_path, backend)
    try:
        _begin_seeded_code_review(gateway, backend, tmp_path)
        backend.finish_review()
        command = _projected_command(gateway, "correct")
        gateway.handle(
            SessionCommand(
                command_id="redirect-before-correction",
                session_id="research",
                kind=CommandKind.CHAT,
                created_at="2026-09-11T00:00:00Z",
                payload={"prompt": "Explain the limitations instead; do not correct files."},
            )
        )
        calls = len(backend.prompts)
        response = gateway.handle(command)
        assert any("Conversation changed" in str(event.payload) for event in response.events)
        assert len(backend.prompts) == calls
        assert _state(gateway).corrections == ()
        assert gateway.experiment_records().runs[-1].status == "started"
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


@pytest.mark.parametrize(
    "boundary",
    ["intent", "audit-before", "audit-after", "events-before", "events-after", "dispatch"],
)
def test_correction_admission_interruption_never_repeats_uncertain_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    from heartwood.core_adapter import _state as state_module

    backend = ReviewBackend()
    gateway = _gateway(tmp_path, backend)
    try:
        _begin_seeded_code_review(gateway, backend, tmp_path)
        backend.finish_review()
        command = _projected_command(gateway, "correct")
        store = gateway._services["research"].store
        append = state_module._append_private_json_line
        write = state_module._write_private_json_atomic

        def correction_start(payload: dict[str, object]) -> bool:
            experiment = payload.get("experiment")
            return (
                payload.get("transition") == "correct"
                and isinstance(experiment, dict)
                and experiment.get("status") == "started"
            )

        def interrupt_append(path: Path, text: str) -> None:
            target = store.audit_path if boundary.startswith("audit") else store.events_path
            payload = json.loads(text).get("payload", {})
            # The audit contains only the experiment fingerprint, not the scientific record.
            is_start = (
                payload.get("transition") == "correct" and payload.get("phase") == "running"
                if path == store.audit_path
                else correction_start(payload)
            )
            if path != target or not is_start:
                append(path, text)
                return
            if boundary.endswith("after"):
                append(path, text)
            raise OSError("Synthetic correction admission interruption")

        def interrupt_intent(path: Path, value: dict[str, object]) -> None:
            event = value.get("session_event")
            if isinstance(event, dict) and correction_start(event.get("payload", {})):
                raise OSError("Synthetic correction admission interruption")
            write(path, value)

        with monkeypatch.context() as patches:
            if boundary == "dispatch":
                backend.fail_submission = True
                with pytest.raises(RuntimeError, match="Synthetic interruption"):
                    gateway.handle(command)
            else:
                if boundary == "intent":
                    patches.setattr(state_module, "_write_private_json_atomic", interrupt_intent)
                else:
                    patches.setattr(state_module, "_append_private_json_line", interrupt_append)
                with pytest.raises(OSError, match="Synthetic correction admission interruption"):
                    gateway.handle(command)
        assert len(backend.prompts) == (4 if boundary == "dispatch" else 3)
    finally:
        gateway.stop()
    replacement = FinishedBackend()
    restored = _gateway(tmp_path, replacement)
    try:
        with pytest.raises(SessionRecoveryError):
            restored.handle(command)
        assert not replacement.prompts
        current = _state(restored)
        assert len(current.corrections) == (0 if boundary == "intent" else 1)
        if current.corrections:
            assert current.corrections[-1].attempts[-1].status == "pending"
            assert len(current.corrections[-1].attempts) == 1
        assert restored.experiment_records().runs[1].status == "failed"
    finally:
        restored.stop()


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


@pytest.mark.parametrize("incompatible", [False, True])
def test_native_independent_verification_records_environment_execution_and_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    incompatible: bool,
    analysis_lock: Path,
) -> None:
    from heartwood.core_adapter.research_workflows import workflow_reproduction_spec
    from heartwood.gateway.python_environment import inspect_verification_environment

    expected = inspect_verification_environment().model_copy(
        update={"packages": (("synthetic-analysis", "1.0"),)}
    )
    if incompatible:
        expected = expected.model_copy(update={"python": "0.0.1"})
    (tmp_path / "environment.json").write_text(expected.model_dump_json())
    (tmp_path / "data.csv").write_text("1\n3\n5\n")
    (tmp_path / "metrics.json").write_text('{"mean": 3.0}')
    (tmp_path / "predictions.csv").write_text("centered\n-2.0\n0.0\n2.0\n")
    (tmp_path / "analysis.py").write_text(
        "import argparse, json\nfrom pathlib import Path\nfrom synthetic_analysis import mean\n"
        "p=argparse.ArgumentParser()\np.add_argument('--data')\n"
        "p.add_argument('--output-dir')\na=p.parse_args()\n"
        "values=[float(x) for x in Path(a.data).read_text().splitlines()]\n"
        "average=mean(values)\nout=Path(a.output_dir)\nout.mkdir()\n"
        "(out/'metrics.json').write_text(json.dumps({'mean':average}))\n"
        "centered=''.join(f'{x-average}\\n' for x in values)\n"
        "(out/'predictions.csv').write_text('centered\\n'+centered)\n"
    )
    inputs = {
        name: f"{name}.{suffix}"
        for name, suffix in (
            ("environment", "json"),
            ("data", "csv"),
            ("metrics", "json"),
            ("predictions", "csv"),
            ("program", "py"),
        )
    }
    inputs["program"] = "analysis.py"
    inputs["lockfile"] = analysis_lock.name
    gateway = _sdk_gateway(tmp_path, TestLLM.from_messages([]), monkeypatch)
    try:
        binding = gateway.prepare_research_workflow(
            "result-verification", inputs=inputs, output_directory="results"
        )
        probe = workflow_reproduction_spec(binding, "environment")
        rerun = workflow_reproduction_spec(binding, "reproduce")
        assert probe is not None
        assert rerun is not None
    finally:
        gateway.stop()

    def finish() -> Message:
        return _tool_message(
            "finish", message="Stage complete", status="success", outcome_summary="Check evidence."
        )

    def create(path: str, text: str) -> Message:
        return _tool_message(
            "file_editor", command="create", path=str(tmp_path / path), file_text=text
        )

    llm = TestLLM.from_messages(
        [
            _tool_message("terminal", command=probe.command),
            finish(),
            _tool_message("terminal", command=rerun.command),
            create(
                "results/verification.json",
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
                "results/verification.md",
                "# Reproduction\nSynthetic mean and centered values match.\n",
            ),
            finish(),
        ]
    )
    gateway = _sdk_gateway(tmp_path, llm, monkeypatch)
    try:
        gateway.handle(
            _command(
                action="start",
                workflow_id="result-verification",
                inputs=inputs,
                output_directory="results",
            )
        )
        (tmp_path / "results").mkdir()
        for stage_id in ("environment", "reproduce", "report"):
            assert _state(gateway).stage_id == stage_id
            gateway.handle(_projected_command(gateway, "run"))
            for _ in range(4):
                assert gateway.wait_for_session_idle(session_id="research", timeout=30)
                group = gateway.session_projection(session_id="research").pending_approval
                if group is None:
                    break
                if stage_id == "environment":
                    assert not (tmp_path / "results/environment").exists()
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
                pytest.fail("Verification did not settle")
            gateway.handle(_projected_command(gateway, "evaluate"))
            current = _state(gateway)
            if incompatible:
                assert current.evaluation is not None
                assert current.evaluation.checks[0].status in {"failed", "not_run"}
                assert current.stage_id == "environment"
                assert not (tmp_path / "results/reproduced").exists()
                return
            if current.phase == "review":
                gateway.handle(_projected_command(gateway, "accept"))
        before = _state(gateway)
        assert before.phase == "completed"
        assert (tmp_path / "results/reproduced/metrics.json").read_bytes() == (
            tmp_path / "metrics.json"
        ).read_bytes()
        events = gateway._services["research"].replay_events()
        proofs = [event for event in events if event.kind == EventKind.WORKFLOW_EXECUTION_RECORDED]
        assert [event.payload["status"] for event in proofs] == [
            "prepared",
            "succeeded",
            "prepared",
            "succeeded",
        ]
    finally:
        gateway.stop()
    unused = TestLLM.from_messages([])
    restored = _sdk_gateway(tmp_path, unused, monkeypatch)
    try:
        assert _state(restored) == before
        restored.handle(
            SessionCommand(
                command_id=uuid4().hex,
                session_id="research",
                kind=CommandKind.AUDIT_EXPORT,
                created_at="2026-09-11T00:00:00Z",
            )
        )
        audit = (restored.sessions_root / "research/audit-export.jsonl").read_text()
        assert "workflow.execution.recorded" in audit
        assert "centered" not in audit
        assert unused.call_count == 0
    finally:
        restored.stop()


def _sdk_gateway(
    root: Path,
    llm: TestLLM,
    monkeypatch: pytest.MonkeyPatch,
    *,
    specialists: bool = False,
    parallel_review_preparer: ParallelReviewPreparer | None = None,
    environment: dict[str, str] | None = None,
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

    def backend_factory(*, session_id: str, **_configuration: object) -> OpenHandsSdkBackend:
        return OpenHandsSdkBackend(
            profile=ModelProfile(
                profile_id="heartwood",
                model="openai/local-model",
                base_url="http://127.0.0.1:8765/v1",
                policy_endpoint="http://127.0.0.1:8765/v1/chat/completions",
                credential_kind="none",
            ),
            workspace=root,
            skills_dir=root / ".heartwood/skills",
            persistence_dir=gateway.sessions_root / session_id / "openhands",
            conversation_key=f"{root}#{session_id}",
            structured_task_outcomes=True,
            specialist_catalog=catalog,
            env={},
        )

    gateway = SessionGateway(
        project=ProjectContext(root),
        env=environment or {},
        parallel_review_preparer=parallel_review_preparer,
    )
    monkeypatch.setattr(gateway, "_backend", backend_factory)
    return gateway


@pytest.mark.parametrize("revoke", [False, True])
def test_native_gateway_loads_deployment_evidence_and_rechecks_before_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    revoke: bool,
    review_evidence: Callable[[EvaluationRuntimeObservation, datetime], ReviewQualifications],
) -> None:
    from openhands.sdk import LocalConversation
    from openhands.tools.task.manager import TaskManager

    child_runs: list[str] = []
    native_run = TaskManager._run_until_finished

    def run_child(manager: TaskManager, task_id: str, conversation: LocalConversation) -> None:
        child_runs.append(task_id)
        native_run(manager, task_id, conversation)

    monkeypatch.setattr(TaskManager, "_run_until_finished", run_child)
    project = tmp_path / "project"
    project.mkdir()
    evidence_path = tmp_path / "qualifications.json"
    messages = [
        _tool_message(
            "task", description="Review plan", prompt="Review synthetic plan.", subagent_type=role
        )
        for role in ("research-planner", "statistical-reviewer")
    ]
    llm = TestLLM.from_messages(
        [
            _tool_message(
                "finish", message="Plan prepared.", status="success", outcome_summary="Done."
            ),
            Message(
                role="assistant",
                content=[],
                tool_calls=[call for message in messages for call in message.tool_calls or []],
            ),
            _tool_message("finish", message="No findings.", candidates=[]),
            _tool_message("finish", message="No findings.", candidates=[]),
            _tool_message(
                "finish", message="Reviews settled.", status="success", outcome_summary="Done."
            ),
        ]
    )
    environment = {"HEARTWOOD_REVIEW_QUALIFICATIONS": str(evidence_path)}
    llm.max_input_tokens = 32768
    llm.max_output_tokens = 4096
    gateway = _sdk_gateway(project, llm, monkeypatch, specialists=True, environment=environment)
    try:
        _begin_baseline_plan(gateway, project)
        assert gateway.wait_for_session_idle(session_id="research", timeout=30)
        observed = gateway.evaluation_observation(session_id="research")
        evidence_path.write_text(review_evidence(observed, datetime.now(UTC)).model_dump_json())
        evidence_path.chmod(0o600)
        preview = _projected_command(gateway, "prepare-parallel-review")
        preview_result = gateway.handle(preview)
        assert llm.call_count == 1
        state = _state(gateway)
        assert state.parallel_review_plan is not None, [
            event.payload
            for event in preview_result.events
            if event.kind == EventKind.ERROR_RECORDED
        ]
        command = _projected_command(gateway, "request-parallel-review")
        gateway.handle(command)
        assert gateway.wait_for_session_idle(session_id="research", timeout=30)
        group = gateway.session_projection(session_id="research").pending_approval
        assert group is not None
        assert len(group.actions) == 2
        assert child_runs == []
        if revoke:
            evidence_path.unlink()
        approval = SessionCommand(
            command_id=uuid4().hex,
            session_id="research",
            kind=CommandKind.APPROVE,
            created_at=datetime.now(UTC).isoformat(),
            payload={"target_id": group.group_id},
        )
        gateway.handle(approval)
        assert gateway.wait_for_session_idle(session_id="research", timeout=30)
        projection = gateway.session_projection(session_id="research")
        if revoke:
            assert child_runs == []
            assert all(child.task_id is None for child in projection.subagents)
            return
        assert len(set(child_runs)) == 2
        assert projection.workflow is not None
        review = projection.workflow.research_review
        assert review is not None
        assert review.status == "assessed"
        assert len(review.submissions) == 2
        assert all(child.task_id is not None for child in projection.subagents)
        assert gateway.handle(command).replayed
        assert gateway.handle(approval).replayed
        calls = llm.call_count
        before = _state(gateway)
    finally:
        gateway.stop()
    unused = TestLLM.from_messages([])
    reopened = _sdk_gateway(project, unused, monkeypatch, specialists=True, environment=environment)
    try:
        assert _state(reopened) == before
        assert reopened.handle(command).replayed
        assert unused.call_count == 0
        assert calls == 3  # Native child conversations own their separate model counters.
    finally:
        reopened.stop()


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
    corrected_path = tmp_path / "correction-one/analysis.py"
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


def test_native_workflow_corrects_twice_with_normal_grouped_approvals_and_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline_backend = ReviewBackend()
    baseline = _gateway(tmp_path, baseline_backend)
    try:
        source = _begin_seeded_code_review(baseline, baseline_backend, tmp_path)
        baseline_backend.finish_review()
        command = _projected_command(baseline, "correct").model_copy(
            update={"command_id": "native-correct"}
        )
        original = _state(baseline)
    finally:
        baseline.stop()
    responses: list[Message | Exception] = []
    outputs: list[Path] = []
    for index in (1, 2):
        attempt_id = uuid5(NAMESPACE_URL, json.dumps([command.command_id, "execute", index])).hex
        directory = f"correction-{attempt_id[:12]}-{index}"
        output = tmp_path / directory / "analysis.py"
        outputs.append(output)
        mkdir = _tool_message("terminal", command=f"mkdir {directory}")
        create = _tool_message(
            "file_editor",
            command="create",
            path=str(output),
            file_text="def still_broken(\n" if index == 1 else "print('corrected')\n",
        )
        assert mkdir.tool_calls is not None
        assert create.tool_calls is not None
        responses.extend(
            [
                Message(
                    role="assistant", content=[], tool_calls=[*mkdir.tool_calls, *create.tool_calls]
                ),
                _tool_message(
                    "finish",
                    message="Check the correction.",
                    status="success",
                    outcome_summary="Needs verification.",
                ),
            ]
        )
    llm = TestLLM.from_messages(responses)
    gateway = _sdk_gateway(tmp_path, llm, monkeypatch)
    approvals: list[SessionCommand] = []
    try:
        gateway.handle(command)
        for index in (0, 1):
            assert gateway.wait_for_session_idle(session_id="research", timeout=30)
            projection = gateway.session_projection(session_id="research")
            assert projection.pending_approval is not None
            assert len(projection.pending_approval.actions) == 2
            assert not outputs[index].exists()
            assert source.read_text() == "def broken(\n"
            approval = SessionCommand(
                command_id=f"approve-correction-{index}",
                session_id="research",
                kind=CommandKind.APPROVE,
                created_at="2026-09-11T00:00:00Z",
                payload={"target_id": projection.pending_approval.group_id},
            )
            approvals.append(approval)
            gateway.handle(approval)
        assert gateway.wait_for_session_idle(session_id="research", timeout=30)
        current = _state(gateway)
        series = current.corrections[-1]
        assert series.stop_reason == "corrected"
        assert len(series.attempts) == 2
        assert llm.call_count == 4
        assert current.completed == original.completed
        assert current.stage_usage_baseline == original.stage_usage_baseline
        assert current.stage_started_at == original.stage_started_at
        assert outputs[0].read_text() == "def still_broken(\n"
        assert outputs[1].read_text() == "print('corrected')\n"
        assert source.read_text() == "def broken(\n"
        assert gateway.handle(command).replayed
        assert all(gateway.handle(approval).replayed for approval in approvals)
        export = gateway.export_experiments()
        runs = gateway.experiment_records().runs
        assert [run.status for run in runs] == ["succeeded", "failed", "failed", "succeeded"]
        assert all(
            any(item.kind == "approval.recorded" for item in run.evidence) for run in runs[-2:]
        )
        gateway.handle(
            SessionCommand(
                command_id="correction-audit",
                session_id="research",
                kind=CommandKind.AUDIT_EXPORT,
                created_at="2026-09-11T00:00:00Z",
            )
        )
        audit = (gateway.sessions_root / "research/audit-export.jsonl").read_text()
        assert "research_correction_fingerprint" in audit
        assert "analysis.py" not in audit
        assert "Untrusted proposed diagnosis" not in audit
    finally:
        gateway.stop()
    unused = TestLLM.from_messages([])
    restored = _sdk_gateway(tmp_path, unused, monkeypatch)
    try:
        assert _state(restored) == current
        assert restored.export_experiments() == export
        assert restored.handle(command).replayed
        assert all(restored.handle(approval).replayed for approval in approvals)
        assert unused.call_count == 0
    finally:
        restored.stop()


@pytest.mark.parametrize("case", ["statistical", "reproduction", "copied-results"])
def test_native_correction_rechecks_analysis_and_requires_recorded_reproduction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    from heartwood.core_adapter.research_workflows import workflow_reproduction_spec
    from heartwood.core_adapter.workflow_corrections import correction_artifact_binding

    monkeypatch.setenv("PATH", f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}")
    backend = ReviewBackend()
    baseline = _gateway(tmp_path, backend)
    try:
        _begin_baseline_execution(baseline, tmp_path)
        source = (
            Path(__file__).parents[2] / "compliance/tests/fixtures/research/reference_analysis.py"
        )
        (tmp_path / "results/analysis.py").write_text(source.read_text())
        subprocess.run(
            [
                sys.executable,
                "results/analysis.py",
                "--data",
                "data.csv",
                "--output-dir",
                "results",
            ],
            cwd=tmp_path,
            check=True,
            timeout=30,
        )
        if case == "statistical":
            metrics = json.loads((tmp_path / "results/metrics.json").read_text())
            metrics["test_rmse"] += 5
            (tmp_path / "results/metrics.json").write_text(json.dumps(metrics))
            condition, category = "baseline-result-inconsistent", "statistical"
            affected = ["metrics", "predictions"]
        else:
            baseline.handle(_projected_command(baseline, "evaluate"))
            assert _state(baseline).stage_id == "verify"
            baseline.handle(_projected_command(baseline, "run"))
            (tmp_path / "results/reproduced").mkdir()
            (tmp_path / "results/reproduced/metrics.json").write_text("{}")
            (tmp_path / "results/reproduced/predictions.csv").write_text(
                (tmp_path / "results/predictions.csv").read_text()
            )
            (tmp_path / "results/verification.json").write_text(
                json.dumps(
                    {
                        "status": "discrepancy",
                        "matching_artifacts": ["predictions.csv"],
                        "mismatched_artifacts": ["metrics.json"],
                    }
                )
            )
            condition, category = "reproduction-artifact-mismatch", "reproducibility"
            affected = ["metrics", "predictions", "reproduced-metrics", "reproduced-predictions"]
        backend.proposals = ReviewProposals.model_validate(
            {
                "candidates": [
                    {
                        "candidate_id": "defect",
                        "condition": condition,
                        "category": category,
                        "severity": "high",
                        "summary": "Synthetic seeded defect",
                        "artifact_ids": affected,
                    }
                ]
            }
        )
        baseline.handle(_projected_command(baseline, "request-review"))
        backend.finish_review()
        original = _state(baseline)
        review = original.research_review
        assert review is not None
        assert review.assessment is not None
        assert review.assessment.findings[0].verification == "verified"
        command = _projected_command(baseline, "correct").model_copy(
            update={"command_id": "native-analysis-correction"}
        )
        attempt_id = uuid5(
            NAMESPACE_URL, json.dumps([command.command_id, original.stage_id, 1])
        ).hex
        directory = f"correction-{attempt_id[:12]}-1"
        plan = baseline.prepare_research_correction(review, output_directory=directory)
        preserved = {
            item.file.path: (tmp_path / item.file.path).read_bytes()
            for item in review.snapshot.artifacts
        }
    finally:
        baseline.stop()

    responses: list[Message | Exception] = []
    if case == "statistical":
        responses.append(
            _tool_message(
                "terminal",
                command=f"python results/analysis.py --data data.csv --output-dir {directory}",
            )
        )
    else:
        spec = workflow_reproduction_spec(
            correction_artifact_binding(original.binding, plan), "verify"
        )
        assert spec is not None
        invocation = spec.command
        if case == "copied-results":
            invocation = (
                f"mkdir -p {spec.directory} && cp results/metrics.json "
                f"results/predictions.csv {spec.directory}/"
            )
        else:
            responses.append(_tool_message("terminal", command=f"mkdir {directory}"))
        responses.extend(
            [
                _tool_message("terminal", command=invocation),
                _tool_message(
                    "file_editor",
                    command="create",
                    path=str(tmp_path / directory / "verification.json"),
                    file_text=json.dumps(
                        {
                            "status": "reproduced",
                            "matching_artifacts": ["metrics.json", "predictions.csv"],
                            "mismatched_artifacts": [],
                        }
                    ),
                ),
            ]
        )
    responses.append(
        _tool_message(
            "finish",
            message="Correction complete; verify independently.",
            status="success",
            outcome_summary="Preserved source evidence.",
        )
    )
    llm = TestLLM.from_messages(responses)
    gateway = _sdk_gateway(tmp_path, llm, monkeypatch)
    try:
        gateway.handle(command)
        for index in range(len(responses) - 1):
            assert gateway.wait_for_session_idle(session_id="research", timeout=30)
            group = gateway.session_projection(session_id="research").pending_approval
            assert group is not None
            assert len(group.actions) == 1
            if index == 0:
                assert not (tmp_path / directory).exists()
            approval = SessionCommand(
                command_id=f"approve-analysis-correction-{index}",
                session_id="research",
                kind=CommandKind.APPROVE,
                created_at="2026-09-11T00:00:00Z",
                payload={"target_id": group.group_id},
            )
            gateway.handle(approval)
            assert gateway.handle(approval).replayed
        assert gateway.wait_for_session_idle(session_id="research", timeout=30)
        corrected = _state(gateway)
        assert corrected.corrections[-1].stop_reason == "corrected"
        assert corrected.completed == original.completed
        assert {path: (tmp_path / path).read_bytes() for path in preserved} == preserved
        gateway.handle(_projected_command(gateway, "evaluate"))
        final = _state(gateway)
        if case == "copied-results":
            assert final.phase == "blocked"
            assert final.stage_id == "verify"
            assert final.evaluation is not None
            assert final.evaluation.checks[0].status == "not_run"
        else:
            assert final.phase == "ready"
            assert final.stage_id == ("verify" if case == "statistical" else "report")
        assert llm.call_count == len(responses)
        assert gateway.handle(command).replayed
        export = gateway.export_experiments()
        runs = gateway.experiment_records().runs
        assert runs[-2].status == "failed"
        assert runs[-1].status == "succeeded"  # Narrow correction outcome, not stage acceptance.
        if case == "reproduction":
            assert any(item.kind == "workflow.execution.recorded" for item in runs[-1].evidence)
        gateway.handle(
            SessionCommand(
                command_id="analysis-correction-audit",
                session_id="research",
                kind=CommandKind.AUDIT_EXPORT,
                created_at="2026-09-11T00:00:00Z",
            )
        )
        audit = (gateway.sessions_root / "research/audit-export.jsonl").read_text()
        assert "research_correction_fingerprint" in audit
        assert "metrics.json" not in audit
    finally:
        gateway.stop()
    unused = TestLLM.from_messages([])
    restored = _sdk_gateway(tmp_path, unused, monkeypatch)
    try:
        assert _state(restored) == final
        assert restored.export_experiments() == export
        assert restored.handle(command).replayed
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


@pytest.mark.parametrize("offset", [0, 1, 50])
def test_observed_stage_budget_blocks_more_work_but_allows_exact_completion(
    tmp_path: Path,
    offset: int,
) -> None:
    backend = FinishedBackend()
    backend.model_calls = (
        research_workflow("dataset-readiness").stage("inspect").budget.maximum_model_calls + offset
    )
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


@pytest.mark.parametrize(("offset", "phase"), [(0, "ready"), (1, "running")])
def test_budget_completion_boundary(tmp_path: Path, offset: int, phase: str) -> None:
    backend = FinishedBackend()
    backend.model_calls = (
        research_workflow("dataset-readiness").stage("inspect").budget.maximum_model_calls + offset
    )
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


def _parallel_preparer(root: Path, seed: ParallelReviewPlan) -> ParallelReviewPreparer:
    """Synthetic qualified evidence exercises admission, not real-provider qualification."""

    def prepare(
        run: WorkflowRun, snapshot: ReviewSnapshot, session_id: str, now: datetime
    ) -> ParallelReviewPlan:
        assert now.tzinfo is not None
        stage = research_workflow(run.binding.workflow_id).stage(run.stage_id)
        return ParallelReviewPlan.model_validate(
            {
                **seed.model_dump(),
                "scope": {
                    **seed.scope.model_dump(),
                    "project_fingerprint": hashlib.sha256(str(root).encode()).hexdigest(),
                    "session_id": session_id,
                    "workflow_run_id": run.run_id,
                    "workflow_id": run.binding.workflow_id,
                    "stage_id": run.stage_id,
                    "revision": run.revision,
                    "snapshot_fingerprint": snapshot.fingerprint,
                    "reviewer_ids": stage.specialist_ids,
                    "budget": stage.budget,
                },
            }
        )

    return prepare


def _review_actions(plan: ReviewExecutionPlan) -> tuple[ReviewDispatchAction, ...]:
    return tuple(
        ReviewDispatchAction(
            event_id=f"native-{index}",
            tool_call_id=f"call-{index}",
            reviewer_id=role,
            action_fingerprint=str(index + 1) * 64,
        )
        for index, role in enumerate(plan.scope.reviewer_ids)
    )


def test_parallel_preview_is_read_only_and_exact_consent_is_journaled(
    tmp_path: Path,
    parallel_review_plan: ParallelReviewPlan,
) -> None:
    backend = ReviewBackend()
    preparer = _parallel_preparer(tmp_path, parallel_review_plan)
    gateway = _gateway(tmp_path, backend, parallel_review_preparer=preparer)
    try:
        _begin_baseline_plan(gateway, tmp_path)
        preview_command = _projected_command(gateway, "prepare-parallel-review")
        preview_result = gateway.handle(preview_command)
        preview = _state(gateway).parallel_review_plan
        assert preview is not None
        from heartwood.cli._interactive import format_workflow_lines
        from heartwood.notebook import build_view_model, build_widget_spec

        projection = gateway.session_projection(session_id="research")
        assert projection.review_execution is not None
        assert projection.review_execution.status == "preview"
        summary = projection.review_execution.summary
        assert summary in format_workflow_lines(projection)
        assert any(
            summary in widget.items for widget in build_widget_spec(build_view_model(projection))
        )
        assert len(backend.prompts) == 1
        assert _state(gateway).research_review is None
        assert gateway.handle(preview_command).replayed
        assert gateway.handle(preview_command).events == preview_result.events
        request = _projected_command(gateway, "request-parallel-review")
        assert request.payload["parallel_review_fingerprint"] == preview.fingerprint
        gateway.handle(request)
        current = _state(gateway)
        assert current.parallel_review_plan is None
        assert current.research_review is not None
        assert current.research_review.parallel_plan == preview
        assert current.research_review.parallel_dispatch == ()
        projected = gateway.session_projection(session_id="research").review_execution
        assert projected is not None
        assert projected.status == "authorized"
        assert len(backend.prompts) == 2
        service = gateway._services["research"]
        assert (
            service.admit_parallel_review(_review_actions(preview), cancelled=lambda: False)
            == preview
        )
        admitted = _state(gateway).research_review
        assert admitted is not None
        assert len(admitted.parallel_dispatch) == 2
        projected = gateway.session_projection(session_id="research").review_execution
        assert projected is not None
        assert projected.status == "admitted"
        with pytest.raises(ValueError, match="already admitted"):
            service.admit_parallel_review(_review_actions(preview), cancelled=lambda: False)
        assert gateway.handle(request).replayed
        assert len(backend.prompts) == 2
    finally:
        gateway.stop()
    fresh = ReviewBackend()
    restored = _gateway(tmp_path, fresh, parallel_review_preparer=preparer)
    try:
        assert restored.handle(preview_command).replayed
        assert restored.handle(request).replayed
        assert _state(restored).research_review == admitted
        assert not fresh.prompts
    finally:
        restored.stop()


@pytest.mark.parametrize("change", ["fingerprint", "files", "revision", "evidence", "expiry"])
def test_parallel_consent_changes_never_start_model_work(
    tmp_path: Path,
    parallel_review_plan: ParallelReviewPlan,
    change: str,
) -> None:
    backend = ReviewBackend()
    preparer = _parallel_preparer(tmp_path, parallel_review_plan)
    gateway = _gateway(tmp_path, backend, parallel_review_preparer=preparer)
    try:
        _begin_baseline_plan(gateway, tmp_path)
        gateway.handle(_projected_command(gateway, "prepare-parallel-review"))
        request = _projected_command(gateway, "request-parallel-review")
        if change == "fingerprint":
            request = request.model_copy(
                update={
                    "payload": {
                        **request.payload,
                        "parallel_review_fingerprint": "f" * 64,
                    }
                }
            )
        elif change == "files":
            (tmp_path / "results/plan.json").write_text("{}")
        elif change == "revision":
            gateway.handle(_projected_command(gateway, "evaluate"))
        else:
            altered = parallel_review_plan.model_copy(
                update=(
                    {"case_id": "changed-evidence"}
                    if change == "evidence"
                    else {"valid_until": datetime.fromisoformat("2020-01-01T00:00:00+00:00")}
                )
            )
            gateway._services["research"]._workflow_evaluator = ResearchStageEvaluator(
                gateway.workspace_inspector,
                parallel_review_preparer=_parallel_preparer(tmp_path, altered),
            )
        response = gateway.handle(request)
        assert any(event.kind == EventKind.ERROR_RECORDED for event in response.events)
        assert _state(gateway).research_review is None
        assert len(backend.prompts) == 1
    finally:
        gateway.stop()


@pytest.mark.parametrize("change", ["cancel", "close", "files", "scope", "duplicate"])
def test_parallel_admission_rechecks_after_slow_preparation(
    tmp_path: Path,
    parallel_review_plan: ParallelReviewPlan,
    change: str,
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    entered, release, cancelled = Event(), Event(), Event()
    backend = ReviewBackend()
    preparer = _parallel_preparer(tmp_path, parallel_review_plan)
    gateway = _gateway(tmp_path, backend, parallel_review_preparer=preparer)
    try:
        _begin_baseline_plan(gateway, tmp_path)
        gateway.handle(_projected_command(gateway, "prepare-parallel-review"))
        preview = _state(gateway).parallel_review_plan
        assert preview is not None
        gateway.handle(_projected_command(gateway, "request-parallel-review"))
        service = gateway._services["research"]

        def wait_prepare(
            run: WorkflowRun, snapshot: ReviewSnapshot, session_id: str, now: datetime
        ) -> ReviewExecutionPlan:
            entered.set()
            assert release.wait(5)
            return preparer(run, snapshot, session_id, now)

        service._workflow_evaluator = ResearchStageEvaluator(
            gateway.workspace_inspector,
            parallel_review_preparer=wait_prepare,
        )
        actions = _review_actions(preview)
        if change in {"scope", "duplicate"}:
            actions = (
                actions[0],
                actions[1].model_copy(
                    update={
                        "reviewer_id": "unapproved"
                        if change == "scope"
                        else actions[0].reviewer_id,
                    }
                ),
            )
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(service.admit_parallel_review, actions, cancelled=cancelled.is_set)
            try:
                assert entered.wait(5)
                if change == "cancel":
                    cancelled.set()
                elif change == "close":
                    service.close()
                elif change == "files":
                    (tmp_path / "results/plan.json").write_text("{}")
            finally:
                release.set()
            with pytest.raises(ValueError, match=r"Parallel (review|dispatch)"):
                future.result(timeout=5)
        review = _state(gateway).research_review
        assert review is not None
        assert review.parallel_dispatch == ()
        if change == "close":
            assert not service.store.owns_writer
    finally:
        gateway.stop()


def test_parallel_review_enforces_the_narrower_consented_stage_budget(
    tmp_path: Path, parallel_review_plan: ParallelReviewPlan
) -> None:
    from heartwood.schemas.execution import ExecutionBudget

    backend = ReviewBackend()
    prepare = _parallel_preparer(tmp_path, parallel_review_plan)

    def limited(
        run: WorkflowRun, snapshot: ReviewSnapshot, session_id: str, now: datetime
    ) -> ReviewExecutionPlan:
        plan = prepare(run, snapshot, session_id, now)
        return plan.model_copy(
            update={
                "scope": plan.scope.model_copy(
                    update={"budget": ExecutionBudget(maximum_model_calls=1)}
                )
            }
        )

    gateway = _gateway(tmp_path, backend, parallel_review_preparer=limited)
    try:
        _begin_baseline_plan(gateway, tmp_path)
        gateway.handle(_projected_command(gateway, "prepare-parallel-review"))
        preview = _state(gateway).parallel_review_plan
        assert preview is not None
        backend.model_calls = 1
        gateway.handle(_projected_command(gateway, "request-parallel-review"))
        with pytest.raises(ValueError, match="budget reached"):
            gateway._services["research"].admit_parallel_review(
                _review_actions(preview), cancelled=lambda: False
            )
        review = _state(gateway).research_review
        assert review is not None
        assert review.parallel_dispatch == ()
    finally:
        gateway.stop()


def _reserve_parallel_trial(
    gateway: SessionGateway, task: ResearchTask | None = None, *, workers: int = 2
) -> ReservedReviewTrial:
    from heartwood.compliance.evaluation_store import EvaluationStore
    from heartwood.model_policy.parallel_reviews import PARALLEL_REVIEW_CHECKS
    from heartwood.schemas.evaluation import (
        EvaluationCase,
        EvaluationCheck,
        EvaluationConfiguration,
        EvaluationDimension,
        EvaluationRun,
        EvaluationSuite,
        RequiredEvaluationCheck,
    )
    from heartwood.schemas.execution import ExecutionUsage

    current = _state(gateway)
    stage = research_workflow(current.binding.workflow_id).stage(current.stage_id)
    observe = gateway.bind_evaluation_observer(session_id="research")
    runtime = observe("research")
    checks = {dimension.value: dimension for dimension in EvaluationDimension}
    checks.update(PARALLEL_REVIEW_CHECKS)
    suite = EvaluationSuite(
        suite_id="synthetic-native-review",
        cases=(
            EvaluationCase(
                case_id="baseline-review",
                workflow_id=current.binding.workflow_id,
                review_stage_id=current.stage_id,
                fixture_digest=review_digest(current.binding.model_dump(mode="json")),
                specialist_ids=stage.specialist_ids,
                required_checks=tuple(
                    RequiredEvaluationCheck(check_id=key, dimension=value)
                    for key, value in checks.items()
                ),
            ),
        ),
    )
    if task is not None:
        suite = planning_review_suite()
        assert task.case in suite.cases
    case = suite.cases[0] if task is None else task.case
    configuration = EvaluationConfiguration(
        provider="synthetic",
        model=runtime.request_model or "unknown",
        request_model=runtime.request_model or "unknown",
        model_revision=None,
        platform="generic",
        hardware=("test",),
        runtime="test",
        openhands_version=runtime.openhands_version or "unknown",
        precision="test",
        context_tokens=runtime.max_input_tokens or 32768,
        output_tokens=runtime.max_output_tokens or 4096,
        tool_parser="native",
        skill_tree_digest="a" * 64,
        harness_revision="b" * 64,
        runtime_fingerprint=runtime.fingerprint,
        specialist_concurrency=workers,
        specialist_catalog_fingerprint=runtime.specialist_catalog_fingerprint,
    )
    if task is not None:
        return reserve_planning_review_trial(
            gateway,
            task,
            session_id="research",
            configuration=configuration,
            execution="deterministic",
            budget=stage.budget,
        )
    now = datetime.fromisoformat(gateway._services["research"].clock())
    trial = EvaluationRun(
        run_id=uuid4(),
        suite_id=suite.suite_id,
        suite_fingerprint=suite.fingerprint,
        case_id=case.case_id,
        fixture_digest=case.fixture_digest,
        seed=0,
        execution="deterministic",
        configuration=configuration,
        runtime_observation=runtime,
        started_at=now,
        finished_at=now,
        budget=stage.budget,
        status="incomplete",
        session_id="research",
        checks=tuple(
            EvaluationCheck(**check.model_dump(), status="not_run")
            for check in case.required_checks
        ),
        usage=ExecutionUsage(elapsed_seconds=0),
    )
    store = EvaluationStore(gateway.project.state_root / "evaluations")
    store.begin(trial)
    return ReservedReviewTrial(gateway.project, store, trial.run_id, suite, observe)


@pytest.mark.parametrize("damage", ["completed", "missing", "corrupt", "runtime"])
def test_reserved_trial_is_rechecked_before_model_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    source: ReservedReviewTrial | None = None

    def prepare(
        run: WorkflowRun, snapshot: ReviewSnapshot, session_id: str, now: datetime
    ) -> ReviewExecutionPlan:
        assert source is not None
        return source(run, snapshot, session_id, now)

    llm = TestLLM.from_messages(
        [
            _tool_message(
                "finish", message="Plan prepared.", status="success", outcome_summary="Done."
            )
        ]
    )
    gateway = _sdk_gateway(
        tmp_path, llm, monkeypatch, specialists=True, parallel_review_preparer=prepare
    )
    try:
        _begin_baseline_plan(gateway, tmp_path)
        assert gateway.wait_for_session_idle(session_id="research", timeout=30)
        source = _reserve_parallel_trial(gateway)
        gateway.handle(_projected_command(gateway, "prepare-parallel-review"))
        command = _projected_command(gateway, "request-parallel-review")
        record_path = source.store.root / f"{source.trial_id}.json"
        if damage == "completed":
            (record,) = source.store.records()
            source.store.complete(record.model_copy(update={"status": "completed"}))
        elif damage == "missing":
            record_path.unlink()
        elif damage == "corrupt":
            record_path.write_text("{")
        else:
            llm.max_output_tokens = 1234
        response = gateway.handle(command)
        assert any(event.kind == EventKind.ERROR_RECORDED for event in response.events)
        assert _state(gateway).research_review is None
        assert llm.call_count == 1
    finally:
        gateway.stop()


@pytest.mark.parametrize("approve", [False, True])
@pytest.mark.parametrize("mode", ["sequential", "qualified", "experimental", "retained-sequential"])
@pytest.mark.parametrize("case_id", ["plan-valid", "plan-outcome-leakage"])
def test_native_review_comparison_preserves_findings_consent_and_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    parallel_review_plan: ParallelReviewPlan,
    approve: bool,
    mode: str,
    case_id: str,
) -> None:
    from threading import Barrier, Lock

    from openhands.sdk import LocalConversation
    from openhands.tools.task.manager import TaskManager

    from heartwood.core_adapter.workflow_runtime import workflow_run

    roles = ("research-planner", "statistical-reviewer")
    workers = 1 if mode in ("sequential", "retained-sequential") else 2
    task = next(task for task in planning_review_tasks() if task.case.case_id == case_id)
    candidates = (
        [
            {
                "candidate_id": "plan-claim",
                "condition": "analysis-plan-incompatible",
                "category": "statistical",
                "severity": "high",
                "summary": "The plan uses an outcome-derived predictor.",
                "artifact_ids": ["plan"],
            }
        ]
        if case_id == "plan-outcome-leakage"
        else []
    )
    task_messages = [
        _tool_message(
            "task",
            description="Review synthetic analysis plan",
            prompt=task.instruction,
            subagent_type=role,
        )
        for role in roles
    ]
    llm = TestLLM.from_messages(
        [
            _tool_message(
                "finish", message="Plan prepared.", status="success", outcome_summary="Done."
            ),
            Message(
                role="assistant",
                content=[],
                tool_calls=[
                    call for message in task_messages for call in (message.tool_calls or [])
                ],
            ),
            _tool_message("finish", message="First review.", candidates=candidates),
            _tool_message("finish", message="Second review.", candidates=candidates),
            _tool_message(
                "finish", message="Reviews settled.", status="success", outcome_summary="Done."
            ),
        ]
    )
    qualified_preparer = _parallel_preparer(tmp_path, parallel_review_plan)
    reservation: ReservedReviewTrial | None = None

    def preparer(
        run: WorkflowRun, snapshot: ReviewSnapshot, session_id: str, now: datetime
    ) -> ReviewExecutionPlan:
        if mode == "experimental":
            assert reservation is not None
            return reservation(run, snapshot, session_id, now)
        return qualified_preparer(run, snapshot, session_id, now)

    gateway = _sdk_gateway(
        tmp_path,
        llm,
        monkeypatch,
        specialists=True,
        parallel_review_preparer=preparer,
    )
    barrier, lock = Barrier(workers), Lock()
    children = 0
    child_calls: list[int] = []
    native_run = TaskManager._run_until_finished

    def run_child(manager: TaskManager, task_id: str, conversation: LocalConversation) -> None:
        nonlocal children
        current = workflow_run(gateway._services["research"].replay_events())
        assert current is not None
        assert current.research_review is not None
        assert len(current.research_review.parallel_dispatch) == (2 if workers == 2 else 0)
        with lock:
            children += 1
        barrier.wait(timeout=5)
        assert isinstance(conversation.agent.llm, TestLLM)
        before = conversation.agent.llm.call_count
        native_run(manager, task_id, conversation)
        with lock:
            child_calls.append(conversation.agent.llm.call_count - before)

    monkeypatch.setattr(TaskManager, "_run_until_finished", run_child)
    try:
        _begin_baseline_plan(
            gateway, tmp_path, question=json.loads(task.inputs["plan.json"])["question"]
        )
        assert gateway.wait_for_session_idle(session_id="research", timeout=30)
        (tmp_path / "results/plan.json").write_text(task.inputs["plan.json"])
        if mode in ("experimental", "retained-sequential"):
            from heartwood.gateway import ProjectionApprovalGroup

            reservation = _reserve_parallel_trial(gateway, task, workers=workers)

            def decide(group: ProjectionApprovalGroup) -> Literal["approve", "reject"]:
                assert len(group.actions) == 2
                assert all(action.details.kind == "task" for action in group.actions)
                assert children == 0
                return "approve" if approve else "reject"

            trial = run_planning_review_trial(gateway, reservation, review=decide)
            assert trial.record == reservation.record()
            assert trial.record.status == "completed"
            # TestLLM does not report provider accounting; unknown is not zero.
            assert trial.record.usage.model_calls is None
            assert trial.record.usage.proposed_actions == 2
            checks = {check.check_id: check.status for check in trial.record.checks}
            if approve:
                assert set(checks.values()) == {"passed"}, checks
            else:
                assert trial.stop == "rejected"
                assert checks["review.findings"] == "failed"
                assert checks["review.schedule"] == "failed"
            with pytest.raises(ValueError, match="incomplete reservation"):
                run_planning_review_trial(gateway, reservation, review=decide)
        else:
            if workers == 2:
                gateway.handle(_projected_command(gateway, "prepare-parallel-review"))
                preview = _state(gateway).parallel_review_plan
                assert preview is not None
                assert preview.purpose == "qualified-review"
            request = _projected_command(
                gateway, "request-parallel-review" if workers == 2 else "request-review"
            )
            gateway.handle(request)
            assert gateway.wait_for_session_idle(session_id="research", timeout=30)
            projection = gateway.session_projection(session_id="research")
            assert projection.pending_approval is not None
            assert len(projection.pending_approval.actions) == 2
            assert children == 0
            decision = SessionCommand(
                command_id=uuid4().hex,
                session_id="research",
                kind=CommandKind.APPROVE if approve else CommandKind.DENY,
                created_at="2026-09-11T00:00:00Z",
                payload={"target_id": projection.pending_approval.group_id},
            )
            gateway.handle(decision)
        assert gateway.wait_for_session_idle(session_id="research", timeout=30)
        review = _state(gateway).research_review
        assert review is not None
        assert children == (2 if approve else 0)
        assert len(review.parallel_dispatch) == (2 if approve and workers == 2 else 0)
        observed_tasks = gateway.session_projection(session_id="research").subagents
        intervals = [item.native_execution for item in observed_tasks if item.native_execution]
        assert len(intervals) == (2 if approve else 0)
        if approve:
            assert (intervals[0].overlap_seconds(intervals[1]) > 0) == (workers == 2)
            assert review.status == "assessed"
            assert verify_planning_review(task, review).status == "passed"
            assert {item.reviewer_id for item in review.submissions} == set(roles)
            projected = gateway.session_projection(session_id="research").review_execution
            if workers == 2:
                assert projected is not None
                assert projected.status == "assessed"
            else:
                assert projected is None
                workflow = gateway.session_projection(session_id="research").workflow
                assert workflow is not None
                assert workflow.research_review == review
        assert llm.call_count == (3 if approve else 2)
        assert child_calls == ([1, 1] if approve else [])
        assert llm.remaining_responses == (0 if approve else 3)
        gateway.handle(
            SessionCommand(
                command_id="parallel-export",
                session_id="research",
                kind=CommandKind.AUDIT_EXPORT,
                created_at="2026-09-11T00:00:00Z",
            )
        )
        audit = (gateway.sessions_root / "research/audit-export.jsonl").read_text()
        assert "research_review_fingerprint" in audit
        assert "action_fingerprint" not in audit
        assert "results/plan.json" not in audit
    finally:
        gateway.stop()
    unused = TestLLM.from_messages([])
    restored = _sdk_gateway(
        tmp_path,
        unused,
        monkeypatch,
        specialists=True,
        parallel_review_preparer=preparer,
    )
    try:
        if mode not in ("experimental", "retained-sequential"):
            assert restored.handle(request).replayed
            assert restored.handle(decision).replayed
        assert _state(restored).research_review == review
        if approve:
            assert verify_planning_review(task, review).status == "passed"
        assert restored.session_projection(session_id="research").subagents == observed_tasks
        assert unused.call_count == 0
    finally:
        restored.stop()


@pytest.mark.parametrize(
    "boundary", ["intent", "audit-before", "audit-after", "events-before", "events-after"]
)
def test_parallel_admission_recovers_append_boundaries_without_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    parallel_review_plan: ParallelReviewPlan,
    boundary: str,
) -> None:
    from heartwood.core_adapter import _state as state_module

    backend = ReviewBackend()
    preparer = _parallel_preparer(tmp_path, parallel_review_plan)
    gateway = _gateway(tmp_path, backend, parallel_review_preparer=preparer)
    try:
        _begin_baseline_plan(gateway, tmp_path)
        gateway.handle(_projected_command(gateway, "prepare-parallel-review"))
        preview = _state(gateway).parallel_review_plan
        assert preview is not None
        gateway.handle(_projected_command(gateway, "request-parallel-review"))
        service = gateway._services["research"]
        store = service.store
        append = state_module._append_private_json_line
        write = state_module._write_private_json_atomic

        def interrupt_append(path: Path, text: str) -> None:
            payload = json.loads(text)
            target = store.audit_path if boundary.startswith("audit") else store.events_path
            if (
                path != target
                or payload.get("payload", {}).get("transition") != "admit-parallel-review"
            ):
                append(path, text)
                return
            if boundary.endswith("after"):
                append(path, text)
            raise OSError("Synthetic admission interruption")

        def interrupt_intent(path: Path, value: dict[str, object]) -> None:
            event = value.get("session_event")
            if (
                isinstance(event, dict)
                and event.get("payload", {}).get("transition") == "admit-parallel-review"
            ):
                raise OSError("Synthetic admission interruption")
            write(path, value)

        monkeypatch.setattr(state_module, "_append_private_json_line", interrupt_append)
        if boundary == "intent":
            monkeypatch.setattr(state_module, "_write_private_json_atomic", interrupt_intent)
        with pytest.raises(OSError, match="Synthetic admission"):
            service.admit_parallel_review(_review_actions(preview), cancelled=lambda: False)
        monkeypatch.undo()
        assert len(backend.prompts) == 2
    finally:
        gateway.stop()
    unused = ReviewBackend()
    restored = _gateway(tmp_path, unused, parallel_review_preparer=preparer)
    try:
        service = restored._service("research")
        service.reconcile()
        events = service.replay_events()
        assert sum(
            event.payload.get("transition") == "admit-parallel-review" for event in events
        ) == (0 if boundary == "intent" else 1)
        if boundary != "intent":
            with pytest.raises(ValueError, match="already admitted"):
                service.admit_parallel_review(_review_actions(preview), cancelled=lambda: False)
        assert unused.prompts == []
    finally:
        restored.stop()


@pytest.mark.parametrize(
    "failure",
    [
        "changed-input",
        "extra-file",
        "symlink-plan",
        "changed-after",
        "observer-changed",
        "stop",
        "callback-error",
    ],
)
def test_retained_review_rejects_substitution_and_retains_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    from dataclasses import replace

    from heartwood.gateway import ProjectionApprovalGroup

    task = planning_review_tasks()[0]
    llm = TestLLM.from_messages(
        [
            _tool_message("finish", message="Plan ready", status="success", outcome_summary="Done"),
            _tool_message(
                "task",
                description="Review plan",
                prompt=task.instruction,
                subagent_type="research-planner",
            ),
        ]
    )
    gateway = _sdk_gateway(tmp_path, llm, monkeypatch, specialists=True)
    try:
        _begin_baseline_plan(
            gateway, tmp_path, question=json.loads(task.inputs["plan.json"])["question"]
        )
        assert gateway.wait_for_session_idle(session_id="research", timeout=30)
        plan = tmp_path / "results/plan.json"
        plan.write_text(task.inputs["plan.json"])
        if failure in ("changed-input", "extra-file", "symlink-plan"):
            if failure == "changed-input":
                (tmp_path / "data.csv").write_text("substituted\n")
            elif failure == "extra-file":
                (tmp_path / "unexpected.txt").write_text("synthetic unrelated file\n")
            else:
                plan.unlink()
                plan.symlink_to(tmp_path / "data.csv")
            with pytest.raises(ValueError, match="exact synthetic fixture"):
                _reserve_parallel_trial(gateway, task, workers=1)
            assert not (gateway.project.state_root / "evaluations").exists()
            assert llm.call_count == 1
            return
        reservation = _reserve_parallel_trial(gateway, task, workers=1)
        if failure == "changed-after":
            plan.write_text("{}\n")
        elif failure == "observer-changed":
            runtime = reservation.observe_runtime("research")
            reservation = replace(
                reservation,
                observe_runtime=lambda _: runtime.model_copy(
                    update={"policy_fingerprint": "f" * 64}
                ),
            )

        def decide(group: ProjectionApprovalGroup) -> Literal["stop"]:
            assert group.actions
            if failure == "callback-error":
                raise RuntimeError("Synthetic reviewer interruption")
            return "stop"

        if failure in ("changed-after", "observer-changed"):
            with pytest.raises(ValueError, match=r"exact synthetic fixture|runtime changed"):
                run_planning_review_trial(gateway, reservation, review=decide)
            assert llm.call_count == 1
            assert reservation.record().status == "incomplete"
        elif failure == "callback-error":
            with pytest.raises(RuntimeError, match="reviewer interruption"):
                run_planning_review_trial(gateway, reservation, review=decide)
            assert reservation.record().status == "incomplete"
            assert gateway.wait_for_session_idle(session_id="research")
            assert gateway.session_projection(session_id="research").pending_approval is not None
        else:
            result = run_planning_review_trial(gateway, reservation, review=decide)
            assert result.stop == "review-stopped"
            assert result.record.status == "completed"
            assert any(check.status == "failed" for check in result.record.checks)
        assert all(
            item.task_id is None
            for item in gateway.session_projection(session_id="research").subagents
        )
    finally:
        gateway.stop()


def test_workflow_start_requires_an_unused_output_directory(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path, FinishedBackend())
    try:
        inputs = _inputs(tmp_path)
        (tmp_path / "results").mkdir()
        (tmp_path / "results/readiness.json").write_text("{}")
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
    finally:
        gateway.stop()
