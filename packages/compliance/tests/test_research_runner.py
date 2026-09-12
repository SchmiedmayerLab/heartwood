# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Real gateway, SDK, tool execution, review, independent rerun, and fresh replay."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event as ThreadEvent
from types import SimpleNamespace

import pytest
from openhands.sdk import LLMStreamChunk, LocalConversation, Tool
from openhands.sdk.conversation import BaseConversation
from openhands.sdk.event import Event
from openhands.sdk.llm import LLMResponse, Message, MessageToolCall, TextContent
from openhands.sdk.llm.llm import LLMCallContext
from openhands.sdk.llm.streaming import TokenCallbackType
from openhands.sdk.security import AlwaysConfirm
from openhands.sdk.settings import OpenHandsAgentSettings
from openhands.sdk.testing import TestLLM
from openhands.sdk.tool import Action, Observation, ToolDefinition
from openhands.tools import TerminalTool

from heartwood.compliance.evaluation_store import EvaluationStore
from heartwood.compliance.research import ResearchTask, research_tasks
from heartwood.compliance.research_runner import ReviewDecision, _rerun_spec, run_research_trial
from heartwood.core_adapter import SessionService
from heartwood.gateway import (
    ModelProfile,
    OpenHandsSdkBackend,
    ProjectContext,
    ProjectionActionRecord,
    ProjectionApprovalGroup,
    SessionGateway,
)
from heartwood.gateway._project_file_editor import PROJECT_FILE_EDITOR_SPEC
from heartwood.schemas.evaluation import EvaluationConfiguration
from heartwood.schemas.execution import ExecutionBudget


@pytest.mark.parametrize(
    ("is_input", "reset", "eligible"),
    [(False, False, True), (True, False, False), (False, True, False)],
)
def test_reproduction_requires_a_normal_terminal_invocation(
    is_input: bool, reset: bool, eligible: bool
) -> None:
    action = ProjectionActionRecord.model_validate(
        {
            "tool_call_id": "reviewed-reproduction",
            "tool_name": "terminal",
            "risk": "unknown",
            "summary": "Synthetic rerun",
            "details": {
                "kind": "terminal",
                "command": "python analysis.py --data data.csv --output-dir reproduced",
                "is_input": is_input,
                "reset": reset,
            },
            "state": "awaiting-review",
            "proposed_sequence": 1,
            "updated_sequence": 1,
        }
    )
    assert (_rerun_spec(action) is not None) is eligible


def _configuration() -> EvaluationConfiguration:
    return EvaluationConfiguration(
        provider="synthetic",
        model="test-model",
        request_model="test-model",
        model_revision="a" * 40,
        platform="generic",
        hardware=("cpu",),
        runtime="TestLLM",
        openhands_version="1.46.0",
        precision="not-applicable",
        context_tokens=32768,
        output_tokens=4096,
        tool_parser="native",
        skill_tree_digest="b" * 64,
        harness_revision="c" * 64,
    )


def _prepare(root: Path, case_id: str = "baseline-analysis") -> ResearchTask:
    task = next(task for task in research_tasks() if task.case.case_id == case_id)
    for name, text in task.inputs.items():
        (root / name).write_text(text)
    return task


def _message(command: str, call_id: str) -> Message:
    return Message(
        role="assistant",
        content=[],
        tool_calls=[
            MessageToolCall(
                id=call_id,
                name="terminal",
                arguments=json.dumps({"command": command}),
                origin="completion",
            )
        ],
    )


def _finish() -> Message:
    return Message(role="assistant", content=[TextContent(text="Synthetic analysis complete.")])


def _program_files() -> dict[str, str]:
    reference = (Path(__file__).parent / "fixtures/research/reference_analysis.py").read_text()
    plan = json.dumps(
        {
            "question": "Does measurement predict response for held-out subjects?",
            "estimand": "Held-out visit prediction error",
            "outcome": "response",
            "features": ["measurement"],
            "group_column": "subject_id",
            "split_column": "partition",
            "assumptions": ["Prespecified subject-disjoint split"],
            "limitations": ["Synthetic data with too few subjects for scientific inference"],
        }
    )
    return {
        "analysis.py": reference,
        "plan.json": plan,
        "report.md": "Synthetic held-out baseline and subject sensitivity.\n",
    }


