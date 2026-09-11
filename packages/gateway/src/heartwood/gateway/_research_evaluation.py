# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Read-only project binding and evidence production for research stages."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath

from heartwood.core_adapter.reproduction import ReproductionWitness
from heartwood.core_adapter.research_checks import (
    compare_reproduction_artifacts,
    evaluate_research_check,
    supported_research_checks,
)
from heartwood.core_adapter.research_workflows import (
    research_workflow,
    research_workflows,
    workflow_reproduction_spec,
)
from heartwood.core_adapter.workflow_evidence import assess_workflow_stage
from heartwood.gateway._research_review import ResearchReviewEvaluator
from heartwood.gateway._session_projection import project_session
from heartwood.gateway._workspace import WorkspaceInspectionError, WorkspaceInspector
from heartwood.gateway.experiments import experiment_digest, observed_python_environment
from heartwood.schemas.execution import ExecutionUsage
from heartwood.schemas.experiments import ExperimentDefinition, ExperimentFile, ExperimentStage
from heartwood.schemas.project_paths import project_relative_path
from heartwood.schemas.review import ReviewAssessment, ReviewSnapshot, ReviewSubmission
from heartwood.schemas.workflows import (
    WorkflowBoundInput,
    WorkflowCatalog,
    WorkflowCatalogEntry,
    WorkflowCheckResult,
    WorkflowDefinition,
    WorkflowOutcomeStatus,
    WorkflowProjectBinding,
    WorkflowRun,
    WorkflowStageEvaluation,
    WorkflowValueFingerprint,
)
from heartwood.session import EventKind, SessionEvent

_REPRODUCTION_CHECKS = frozenset({"execution.reproduction", "execution.comparison"})


