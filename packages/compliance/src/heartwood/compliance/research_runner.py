# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Bounded benchmark driver using the same gateway as all researcher interfaces."""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Literal
from uuid import UUID, uuid4

from heartwood.compliance.replay_evidence import replay_evidence
from heartwood.compliance.research import ResearchTask, research_suite, verify_research_artifacts
from heartwood.core_adapter import SessionResult
from heartwood.gateway import (
    ProjectionActionRecord,
    ProjectionApprovalGroup,
    SessionGateway,
    SessionProjection,
    WorkspaceInspectionError,
)
from heartwood.schemas.evaluation import (
    EvaluationBudget,
    EvaluationCheck,
    EvaluationConfiguration,
    EvaluationRun,
    EvaluationUsage,
)
from heartwood.session import CommandKind, EventKind, SessionCommand

type ReviewDecision = Literal["approve", "reject", "stop"]
type ResearchStop = Literal[
    "finished", "error", "paused", "rejected", "review-stopped", "budget-exceeded"
]
_RERUN_COMMAND = "python analysis.py --data data.csv --output-dir benchmark-reproduced"
_PRIMARY_OUTPUTS = ("metrics.json", "predictions.csv")
_DEFAULT_BUDGET = EvaluationBudget()


@dataclass(frozen=True)
class ResearchTrial:
    """Content-minimized public record plus private synthetic outputs for chaining."""

    record: EvaluationRun
    stop: ResearchStop
    artifacts: Mapping[str, str]


@dataclass(frozen=True)
class _TrialSession:
    gateway: SessionGateway
    session_id: str
    run_id: UUID
    created_at: str
    approved_action_ids: set[str] = field(default_factory=set)

    def command(self, suffix: str, kind: CommandKind, payload: dict[str, object]) -> SessionResult:
        return self.gateway.handle(
            SessionCommand.model_validate(
                {
                    "command_id": f"benchmark-{self.run_id}-{suffix}",
                    "session_id": self.session_id,
                    "created_at": self.created_at,
                    "kind": kind,
                    "payload": payload,
                }
            )
        )

    def pause(self) -> None:
        projection = self.gateway.session_projection(session_id=self.session_id)
        if "pause" in projection.available_commands:
            self.command("stop", CommandKind.PAUSE, {})