def _file_message(root: Path, artifacts: Mapping[str, str] | None = None) -> Message:
    return Message(
        role="assistant",
        content=[],
        tool_calls=[
            MessageToolCall(
                id=f"create-{name}",
                name="file_editor",
                arguments=json.dumps(
                    {"command": "create", "path": str(root / name), "file_text": text}
                ),
                origin="completion",
            )
            for name, text in (_program_files() if artifacts is None else artifacts).items()
        ],
    )


def _gateway(
    root: Path,
    llm: TestLLM,
    *,
    backend_type: type[OpenHandsSdkBackend] = OpenHandsSdkBackend,
) -> SessionGateway:
    def service_factory(sessions_root: Path, session_id: str) -> SessionService:
        persistence = sessions_root / session_id / "openhands"

        def factory(
            callback: Callable[[Event], None], token_callback: Callable[[LLMStreamChunk], None]
        ) -> LocalConversation:
            agent = OpenHandsAgentSettings(
                llm=llm,
                tools=[
                    Tool(name=TerminalTool.name),
                    Tool(name=PROJECT_FILE_EDITOR_SPEC, params={"project_root": str(root)}),
                ],
                enable_switch_llm_tool=False,
                tool_concurrency_limit=1,
            ).create_agent()
            conversation = LocalConversation(
                agent=agent,
                workspace=root,
                persistence_dir=persistence,
                profile_store_dir=persistence / "profiles",
                conversation_id=uuid.uuid5(uuid.NAMESPACE_URL, str(persistence)),
                callbacks=[callback],
                token_callbacks=[token_callback],
                visualizer=None,
                delete_on_close=False,
            )
            conversation.set_confirmation_policy(AlwaysConfirm())
            if isinstance(llm, _MeteredTestLLM):
                # SDK discovery intentionally excludes LLM subclasses such as TestLLM.
                conversation.llm_registry.subscribe(conversation.state.stats.register_llm)
                conversation.llm_registry.add(llm)
            return conversation

        backend = backend_type(
            profile=ModelProfile(
                profile_id="heartwood",
                model="openai/local-model",
                base_url="http://127.0.0.1:8765/v1",
                policy_endpoint="http://127.0.0.1:8765/v1/chat/completions",
                credential_kind="none",
            ),
            workspace=root,
            skills_dir=root / ".heartwood/skills",
            persistence_dir=persistence,
            conversation_key=f"{root}#{session_id}",
            env={},
            conversation_factory=factory,
        )
        return SessionService.local_default(
            sessions_root, session_id=session_id, backend=backend, env={}
        )

    return SessionGateway(project=ProjectContext(root), service_factory=service_factory, env={})


@pytest.mark.parametrize("condition", ["live", "model", "platform", "sdk", "fingerprint"])
def test_invalid_runtime_declarations_fail_before_model_work(
    tmp_path: Path, condition: str
) -> None:
    task = _prepare(tmp_path, "dataset-readiness")
    llm = TestLLM.from_messages([_finish()])
    gateway = _gateway(tmp_path, llm)
    changes: dict[str, dict[str, object]] = {
        "live": {},
        "model": {"request_model": "mislabelled-model"},
        "platform": {"platform": "terra"},
        "sdk": {"openhands_version": "1.41.0"},
        "fingerprint": {"runtime_fingerprint": "d" * 64},
    }
    try:
        with pytest.raises(
            ValueError, match=r"production OpenHands|observed client runtime|runtime fingerprint"
        ):
            run_research_trial(
                gateway,
                task,
                configuration=_configuration().model_copy(update=changes[condition]),
                execution="live_model" if condition == "live" else "deterministic",
                review=lambda _group: "approve",
            )
        assert llm.call_count == 0
        assert not (gateway.project.state_root / "evaluations").exists()
    finally:
        gateway.stop()


