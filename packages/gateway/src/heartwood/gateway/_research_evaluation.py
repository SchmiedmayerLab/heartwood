# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Read-only project binding and evidence production for research stages."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import PurePosixPath

from heartwood.core_adapter.research_checks import evaluate_research_check
from heartwood.core_adapter.research_workflows import research_workflow
from heartwood.core_adapter.workflow_evidence import assess_workflow_stage
from heartwood.gateway._workspace import WorkspaceInspectionError, WorkspaceInspector
from heartwood.schemas.project_paths import project_relative_path
from heartwood.schemas.workflows import (
    WorkflowBoundInput,
    WorkflowCheckResult,
    WorkflowDefinition,
    WorkflowOutcomeStatus,
    WorkflowProjectBinding,
    WorkflowStageEvaluation,
    WorkflowValueFingerprint,
)


class ResearchStageEvaluator:
    """Reuse workspace confinement and core checks without executing an agent or tool."""

    def __init__(self, workspace: WorkspaceInspector) -> None:
        self.workspace = workspace

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

    def evaluate(
        self,
        binding: WorkflowProjectBinding,
        stage_id: str,
        *,
        model_status: WorkflowOutcomeStatus | None,
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
