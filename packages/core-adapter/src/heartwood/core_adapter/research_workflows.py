# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""First-party research task definitions, independent of model and platform routing."""

from pathlib import PurePosixPath

from heartwood.core_adapter.reproduction import ReproductionSpec
from heartwood.schemas.execution import ExecutionBudget
from heartwood.schemas.workflows import (
    WorkflowArtifact,
    WorkflowCheck,
    WorkflowDefinition,
    WorkflowInput,
    WorkflowProjectBinding,
    WorkflowStage,
)

_DATA = (
    WorkflowInput(
        input_id="data",
        label="Dataset",
        kind="file",
        description="Project-local tabular data to inspect without modifying it.",
    ),
    WorkflowInput(
        input_id="dictionary",
        label="Data Dictionary",
        kind="file",
        description="Column meanings, valid values, identifiers, and timing information.",
    ),
)
_BUDGET = ExecutionBudget(
    maximum_seconds=1800,
    maximum_model_calls=80,
    maximum_tokens=400_000,
    maximum_reported_cost_usd=4,
    maximum_actions=100,
)
_REPRODUCTION_ARTIFACTS = (
    WorkflowArtifact(
        artifact_id="verification",
        label="Verification Results",
        relative_path="verification.json",
        media_type="application/json",
    ),
    WorkflowArtifact(
        artifact_id="reproduced-metrics",
        label="Reproduced Metrics",
        relative_path="reproduced/metrics.json",
        media_type="application/json",
    ),
    WorkflowArtifact(
        artifact_id="reproduced-predictions",
        label="Reproduced Predictions",
        relative_path="reproduced/predictions.csv",
        media_type="text/csv",
    ),
)


def research_workflows() -> tuple[WorkflowDefinition, ...]:
    """Return maintained definitions without provisioning tools, models, or specialists."""
    return (_readiness(), _baseline(), _verification())


def research_workflow(workflow_id: str) -> WorkflowDefinition:
    """Select a known definition; unknown identifiers never fall back to another task."""
    for definition in research_workflows():
        if definition.workflow_id == workflow_id:
            return definition
    raise ValueError("Unknown research workflow")


def workflow_reproduction_spec(
    binding: WorkflowProjectBinding, stage_id: str
) -> ReproductionSpec | None:
    """Derive reproduction instructions from bound inputs and declared stage artifacts."""
    definition = research_workflow(binding.workflow_id)
    if definition.fingerprint != binding.workflow_fingerprint:
        raise ValueError("Workflow definition changed; prepare a new binding")
    stage = definition.stage(stage_id)
    if not any(
        check.evaluator_id in {"execution.reproduction", "execution.comparison"}
        for check in stage.checks
    ):
        return None
    files = {item.input_id: item.value for item in binding.inputs if item.kind == "file"}
    paths = {
        **files,
        **{
            artifact.artifact_id: binding.artifact_path(artifact.artifact_id)
            for artifact in definition.artifacts
        },
    }
    protected = set(files.values())
    for previous in definition.stages:
        if previous.stage_id == stage_id:
            break
        protected.update(paths[name] for name in previous.writes)
    outputs = tuple(
        PurePosixPath(paths[name]) for name in ("reproduced-metrics", "reproduced-predictions")
    )
    if outputs[0].parent != outputs[1].parent:
        raise ValueError("Reproduction outputs require one dedicated directory")
    return ReproductionSpec(
        program=paths["program"],
        data=paths["data"],
        directory=str(outputs[0].parent),
        protected_paths=tuple(sorted(protected)),
        output_names=tuple(path.name for path in outputs),
    )