@pytest.mark.parametrize("during_review", [False, True])
def test_runtime_change_leaves_incomplete_evidence_and_never_approves_a_tool(
    tmp_path: Path, during_review: bool
) -> None:
    task = _prepare(tmp_path, "dataset-readiness")
    llm = TestLLM.from_messages([_file_message(tmp_path)] if during_review else [_finish()])
    gateway = _gateway(tmp_path, llm)

    def review(_group: ProjectionApprovalGroup) -> ReviewDecision:
        llm.temperature = 0.8
        return "approve"

    def observe(_projection: object) -> None:
        if not during_review:
            llm.temperature = 0.8

    try:
        with pytest.raises(ValueError, match="runtime changed"):
            run_research_trial(
                gateway,
                task,
                configuration=_configuration(),
                execution="deterministic",
                review=review,
                observe=observe,
            )
        records = EvaluationStore(gateway.project.state_root / "evaluations").records()
        assert len(records) == 1
        assert records[0].status == "incomplete"
        assert all(check.status == "not_run" for check in records[0].checks)
        assert not (tmp_path / "analysis.py").exists()
        assert llm.call_count <= 1
    finally:
        gateway.stop()


class _MeteredTestLLM(TestLLM):
    """Exercise production usage projection with deterministic provider measurements."""

    def completion(
        self,
        messages: list[Message],
        tools: Sequence[ToolDefinition[Action, Observation]] | None = None,
        add_security_risk_prediction: bool = False,
        on_token: TokenCallbackType | None = None,
        call_context: LLMCallContext | None = None,
        **kwargs: object,
    ) -> LLMResponse:
        response = super().completion(
            messages,
            tools,
            add_security_risk_prediction,
            on_token,
            call_context,
            **kwargs,
        )
        self.metrics.add_token_usage(80, 20, 0, 0, 32768, f"measured-{self.call_count}")
        self.metrics.add_cost(0.01)
        return response.model_copy(update={"metrics": self.metrics.get_snapshot()})


@pytest.mark.parametrize("propose_action", [False, True])
def test_trial_waits_for_sdk_finalization_before_completion_or_review(
    tmp_path: Path, propose_action: bool
) -> None:
    finalizing = ThreadEvent()
    release = ThreadEvent()

    class DelayedFinalizationBackend(OpenHandsSdkBackend):
        async def _run_until_stable(
            self, *, session_id: str, conversation: BaseConversation
        ) -> frozenset[str]:
            result = await super()._run_until_stable(
                session_id=session_id, conversation=conversation
            )
            finalizing.set()
            if not await asyncio.to_thread(release.wait, 5):
                raise TimeoutError("Synthetic SDK finalization was not released")
            return result

    task = _prepare(tmp_path, "dataset-readiness")
    messages: list[Message | Exception] = (
        [_file_message(tmp_path, {"readiness.md": "Synthetic review"}), _finish()]
        if propose_action
        else [_finish()]
    )
    llm = _MeteredTestLLM.from_messages(messages)
    gateway = _gateway(tmp_path, llm, backend_type=DelayedFinalizationBackend)
    reviewed: list[str] = []

    def review(group: ProjectionApprovalGroup) -> ReviewDecision:
        assert release.is_set()
        reviewed.append(group.group_id)
        return "approve"

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            run_research_trial,
            gateway,
            task,
            configuration=_configuration(),
            execution="deterministic",
            review=review,
        )
        try:
            assert finalizing.wait(timeout=3)
            assert not future.done()
            assert not reviewed
            assert not (tmp_path / "readiness.md").exists()
            release.set()
            trial = future.result(timeout=30)
            assert trial.stop == "finished"
            assert trial.record.usage.model_calls == (2 if propose_action else 1)
            assert trial.record.usage.input_tokens == (160 if propose_action else 80)
            assert len(reviewed) == int(propose_action)
        finally:
            release.set()
            try:
                future.result(timeout=30)
            finally:
                gateway.stop()


def test_unsettled_final_boundary_leaves_an_incomplete_trial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = _prepare(tmp_path, "dataset-readiness")
    llm = TestLLM.from_messages([_finish()])
    gateway = _gateway(tmp_path, llm)
    wait = gateway.wait_for_session_idle

    def unsettled(*, session_id: str, timeout: float = 0) -> bool:
        return False if timeout == 30 else wait(session_id=session_id, timeout=timeout)

    monkeypatch.setattr(gateway, "wait_for_session_idle", unsettled)
    try:
        with pytest.raises(TimeoutError, match="settled execution boundary"):
            run_research_trial(
                gateway,
                task,
                configuration=_configuration(),
                execution="deterministic",
                review=lambda _: pytest.fail("No tools expected"),
            )
        (record,) = EvaluationStore(gateway.project.state_root / "evaluations").records()
        assert record.status == "incomplete"
        assert all(check.status == "not_run" for check in record.checks)
        assert llm.call_count == 1
    finally:
        gateway.stop()


