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
    BackendLifecycle,
    BackendLifecycleEvent,
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
    finally:
        gateway.stop()
    replacement = FinishedBackend()
    fresh = _gateway(tmp_path, replacement)
    try:
        with pytest.raises(SessionRecoveryError):
            fresh.handle(command)
        assert not replacement.prompts
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


def _sdk_gateway(root: Path, llm: TestLLM, monkeypatch: pytest.MonkeyPatch) -> SessionGateway:
    monkeypatch.setattr(sdk_module, "LLM", lambda **_options: llm)

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
        assert (tmp_path / "results/readiness.md").read_text() == report
    finally:
        restored.stop()


@pytest.mark.parametrize("mutation", [None, "input", "program", "destination"])
def test_real_sdk_baseline_reproduces_through_journaled_actions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str | None
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