def _readiness() -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_id="dataset-readiness",
        version=1,
        label="Dataset Readiness Review",
        description="Inspect data quality and identify unresolved analysis risks before modeling.",
        inputs=_DATA,
        budget=_BUDGET,
        artifacts=(
            WorkflowArtifact(
                artifact_id="readiness",
                label="Readiness Results",
                relative_path="readiness.json",
                media_type="application/json",
            ),
            WorkflowArtifact(
                artifact_id="report",
                label="Readiness Report",
                relative_path="readiness.md",
                media_type="text/markdown",
            ),
        ),
        stages=(
            WorkflowStage(
                stage_id="inspect",
                label="Inspect Data",
                specialist_ids=("statistical-reviewer",),
                reads=("data", "dictionary"),
                writes=("readiness",),
                reviewer_gate="none",
                instruction="Inspect schema, metadata, missingness, validity, duplicates, temporal "
                "consistency, group balance, and leakage. Keep inputs unchanged. Record aggregate "
                "findings and whether analysis is ready; do not silently discard observations.",
                checks=(
                    WorkflowCheck(
                        check_id="readiness-results",
                        evaluator_id="research.readiness",
                        description="Verify aggregate counts and data-quality findings.",
                        artifact_ids=("data", "dictionary", "readiness"),
                    ),
                ),
            ),
            WorkflowStage(
                stage_id="report",
                label="Review Findings",
                reads=("readiness",),
                writes=("report",),
                reviewer_gate="researcher",
                instruction="Explain findings, limitations, and the decisions required before "
                "analysis. An unresolved data problem is a valid readiness finding, not permission "
                "to alter data or claim that analysis is ready.",
                checks=(
                    WorkflowCheck(
                        check_id="readiness-report",
                        evaluator_id="artifact.nonempty",
                        description="Require a readable report alongside the structured findings.",
                        artifact_ids=("report",),
                    ),
                ),
                specialist_ids=("research-planner",),
            ),
        ),
    )


def _baseline() -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_id="baseline-analysis",
        version=1,
        label="Reproducible Baseline Analysis",
        description="Plan, execute, reproduce, and report a baseline with stated limitations.",
        inputs=(
            *_DATA,
            WorkflowInput(
                input_id="question",
                label="Research Question",
                kind="text",
                description="The question, outcome, population, and intended use of the analysis.",
            ),
        ),
        budget=_BUDGET,
        artifacts=(
            WorkflowArtifact(
                artifact_id="plan",
                label="Analysis Plan",
                relative_path="plan.json",
                media_type="application/json",
            ),
            WorkflowArtifact(
                artifact_id="program",
                label="Analysis Code",
                relative_path="analysis.py",
                media_type="text/x-python",
            ),
            WorkflowArtifact(
                artifact_id="metrics",
                label="Metrics",
                relative_path="metrics.json",
                media_type="application/json",
            ),
            WorkflowArtifact(
                artifact_id="predictions",
                label="Predictions",
                relative_path="predictions.csv",
                media_type="text/csv",
            ),
            *_REPRODUCTION_ARTIFACTS,
            WorkflowArtifact(
                artifact_id="report",
                label="Analysis Report",
                relative_path="report.md",
                media_type="text/markdown",
            ),
        ),
        stages=(
            WorkflowStage(
                stage_id="plan",
                label="Plan Analysis",
                reads=("data", "dictionary", "question"),
                writes=("plan",),
                skill_ids=("baseline-model",),
                specialist_ids=("research-planner", "statistical-reviewer"),
                instruction="Define the question, estimand, outcome, features, split, assumptions, "
                "diagnostics, sensitivity checks, and limitations. Respect group and time "
                "boundaries, "
                "exclude outcome-derived features, and obtain researcher approval before fitting.",
                checks=(
                    WorkflowCheck(
                        check_id="analysis-plan",
                        evaluator_id="research.analysis-plan",
                        description="Verify the plan against the data dictionary.",
                        artifact_ids=("data", "dictionary", "question", "plan"),
                    ),
                ),
            ),
            WorkflowStage(
                stage_id="execute",
                label="Run Baseline",
                specialist_ids=("statistical-reviewer",),
                reads=("data", "dictionary", "plan"),
                writes=("program", "metrics", "predictions"),
                reviewer_gate="none",
                skill_ids=("baseline-model",),
                instruction="Implement the approved baseline with explicit inputs and outputs, "
                "parameters and seeds. Use reviewed tools. Report held-out diagnostics, "
                "a simple comparator, and sensitivity checks. Preserve the original data and plan.",
                checks=(
                    WorkflowCheck(
                        check_id="analysis-program",
                        evaluator_id="python.syntax",
                        description="Require parseable analysis source.",
                        artifact_ids=("program",),
                    ),
                    WorkflowCheck(
                        check_id="analysis-results",
                        evaluator_id="research.baseline",
                        description="Check predictions, metrics, split, and sensitivity results.",
                        artifact_ids=("data", "dictionary", "plan", "metrics", "predictions"),
                    ),
                ),
            ),
            WorkflowStage(
                stage_id="verify",
                label="Verify Reproduction",
                reads=("data", "program", "metrics", "predictions"),
                writes=("verification", "reproduced-metrics", "reproduced-predictions"),
                reviewer_gate="none",
                specialist_ids=("reproducibility-reviewer",),
                instruction="Re-execute the unchanged analysis in a fresh process and output "
                "directory. Compare outputs independently. Record discrepancies; do not repair "
                "or overwrite the primary results to manufacture agreement.",
                checks=(
                    WorkflowCheck(
                        check_id="analysis-reproduction",
                        evaluator_id="execution.reproduction",
                        description="Verify unchanged inputs, re-execution, and matching outputs.",
                        artifact_ids=(
                            "data",
                            "program",
                            "metrics",
                            "predictions",
                            "verification",
                            "reproduced-metrics",
                            "reproduced-predictions",
                        ),
                    ),
                ),
            ),
            WorkflowStage(
                stage_id="report",
                label="Review Analysis",
                reads=("plan", "metrics", "verification"),
                writes=("report",),
                instruction="Summarize the question, methods, held-out results, diagnostic and "
                "sensitivity findings, reproduction status, and limitations. Separate supported "
                "findings from hypotheses and do not imply clinical or scientific validation.",
                checks=(
                    WorkflowCheck(
                        check_id="analysis-report",
                        evaluator_id="artifact.nonempty",
                        description="Require the final report for researcher review.",
                        artifact_ids=("report",),
                    ),
                ),
            ),
        ),
    )