@pytest.mark.parametrize(
    "limits",
    [
        {"maximum_model_calls": 1},
        {"maximum_tokens": 100},
        {"maximum_reported_cost_usd": 0.01},
    ],
)
@pytest.mark.parametrize("finished", [False, True])
def test_exact_provider_limit_allows_completion_but_never_another_reviewed_action(
    tmp_path: Path, limits: dict[str, int | float], finished: bool
) -> None:
    task = _prepare(tmp_path, "dataset-readiness")
    llm = _MeteredTestLLM.from_messages([_finish() if finished else _file_message(tmp_path)])
    gateway = _gateway(tmp_path, llm)
    try:
        trial = run_research_trial(
            gateway,
            task,
            configuration=_configuration(),
            execution="deterministic",
            review=lambda _: pytest.fail("No action may be admitted at the provider limit"),
            budget=ExecutionBudget.model_validate(limits),
        )
        assert trial.stop == ("finished" if finished else "budget-exceeded")
        assert llm.call_count == trial.record.usage.model_calls == 1
        assert trial.record.usage.input_tokens == 80
        assert trial.record.usage.output_tokens == 20
        assert trial.record.usage.reported_cost_usd == 0.01
        assert not (tmp_path / "analysis.py").exists()
        assert trial.record.usage.exceeded_limits(trial.record.budget) == ()
    finally:
        gateway.stop()


def test_response_overrun_cannot_be_reported_as_successful_completion(tmp_path: Path) -> None:
    task = _prepare(tmp_path, "dataset-readiness")
    llm = _MeteredTestLLM.from_messages([_finish()])
    gateway = _gateway(tmp_path, llm)
    try:
        trial = run_research_trial(
            gateway,
            task,
            configuration=_configuration(),
            execution="deterministic",
            review=lambda _: pytest.fail("No tools expected"),
            budget=ExecutionBudget(maximum_tokens=99),
        )
        assert trial.stop == "budget-exceeded"
        assert llm.call_count == 1
        assert trial.record.usage.exceeded_limits(trial.record.budget) == ("tokens",)
        assert next(
            c for c in trial.record.checks if c.check_id == "workflow-completed"
        ).status == ("failed")
    finally:
        gateway.stop()


@pytest.mark.parametrize("maximum_actions", [1, 2])
def test_group_at_action_limit_is_allowed_but_oversized_group_never_executes(
    tmp_path: Path, maximum_actions: int
) -> None:
    task = _prepare(tmp_path, "dataset-readiness")
    outputs = {"readiness.md": "Synthetic review", "readiness.json": "{}"}
    llm = TestLLM.from_messages([_file_message(tmp_path, outputs), _finish()])
    gateway = _gateway(tmp_path, llm)
    reviewed: list[str] = []

    def review(group: ProjectionApprovalGroup) -> ReviewDecision:
        reviewed.append(group.group_id)
        return "approve"

    try:
        trial = run_research_trial(
            gateway,
            task,
            configuration=_configuration(),
            execution="deterministic",
            review=review,
            budget=ExecutionBudget(maximum_actions=maximum_actions),
        )
        assert trial.stop == ("finished" if maximum_actions == 2 else "budget-exceeded")
        assert len(reviewed) == (1 if maximum_actions == 2 else 0)
        assert (tmp_path / "readiness.md").exists() is (maximum_actions == 2)
        assert trial.record.usage.proposed_actions == 2
    finally:
        gateway.stop()