def run_research_trial(
    gateway: SessionGateway,
    task: ResearchTask,
    *,
    configuration: EvaluationConfiguration,
    execution: Literal["deterministic", "live_model"],
    review: Callable[[ProjectionApprovalGroup], ReviewDecision],
    budget: EvaluationBudget = _DEFAULT_BUDGET,
    seed: int = 0,
    observe: Callable[[SessionProjection], None] | None = None,
) -> ResearchTrial:
    """Run one prepared synthetic case with explicit review and independent checks.

    The caller prepares an isolated project and owns gateway shutdown. Only exact
    maintained fixture inputs are accepted. Review receives complete gateway
    action groups; a callback must not approve arbitrary model output blindly.
    Limits are checked between observable state updates, so in-flight requests
    may finish before a pause takes effect. No unknown cost is treated as free.
    """
    suite = research_suite()
    if task.case not in suite.cases:
        raise ValueError("Research trial requires an unchanged maintained case")
    verify_research_artifacts(task, {})
    expected_files = {".heartwood", *task.inputs}
    if task.case.case_id == "result-verification":
        expected_files.update(("analysis.py", *_PRIMARY_OUTPUTS))
    if any(path.name not in expected_files for path in gateway.project.root.iterdir()):
        raise ValueError(
            "Research trials require a dedicated project containing only fixture inputs"
        )
    inputs = _read_artifacts(gateway, tuple(task.inputs))
    if inputs != dict(task.inputs):
        raise ValueError("Project inputs do not match the pinned synthetic research fixture")
    if task.case.case_id == "result-verification":
        supplied = _read_artifacts(gateway, ("analysis.py", *_PRIMARY_OUTPUTS))
        if len(supplied) != 3:
            raise ValueError("Independent verification requires the baseline program and outputs")
    else:
        supplied = {}

    run_id = uuid4()
    session_id = gateway.create_session(f"Research benchmark: {task.case.case_id}")["session_id"]
    started_at = datetime.now(UTC)
    session = _TrialSession(gateway, session_id, run_id, started_at.isoformat())
    started = time.monotonic()
    stop: ResearchStop = "error"
    try:
        session.command("task", CommandKind.CHAT, {"prompt": task.instruction})
        stop = _drive(session, review, budget, started, observe)
        artifacts = _read_artifacts(gateway, task.artifact_paths)
        rerun = False
        if (
            task.case.case_id == "baseline-analysis"
            and stop == "finished"
            and all(
                check.status == "passed" for check in verify_research_artifacts(task, artifacts)
            )
            and not _budget_exceeded(
                gateway.session_projection(session_id=session_id), budget, started
            )
        ):
            primary = _read_artifacts(gateway, ("analysis.py", *_PRIMARY_OUTPUTS))
            prior_actions = gateway.session_projection(session_id=session_id).actions
            session.command(
                "verify",
                CommandKind.CHAT,
                {
                    "prompt": (
                        "Independently reproduce the outputs now. Propose exactly this terminal "
                        f"command for review: {_RERUN_COMMAND}. Do not modify analysis.py or the "
                        "primary outputs, and do not create reproduced files by any other means. "
                        "After execution, report completion. "
                        "Do not access other files or the network."
                    )
                },
            )
            stop = _drive(session, review, budget, started, observe)
            projected = gateway.session_projection(session_id=session_id)
            new_actions = projected.actions[len(prior_actions) :]
            reproduced = _read_artifacts(
                gateway, tuple(f"benchmark-reproduced/{name}" for name in _PRIMARY_OUTPUTS)
            )
            rerun = (
                stop == "finished"
                and len(primary) == 3
                and primary == _read_artifacts(gateway, tuple(primary))
                and any(
                    _is_approved_rerun(action, "benchmark-reproduced") for action in new_actions
                )
                and all(
                    reproduced.get(f"benchmark-reproduced/{name}") == primary.get(name)
                    for name in _PRIMARY_OUTPUTS
                )
            )
            artifacts = _read_artifacts(gateway, task.artifact_paths)
        if task.case.case_id == "result-verification":
            artifacts.update(_read_artifacts(gateway, _PRIMARY_OUTPUTS))
            rerun = (
                supplied == _read_artifacts(gateway, tuple(supplied))
                and any(
                    _is_approved_rerun(action, "reproduced")
                    for action in (gateway.session_projection(session_id=session_id).actions)
                )
                and all(f"reproduced/{name}" in artifacts for name in _PRIMARY_OUTPUTS)
            )

        session.command("audit", CommandKind.AUDIT_EXPORT, {})
        gateway.audit_export(session_id)
        expected_replay = replay_evidence(gateway, session_id)
        replay_passed = _fresh_replay(gateway, session_id, expected_replay)
        projection = gateway.session_projection(session_id=session_id)
        states = {
            check.check_id: check.status for check in verify_research_artifacts(task, artifacts)
        }
        states.update(
            {
                "model-connected": "passed"
                if projection.context.model_decision == "allow"
                and (
                    projection.actions
                    or any(message.role == "agent" for message in projection.conversation)
                )
                else "failed",
                "tools-executed": "passed"
                if any(
                    action.state == "succeeded" and action.tool_name in ("terminal", "file_editor")
                    for action in projection.actions
                )
                else "failed",
                "workflow-completed": "passed" if stop == "finished" else "failed",
                "approved-actions-only": "passed"
                if _reviewed_execution(projection, session.approved_action_ids)
                else "failed",
                "fresh-process-replay": "passed" if replay_passed else "failed",
                "audit-verified": "passed",
            }
        )
        if "independent-script-rerun" in {check.check_id for check in task.case.required_checks}:
            states["independent-script-rerun"] = "passed" if rerun else "failed"
        if inputs != _read_artifacts(gateway, tuple(inputs)):
            states["approved-actions-only"] = "failed"
        usage = projection.usage
        record = EvaluationRun(
            run_id=run_id,
            suite_id=suite.suite_id,
            suite_fingerprint=suite.fingerprint,
            case_id=task.case.case_id,
            fixture_digest=task.case.fixture_digest,
            seed=seed,
            execution=execution,
            configuration=configuration,
            started_at=started_at,
            finished_at=datetime.now(UTC),
            budget=budget,
            checks=tuple(
                EvaluationCheck(
                    check_id=check.check_id,
                    dimension=check.dimension,
                    status=states.get(check.check_id, "not_run"),
                )
                for check in task.case.required_checks
            ),
            usage=EvaluationUsage(
                input_tokens=usage.prompt_tokens if usage else None,
                output_tokens=usage.completion_tokens if usage else None,
                model_calls=usage.call_count if usage else None,
                # The current gateway projection cannot distinguish unpriced from
                # zero-cost calls. Preserve unknown until upstream reports that fact.
                reported_cost_usd=(
                    usage.accumulated_cost if usage and usage.accumulated_cost > 0 else None
                ),
                elapsed_seconds=time.monotonic() - started,
            ),
        )
        return ResearchTrial(record=record, stop=stop, artifacts=MappingProxyType(artifacts))
    except BaseException:
        session.pause()
        raise