def _verification() -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_id="result-verification",
        version=1,
        label="Independent Result Verification",
        description="Reconstruct an analysis environment and report whether its outputs reproduce.",
        inputs=(
            WorkflowInput(
                input_id="data",
                label="Dataset",
                kind="file",
                description="The unchanged project-local input dataset.",
            ),
            WorkflowInput(
                input_id="program",
                label="Analysis Code",
                kind="file",
                description="The original program to execute without modification.",
            ),
            WorkflowInput(
                input_id="metrics",
                label="Original Metrics",
                kind="file",
                description="Original metrics for independent comparison.",
            ),
            WorkflowInput(
                input_id="predictions",
                label="Original Predictions",
                kind="file",
                description="Original predictions for independent comparison.",
            ),
            WorkflowInput(
                input_id="environment",
                label="Environment Record",
                kind="file",
                description="Recorded runtime, dependencies, parameters, and seeds.",
            ),
        ),
        budget=_BUDGET,
        artifacts=(
            WorkflowArtifact(
                artifact_id="environment-check",
                label="Environment Comparison",
                relative_path="environment-check.json",
                media_type="application/json",
            ),
            *_REPRODUCTION_ARTIFACTS,
            WorkflowArtifact(
                artifact_id="report",
                label="Verification Report",
                relative_path="verification.md",
                media_type="text/markdown",
            ),
        ),
        stages=(
            WorkflowStage(
                stage_id="environment",
                label="Check Environment",
                reads=("environment", "program"),
                writes=("environment-check",),
                instruction="Compare the available runtime and dependencies to the supplied "
                "environment record. Propose any required setup through normal action review. "
                "Report missing dependencies instead of silently changing the analysis.",
                checks=(
                    WorkflowCheck(
                        check_id="verification-environment",
                        evaluator_id="execution.environment",
                        description="Compare the declared and observed environments.",
                        artifact_ids=("environment", "program", "environment-check"),
                    ),
                ),
            ),
            WorkflowStage(
                stage_id="reproduce",
                label="Re-execute Analysis",
                reads=("data", "program", "metrics", "predictions", "environment-check"),
                writes=("verification", "reproduced-metrics", "reproduced-predictions"),
                reviewer_gate="none",
                instruction="Re-execute with recorded parameters in a fresh process and output "
                "directory. Compare regenerated outputs without modifying originals. A discrepancy "
                "is a valid verification result and must be reported, not silently corrected.",
                checks=(
                    WorkflowCheck(
                        check_id="verification-execution",
                        evaluator_id="execution.comparison",
                        description="Verify execution and the artifact comparison.",
                        artifact_ids=(
                            "data",
                            "program",
                            "metrics",
                            "predictions",
                            "verification",
                            "reproduced-metrics",
                            "reproduced-predictions",
                        ),
                    ),
                ),
            ),
            WorkflowStage(
                stage_id="report",
                label="Review Reproduction",
                reads=("environment-check", "verification"),
                writes=("report",),
                specialist_ids=("reproducibility-reviewer",),
                instruction="Report environment differences, re-execution evidence, matching "
                "outputs, discrepancies, and unresolved limitations for researcher review.",
                checks=(
                    WorkflowCheck(
                        check_id="verification-report",
                        evaluator_id="artifact.nonempty",
                        description="Require an explanation of reproduction findings.",
                        artifact_ids=("report",),
                    ),
                ),
            ),
        ),
    )