def test_expired_budget_after_review_does_not_execute_the_approved_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = _prepare(tmp_path)
    llm = TestLLM.from_messages([_file_message(tmp_path)])
    gateway = _gateway(tmp_path, llm)
    clock = [0.0]
    monkeypatch.setattr(
        "heartwood.compliance.research_runner.time",
        SimpleNamespace(monotonic=lambda: clock[0], sleep=time.sleep),
    )

    def review(_group: ProjectionApprovalGroup) -> ReviewDecision:
        clock[0] = 300.0
        return "approve"

    try:
        trial = run_research_trial(
            gateway,
            task,
            configuration=_configuration(),
            execution="deterministic",
            review=review,
        )
        assert trial.stop == "budget-exceeded"
        assert llm.call_count == 1
        assert not (tmp_path / "analysis.py").exists()
        assert trial.record.usage.elapsed_seconds == 300.0
    finally:
        gateway.stop()


def test_exhausted_baseline_cannot_start_reproduction_or_claim_workflow_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}")
    task = _prepare(tmp_path)
    command = "python analysis.py --data data.csv --output-dir ."
    llm = _MeteredTestLLM.from_messages(
        [_file_message(tmp_path), _message(command, "primary"), _finish()]
    )
    gateway = _gateway(tmp_path, llm)
    try:
        trial = run_research_trial(
            gateway,
            task,
            configuration=_configuration(),
            execution="deterministic",
            review=lambda _: "approve",
            budget=ExecutionBudget(maximum_model_calls=3),
        )
        assert trial.stop == "budget-exceeded"
        assert llm.call_count == trial.record.usage.model_calls == 3
        assert (tmp_path / "metrics.json").exists()
        assert not (tmp_path / "benchmark-reproduced").exists()
        checks = {check.check_id: check.status for check in trial.record.checks}
        assert checks["workflow-completed"] == checks["independent-script-rerun"] == "failed"
        assert checks["baseline-heldout-metrics"] == "passed"
    finally:
        gateway.stop()


@pytest.mark.parametrize("scratch_directory", [False, True])
def test_baseline_uses_reviewed_tools_and_a_separate_process_to_reproduce_and_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scratch_directory: bool,
) -> None:
    monkeypatch.setenv("PATH", f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}")
    task = _prepare(tmp_path)
    command = "python analysis.py --data data.csv --output-dir ."
    rerun = "python analysis.py --data data.csv --output-dir benchmark-reproduced"
    scratch = "mkdir -p scratch && printf synthetic > scratch/note.txt"
    llm = TestLLM.from_messages(
        [
            *([_message(scratch, "scratch")] if scratch_directory else []),
            _file_message(tmp_path),
            _message(command, "primary"),
            _finish(),
            _message(rerun, "rerun"),
            _finish(),
        ]
    )
    gateway = _gateway(tmp_path, llm)
    groups: list[str] = []

    def review(group: ProjectionApprovalGroup) -> ReviewDecision:
        for action in group.actions:
            if action.details.kind == "terminal":
                assert action.details.command in (command, rerun, scratch)
            else:
                assert action.details.kind == "file-editor"
                path = Path(str(action.arguments["path"]))
                assert path.parent == tmp_path
                assert action.arguments["file_text"] == _program_files()[path.name]
        groups.append(group.group_id)
        return "approve"

    try:
        trial = run_research_trial(
            gateway,
            task,
            configuration=_configuration(),
            execution="deterministic",
            review=review,
            budget=ExecutionBudget(maximum_seconds=20),
        )
        assert trial.stop == "finished"
        assert len(groups) == len(set(groups)) == (4 if scratch_directory else 3)
        assert {check.check_id: check.status for check in trial.record.checks} == {
            check.check_id: "passed" for check in task.case.required_checks
        }
        assert trial.record.execution == "deterministic"
        runtime = trial.record.runtime_observation
        assert runtime is not None
        assert runtime.source == "injected"
        assert runtime.request_model == llm.model
        assert trial.record.configuration.runtime_fingerprint == runtime.fingerprint
        assert llm.call_count == (6 if scratch_directory else 5)
        assert trial.record.usage.model_calls is None
        assert trial.record.usage.reported_cost_usd is None
        assert "Synthetic analysis complete" not in trial.record.model_dump_json()
        assert EvaluationStore(gateway.project.state_root / "evaluations").records() == (
            trial.record,
        )
    finally:
        gateway.stop()

    verification_root = tmp_path / "independent-verification"
    verification_root.mkdir()
    verification_task = _prepare(verification_root, "result-verification")
    for name in ("analysis.py", "metrics.json", "predictions.csv"):
        (verification_root / name).write_text(trial.artifacts[name])
    outputs = {
        "verification.json": json.dumps(
            {
                "status": "reproduced",
                "matching_artifacts": ["metrics.json", "predictions.csv"],
                "mismatched_artifacts": [],
            }
        ),
        "verification.md": "Re-executed the source program and verified identical outputs.",
    }
    command = "python analysis.py --data data.csv --output-dir reproduced"
    verification_llm = TestLLM.from_messages(
        [_message(command, "reexecute"), _file_message(verification_root, outputs), _finish()]
    )
    verification_gateway = _gateway(verification_root, verification_llm)

    def verify_review(group: ProjectionApprovalGroup) -> ReviewDecision:
        for action in group.actions:
            if action.details.kind == "terminal":
                assert action.details.command == command
            else:
                assert action.details.kind == "file-editor"
                path = Path(str(action.arguments["path"]))
                assert path.parent == verification_root
                assert action.arguments["file_text"] == outputs[path.name]
        return "approve"

    try:
        verified = run_research_trial(
            verification_gateway,
            verification_task,
            configuration=_configuration(),
            execution="deterministic",
            review=verify_review,
            budget=ExecutionBudget(maximum_seconds=20),
        )
        assert verified.stop == "finished"
        assert all(check.status == "passed" for check in verified.record.checks)
        assert verification_llm.call_count == 3
    finally:
        verification_gateway.stop()