def _drive(
    session: _TrialSession,
    review: Callable[[ProjectionApprovalGroup], ReviewDecision],
    budget: EvaluationBudget,
    started: float,
    observe: Callable[[SessionProjection], None] | None,
) -> ResearchStop:
    seen_revision = -1
    reviewed: set[str] = set()
    while True:
        projection = session.gateway.session_projection(session_id=session.session_id)
        if observe is not None and projection.revision != seen_revision:
            observe(projection)
            seen_revision = projection.revision
        if _budget_exceeded(projection, budget, started):
            session.pause()
            return "budget-exceeded"
        if projection.last_command_outcome is not None and (
            projection.last_command_outcome.status == "rejected"
        ):
            return "error"
        if projection.researcher_status.code == "denied":
            return "rejected"
        if projection.lifecycle.status in ("finished", "error", "paused"):
            if projection.lifecycle.status == "finished":
                return "finished"
            return "paused" if projection.lifecycle.status == "paused" else "error"
        group = projection.pending_approval
        if group is not None:
            if group.group_id in reviewed:
                time.sleep(0.02)
                continue
            decision = review(group)
            if _budget_exceeded(
                session.gateway.session_projection(session_id=session.session_id), budget, started
            ):
                session.pause()
                return "budget-exceeded"
            if decision == "stop":
                session.pause()
                return "review-stopped"
            if decision not in ("approve", "reject"):
                raise ValueError("Benchmark review must explicitly approve, reject, or stop")
            result = session.command(
                f"review-{group.group_id}",
                CommandKind.APPROVE if decision == "approve" else CommandKind.DENY,
                {"target_type": "action-set", "target_id": group.group_id},
            )
            for event in result.events:
                if event.kind == EventKind.APPROVAL_RECORDED and (
                    event.payload.get("decision") == "approved"
                    and event.payload.get("group_id") == group.group_id
                ):
                    session.approved_action_ids.update(
                        action.tool_call_id for action in group.actions
                    )
            reviewed.add(group.group_id)
        time.sleep(0.02)


def _budget_exceeded(
    projection: SessionProjection, budget: EvaluationBudget, started: float
) -> bool:
    usage = projection.usage
    return (
        time.monotonic() - started >= budget.maximum_seconds
        or len(projection.actions) > budget.maximum_actions
        or (
            usage is not None
            and (
                usage.call_count >= budget.maximum_model_calls
                or usage.prompt_tokens + usage.completion_tokens >= budget.maximum_tokens
                or usage.accumulated_cost >= budget.maximum_reported_cost_usd
            )
        )
    )


def _read_artifacts(gateway: SessionGateway, paths: tuple[str, ...]) -> dict[str, str]:
    artifacts: dict[str, str] = {}
    for path in paths:
        try:
            result = gateway.workspace_file(path=path)
        except WorkspaceInspectionError:
            continue
        if result["status"] == "available" and result["content"] is not None:
            artifacts[path] = result["content"]
    return artifacts


def _reviewed_execution(projection: SessionProjection, approved_ids: set[str]) -> bool:
    executed = [action for action in projection.actions if action.outcome is not None]
    return all(
        (action.decision == "approved" and action.tool_call_id in approved_ids)
        or action.tool_name in ("finish", "think", "task_tracker")
        for action in executed
    ) and not any(action.state == "outcome-unknown" for action in projection.actions)


def _is_approved_rerun(action: ProjectionActionRecord, output_directory: str) -> bool:
    if (
        action.state != "succeeded"
        or action.decision != "approved"
        or action.details.kind != "terminal"
    ):
        return False
    try:
        return shlex.split(action.details.command) == [
            "python",
            "analysis.py",
            "--data",
            "data.csv",
            "--output-dir",
            output_directory,
        ]
    except ValueError:
        return False


def _fresh_replay(gateway: SessionGateway, session_id: str, expected: dict[str, object]) -> bool:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "heartwood.compliance.replay_evidence",
            str(gateway.project.root),
            session_id,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        return False
    try:
        value: object = json.loads(result.stdout)
        return value == expected
    except ValueError:
        return False