class ResearchStageEvaluator:
    """Reuse workspace confinement and core checks without executing an agent or tool."""

    def __init__(self, workspace: WorkspaceInspector) -> None:
        self.workspace = workspace

    def prepare_review(self, binding: WorkflowProjectBinding, stage_id: str) -> ReviewSnapshot:
        """Bind declared files, not paths or evidence roles selected by a model."""
        definition = research_workflow(binding.workflow_id)
        self._validate_binding(definition, binding)
        stage = definition.stage(stage_id)
        scope = set(stage.reads) | set(stage.writes)
        paths = {
            item.input_id: item.value
            for item in binding.inputs
            if item.kind == "file" and item.input_id in scope
        }
        paths.update(
            {
                item.artifact_id: str(PurePosixPath(binding.output_directory) / item.relative_path)
                for item in definition.artifacts
                if item.artifact_id in scope
            }
        )
        return ResearchReviewEvaluator(self.workspace).prepare(paths)

    def assess_review(
        self, snapshot: ReviewSnapshot, submissions: Sequence[ReviewSubmission]
    ) -> ReviewAssessment:
        """Reuse the independent bounded review verifier."""
        return ResearchReviewEvaluator(self.workspace).assess(snapshot, submissions)

    def experiment_definition(
        self, run: WorkflowRun, *, session_id: str, actor_id: str, invocation: str
    ) -> ExperimentDefinition:
        """Bind a stage to its declared files and gateway Python runtime before dispatch."""
        definition = research_workflow(run.binding.workflow_id)
        stage = definition.stage(run.stage_id)
        artifacts = {item.artifact_id: item for item in definition.artifacts}
        paths = {item.value for item in run.binding.inputs if item.kind == "file"}
        paths.update(
            str(PurePosixPath(run.binding.output_directory) / artifacts[name].relative_path)
            for name in stage.reads
            if name in artifacts
        )
        code_paths = {
            str(PurePosixPath(run.binding.output_directory) / artifacts[name].relative_path)
            for name in stage.reads
            if name in artifacts and artifacts[name].media_type == "text/x-python"
        }
        observed = {path: self._fingerprint(path) for path in sorted(paths)}
        outputs = tuple(
            str(PurePosixPath(run.binding.output_directory) / artifacts[name].relative_path)
            for name in stage.writes
        )
        return ExperimentDefinition(
            actor_ref="sha256:" + _digest(actor_id),
            source="heartwood",
            inputs=tuple(item for path, item in observed.items() if path not in code_paths),
            code=tuple(item for path, item in observed.items() if path in code_paths),
            output_paths=outputs,
            code_output_paths=tuple(
                path
                for name, path in zip(stage.writes, outputs, strict=True)
                if artifacts[name].media_type == "text/x-python"
            ),
            environment=observed_python_environment(),
            parameters_sha256=experiment_digest(run.binding.model_dump(mode="json")),
            invocation_sha256=_digest(invocation),
            stage=ExperimentStage(
                session_id=session_id,
                workflow_run_id=run.run_id,
                stage_id=run.stage_id,
                workflow_sha256=run.binding.workflow_fingerprint,
            ),
        )

    def experiment_outputs(
        self, run: WorkflowRun, evaluation: WorkflowStageEvaluation
    ) -> tuple[ExperimentFile, ...]:
        """Capture only bytes matching the independently checked stage outputs."""
        definition = research_workflow(run.binding.workflow_id)
        expected = {item.artifact_id: item.sha256 for item in evaluation.artifacts}
        outputs = []
        stage = definition.stage(run.stage_id)
        for artifact in definition.artifacts:
            if artifact.artifact_id in stage.writes:
                observed = self._fingerprint(
                    str(PurePosixPath(run.binding.output_directory) / artifact.relative_path)
                )
                if observed.sha256 != expected.get(artifact.artifact_id):
                    raise ValueError("Stage output changed after evaluation")
                outputs.append(observed)
        return tuple(outputs)

    def _fingerprint(self, path: str) -> ExperimentFile:
        return self.workspace.fingerprint(path, max_bytes=self.workspace.limits.max_file_bytes)

    @staticmethod
    def catalog() -> WorkflowCatalog:
        """Describe supported workflows without reading files or constructing a model."""
        supported = supported_research_checks() | _REPRODUCTION_CHECKS
        return WorkflowCatalog(
            workflows=tuple(
                WorkflowCatalogEntry(
                    definition=definition,
                    unavailable_checks=tuple(
                        sorted(
                            {
                                check.evaluator_id
                                for stage in definition.stages
                                for check in stage.checks
                                if check.evaluator_id not in supported
                            }
                        )
                    ),
                )
                for definition in research_workflows()
            )
        )

    def prepare(
        self, workflow_id: str, *, inputs: Mapping[str, str], output_directory: str
    ) -> WorkflowProjectBinding:
        """Bind the exact researcher inputs without writing project files."""
        definition = research_workflow(workflow_id)
        project_relative_path(output_directory, allow_root=False)
        if set(inputs) != {item.input_id for item in definition.inputs}:
            raise ValueError("Supply each declared workflow input exactly once")
        bound = []
        for item in definition.inputs:
            value = inputs[item.input_id]
            content = self._read(value) if item.kind == "file" else value
            if content is None or not content.strip():
                raise ValueError(
                    "A workflow input is empty, unavailable, or exceeds inspection limits"
                )
            bound.append(
                WorkflowBoundInput(
                    input_id=item.input_id, kind=item.kind, value=value, sha256=_digest(content)
                )
            )
        binding = WorkflowProjectBinding(
            workflow_id=definition.workflow_id,
            workflow_fingerprint=definition.fingerprint,
            output_directory=output_directory,
            inputs=tuple(bound),
        )
        self._validate_binding(definition, binding)
        return binding

    def usage(self, events: Sequence[SessionEvent]) -> ExecutionUsage:
        """Reuse the interface's authoritative usage reduction for admission."""
        if not any(event.kind == EventKind.USER_MESSAGE_RECORDED for event in events):
            return ExecutionUsage(
                input_tokens=0,
                output_tokens=0,
                model_calls=0,
                reported_cost_usd=0,
                proposed_actions=0,
                elapsed_seconds=0,
            )
        return project_session(
            tuple(events),
            session_id=events[0].session_id,
        ).execution_usage(elapsed_seconds=0)

    def read_files(self, paths: tuple[str, ...]) -> dict[str, str]:
        """Inspect complete project text through the shared confinement boundary."""
        return {path: content for path in paths if (content := self._read(path)) is not None}

    @property
    def project_directory(self) -> str:
        """Return the authoritative project root used by the agent workspace."""
        return str(self.workspace.project.root)

    def is_absent(self, path: str) -> bool:
        """Require an existing safe parent and an absent final directory entry."""
        return self.workspace.is_absent(path)

    def evaluate(
        self,
        binding: WorkflowProjectBinding,
        stage_id: str,
        *,
        model_status: WorkflowOutcomeStatus | None,
        reproductions: tuple[ReproductionWitness, ...] = (),
    ) -> WorkflowStageEvaluation:
        """Produce read-only stage evidence; missing execution proof remains not run."""
        definition = research_workflow(binding.workflow_id)
        self._validate_binding(definition, binding)
        stage = definition.stage(stage_id)
        values = self._values(definition, binding, stage_id)
        current = tuple(
            WorkflowValueFingerprint(artifact_id=name, sha256=_digest(text))
            for name, text in sorted(values.items())
        )
        checks = []
        for check in stage.checks:
            selected = {name: values[name] for name in check.artifact_ids if name in values}
            status = (
                evaluate_research_check(check.evaluator_id, selected)
                if len(selected) == len(check.artifact_ids)
                else "not_run"
            )
            if check.evaluator_id in _REPRODUCTION_CHECKS:
                spec = workflow_reproduction_spec(binding, stage_id)
                eligible = tuple(
                    witness
                    for witness in reproductions
                    if witness.spec == spec and witness.stage_id == stage_id
                )
                if any(
                    witness.verifies(
                        protected=self.read_files(witness.spec.protected_paths),
                        outputs=self.read_files(witness.spec.output_paths),
                    )
                    for witness in eligible
                ):
                    status = (
                        "passed"
                        if compare_reproduction_artifacts(
                            selected, require_match=check.evaluator_id == "execution.reproduction"
                        )
                        else "failed"
                    )
                elif eligible:
                    status = (
                        "failed" if any(w.status != "prepared" for w in eligible) else "not_run"
                    )
            checks.append(
                WorkflowCheckResult(
                    check_id=check.check_id,
                    evaluator_id=check.evaluator_id,
                    status=status,
                    inspected=tuple(item for item in current if item.artifact_id in selected),
                )
            )
        if self._values(definition, binding, stage_id) != values:
            checks = [check.model_copy(update={"status": "not_run"}) for check in checks]
        assessment = assess_workflow_stage(
            definition,
            stage_id,
            artifacts=current,
            checks=checks,
            model_status=model_status,
            expected_inputs=tuple(
                WorkflowValueFingerprint(artifact_id=item.input_id, sha256=item.sha256)
                for item in binding.inputs
            ),
        )
        return WorkflowStageEvaluation(
            artifacts=current, checks=tuple(checks), assessment=assessment
        )

    def _read(self, path: str) -> str | None:
        try:
            result = self.workspace.file(path)
        except WorkspaceInspectionError:
            return None
        return result["content"] if result["status"] == "available" else None

    def _values(
        self, definition: WorkflowDefinition, binding: WorkflowProjectBinding, stage_id: str
    ) -> dict[str, str]:
        values = {}
        for item in binding.inputs:
            text = self._read(item.value) if item.kind == "file" else item.value
            if text is not None:
                values[item.input_id] = text
        stage = definition.stage(stage_id)
        for artifact in definition.artifacts:
            if artifact.artifact_id in (*stage.reads, *stage.writes):
                text = self._read(
                    str(PurePosixPath(binding.output_directory) / artifact.relative_path)
                )
                if text is not None:
                    values[artifact.artifact_id] = text
        return values

    @staticmethod
    def _validate_binding(definition: WorkflowDefinition, binding: WorkflowProjectBinding) -> None:
        if binding.workflow_fingerprint != definition.fingerprint:
            raise ValueError("Workflow definition changed; prepare a new binding")
        if {item.input_id: item.kind for item in binding.inputs} != {
            item.input_id: item.kind for item in definition.inputs
        }:
            raise ValueError("Workflow binding does not match the declared inputs")
        outputs = [
            PurePosixPath(binding.output_directory.casefold()) / artifact.relative_path.casefold()
            for artifact in definition.artifacts
        ]
        for item in binding.inputs:
            if item.kind == "file":
                path = PurePosixPath(item.value.casefold())
                if any(
                    path == output or path in output.parents or output in path.parents
                    for output in outputs
                ):
                    raise ValueError("Workflow outputs must not overlap input files")


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