def test_refused_action_never_creates_research_artifacts(tmp_path: Path) -> None:
    task = _prepare(tmp_path)
    llm = TestLLM.from_messages([_file_message(tmp_path), _finish()])
    gateway = _gateway(tmp_path, llm)
    try:
        trial = run_research_trial(
            gateway,
            task,
            configuration=_configuration(),
            execution="deterministic",
            review=lambda _group: "stop",
        )
        assert trial.stop == "review-stopped"
        assert not (tmp_path / "analysis.py").exists()
        checks = {check.check_id: check.status for check in trial.record.checks}
        assert checks["workflow-completed"] == checks["tools-executed"] == "failed"
        assert checks["audit-verified"] == checks["fresh-process-replay"] == "passed"
    finally:
        gateway.stop()


def test_nonfixture_input_is_rejected_before_a_model_call(tmp_path: Path) -> None:
    task = _prepare(tmp_path)
    (tmp_path / "data.csv").write_text("not the supplied synthetic fixture")
    gateway = _gateway(tmp_path, TestLLM.from_messages([]))
    try:
        with pytest.raises(ValueError, match="pinned synthetic"):
            run_research_trial(
                gateway,
                task,
                configuration=_configuration(),
                execution="deterministic",
                review=lambda _group: "approve",
            )
        assert gateway.sessions()["sessions"] == []
    finally:
        gateway.stop()


def test_budget_stops_before_any_pending_tool_is_approved(tmp_path: Path) -> None:
    task = _prepare(tmp_path)
    gateway = _gateway(tmp_path, TestLLM.from_messages([_file_message(tmp_path)]))
    try:
        trial = run_research_trial(
            gateway,
            task,
            configuration=_configuration(),
            execution="deterministic",
            review=lambda _group: pytest.fail("No action should be reviewed after the budget"),
            budget=ExecutionBudget(maximum_seconds=0.001),
        )
        assert trial.stop == "budget-exceeded"
        assert not (tmp_path / "analysis.py").exists()
    finally:
        gateway.stop()


def test_rejected_group_never_executes_and_does_not_schedule_a_reproduction(tmp_path: Path) -> None:
    task = _prepare(tmp_path)
    llm = TestLLM.from_messages([_file_message(tmp_path), _finish()])
    gateway = _gateway(tmp_path, llm)
    try:
        trial = run_research_trial(
            gateway,
            task,
            configuration=_configuration(),
            execution="deterministic",
            review=lambda _group: "reject",
            budget=ExecutionBudget(maximum_seconds=10),
        )
        assert not trial.artifacts
        assert llm.call_count == 1
        checks = {check.check_id: check.status for check in trial.record.checks}
        assert checks["baseline-artifacts"] == checks["tools-executed"] == "failed"
        assert checks["independent-script-rerun"] == "failed"
    finally:
        gateway.stop()


def test_readiness_completes_through_grouped_tools_without_an_extra_model_turn(
    tmp_path: Path,
) -> None:
    task = _prepare(tmp_path, "dataset-readiness")
    outputs = {
        "readiness.json": json.dumps(
            {
                "row_count": 15,
                "subject_count": 8,
                "duplicate_rows": 1,
                "missing_by_column": {"measurement": 1},
                "invalid_by_column": {"measurement": 1, "visit": 1},
                "arm_counts": {"A": 7, "B": 8},
                "leakage_columns": ["future_response"],
                "ready_for_analysis": False,
            }
        ),
        "readiness.md": (
            "Resolve invalid/missing measurements and duplicates; exclude future_response."
        ),
    }
    llm = TestLLM.from_messages([_file_message(tmp_path, outputs), _finish()])
    gateway = _gateway(tmp_path, llm)

    def review(group: ProjectionApprovalGroup) -> ReviewDecision:
        assert len(group.actions) == 2
        for action in group.actions:
            assert action.details.kind == "file-editor"
            assert (
                action.arguments["file_text"] == outputs[Path(str(action.arguments["path"])).name]
            )
        return "approve"

    try:
        trial = run_research_trial(
            gateway,
            task,
            configuration=_configuration(),
            execution="deterministic",
            review=review,
            budget=ExecutionBudget(maximum_seconds=10),
        )
        assert trial.stop == "finished"
        assert llm.call_count == 2
        assert all(check.status == "passed" for check in trial.record.checks)
    finally:
        gateway.stop()


@pytest.mark.parametrize("noop_order", ["absent", "before", "after"])
def test_copied_outputs_are_not_evidence_of_independent_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, noop_order: str
) -> None:
    monkeypatch.setenv("PATH", f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}")
    task = _prepare(tmp_path, "result-verification")
    originals = {"metrics.json": "{}\n", "predictions.csv": "subject_id,visit,prediction\n"}
    for name, value in {
        **originals,
        "analysis.py": "# A source program with no execution\n",
    }.items():
        (tmp_path / name).write_text(value)
    outputs = {
        **{f"reproduced/{name}": value for name, value in originals.items()},
        "verification.json": json.dumps(
            {
                "status": "reproduced",
                "matching_artifacts": ["metrics.json", "predictions.csv"],
                "mismatched_artifacts": [],
            }
        ),
        "verification.md": "The copied bytes match, but the script did not run.",
    }
    messages: list[Message | Exception] = [
        _message("mkdir reproduced", "prepare-output"),
        _file_message(tmp_path, outputs),
    ]
    if noop_order != "absent":
        messages.insert(
            0 if noop_order == "before" else len(messages),
            _message("python analysis.py --data data.csv --output-dir reproduced", "noop"),
        )
    messages.append(_finish())
    gateway = _gateway(tmp_path, TestLLM.from_messages(messages))
    try:
        trial = run_research_trial(
            gateway,
            task,
            configuration=_configuration(),
            execution="deterministic",
            review=lambda _group: "approve",
            budget=ExecutionBudget(maximum_seconds=10),
        )
        checks = {check.check_id: check.status for check in trial.record.checks}
        assert checks["verification-comparison"] == "passed"
        assert checks["independent-script-rerun"] == "failed"
    finally:
        gateway.stop()


@pytest.mark.parametrize("name", ["analysis.py", "unrelated.txt", "benchmark-reproduced"])
def test_nonempty_projects_are_rejected_without_reading_unrelated_files(
    tmp_path: Path, name: str
) -> None:
    task = _prepare(tmp_path)
    (tmp_path / name).write_text("Do not inspect unrelated content.")
    llm = TestLLM.from_messages([])
    gateway = _gateway(tmp_path, llm)
    try:
        with pytest.raises(ValueError, match="dedicated project"):
            run_research_trial(
                gateway,
                task,
                configuration=_configuration(),
                execution="deterministic",
                review=lambda _group: "approve",
            )
        assert llm.call_count == 0
    finally:
        gateway.stop()


def test_review_failure_leaves_durable_incomplete_evidence_without_executing_tools(
    tmp_path: Path,
) -> None:
    task = _prepare(tmp_path)
    llm = TestLLM.from_messages([_file_message(tmp_path)])
    gateway = _gateway(tmp_path, llm)
    store = EvaluationStore(gateway.project.state_root / "evaluations")

    def review(_group: ProjectionApprovalGroup) -> ReviewDecision:
        records = store.records()
        assert len(records) == 1
        assert records[0].status == "incomplete"
        raise RuntimeError("synthetic reviewer failure: never publish this text")

    try:
        with pytest.raises(RuntimeError, match="synthetic reviewer failure"):
            run_research_trial(
                gateway,
                task,
                configuration=_configuration(),
                execution="deterministic",
                review=review,
            )
        records = EvaluationStore(store.root).records()
        assert len(records) == 1
        record = records[0]
        assert record.status == "incomplete"
        assert all(check.status == "not_run" for check in record.checks)
        assert "never publish" not in record.model_dump_json()
        assert record.session_id is not None
        projection = gateway.session_projection(session_id=record.session_id)
        assert projection.lifecycle.status == "waiting-for-confirmation"
        assert projection.pending_approval is not None
        assert all(action.state == "awaiting-review" for action in projection.actions)
        assert llm.call_count == 1
        assert not (tmp_path / "analysis.py").exists()
    finally:
        gateway.stop()


def test_failed_evidence_reservation_prevents_model_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _prepare(tmp_path)
    llm = TestLLM.from_messages([])
    gateway = _gateway(tmp_path, llm)

    def fail(*_args: object) -> None:
        raise OSError("synthetic full disk")

    monkeypatch.setattr(EvaluationStore, "begin", fail)
    try:
        with pytest.raises(OSError, match="synthetic full disk"):
            run_research_trial(
                gateway,
                task,
                configuration=_configuration(),
                execution="deterministic",
                review=lambda _group: "stop",
            )
        assert llm.call_count == 0
    finally:
        gateway.stop()


def test_replacing_the_program_for_reexecution_then_restoring_it_does_not_qualify(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}")
    task = _prepare(tmp_path, "result-verification")
    originals = {"metrics.json": "{}\n", "predictions.csv": "subject_id,visit,prediction\n"}
    original_program = "# The supplied program produces no outputs.\n"
    for name, text in {**originals, "analysis.py": original_program}.items():
        (tmp_path / name).write_text(text)
    replacement = (
        "from pathlib import Path\nPath('reproduced').mkdir()\n"
        + "\n".join(
            f"Path({f'reproduced/{name}'!r}).write_text({text!r})"
            for name, text in originals.items()
        )
        + "\n"
    )

    def replace_program(old: str, new: str, call_id: str) -> Message:
        return Message(
            role="assistant",
            content=[],
            tool_calls=[
                MessageToolCall(
                    id=call_id,
                    name="file_editor",
                    origin="completion",
                    arguments=json.dumps(
                        {
                            "command": "str_replace",
                            "path": str(tmp_path / "analysis.py"),
                            "old_str": old,
                            "new_str": new,
                        }
                    ),
                )
            ],
        )

    report = {
        "verification.json": json.dumps(
            {
                "status": "reproduced",
                "matching_artifacts": list(originals),
                "mismatched_artifacts": [],
            }
        ),
        "verification.md": "The replacement produced matching bytes, not a reproduction.",
    }
    llm = TestLLM.from_messages(
        [
            replace_program(original_program, replacement, "replace-program"),
            _message(
                "python analysis.py --data data.csv --output-dir reproduced", "replacement-run"
            ),
            replace_program(replacement, original_program, "restore-program"),
            _file_message(tmp_path, report),
            _finish(),
        ]
    )
    gateway = _gateway(tmp_path, llm)
    try:
        trial = run_research_trial(
            gateway,
            task,
            configuration=_configuration(),
            execution="deterministic",
            review=lambda _group: "approve",
            budget=ExecutionBudget(maximum_seconds=15),
        )
        assert trial.stop == "finished"
        assert llm.call_count == 5
        assert (tmp_path / "analysis.py").read_text() == original_program
        checks = {check.check_id: check.status for check in trial.record.checks}
        assert checks["verification-comparison"] == "passed"
        assert checks["independent-script-rerun"] == "failed"
    finally:
        gateway.stop()
