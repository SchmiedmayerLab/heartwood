# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Research stages use project-bound evidence, not benchmark answers or model claims."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from pathlib import Path

import pytest

from heartwood.core_adapter.research_checks import evaluate_research_check
from heartwood.core_adapter.research_workflows import workflow_reproduction_spec
from heartwood.core_adapter.workflow_runtime import workflow_stage_prompt
from heartwood.gateway import ProjectContext, SessionGateway
from heartwood.gateway._research_evaluation import ResearchStageEvaluator
from heartwood.schemas import WorkspaceFileResponse
from heartwood.schemas.review import (
    ResearchReviewRun,
    ReviewCandidate,
    ReviewProposals,
    ReviewSubmission,
)
from heartwood.schemas.workflows import WorkflowRun


def _values() -> dict[str, str]:
    return {
        "data": "person,visit,signal,target,partition\n"
        "a,1,1,3,fit\na,2,2,5,fit\nb,1,3,7,fit\nb,2,4,9,fit\n"
        "c,1,5,11,holdout\nd,1,6,13,holdout\n",
        "dictionary": json.dumps(
            {
                "grouping_key": "person",
                "primary_key": ["person", "visit"],
                "outcome": "target",
                "permitted_predictors": ["signal"],
                "excluded_predictors": {"post_outcome": "Measured later"},
                "split": {"column": "partition", "train": "fit", "test": "holdout"},
                "validity": {"signal": {"minimum": 0}},
            }
        ),
        "question": "  Does signal predict the target in held-out people?\n",
        "plan": json.dumps(
            {
                "question": "Does signal predict the target in held-out people?",
                "estimand": "Held-out visit prediction error",
                "outcome": "target",
                "features": ["signal"],
                "group_column": "person",
                "split_column": "partition",
                "assumptions": ["Prespecified group-disjoint split"],
                "limitations": ["Small synthetic sample"],
            }
        ),
        "metrics": json.dumps(
            {
                "outcome": "target",
                "features": ["signal"],
                "n_train": 4,
                "n_test": 2,
                "train_subjects": 2,
                "test_subjects": 2,
                "intercept": 1.0,
                "slope": 2.0,
                "test_rmse": 0.0,
                "test_mae": 0.0,
                "test_r2": 1.0,
                "mean_baseline_rmse": math.sqrt(37),
                "sensitivity_rmse": {"a": 0.0, "b": 0.0},
            }
        ),
        "predictions": "person,visit,prediction\nc,1,11\nd,1,13\n",
        "program": "raise RuntimeError('This source must not execute during a syntax check')\n",
        "readiness": json.dumps(
            {
                "row_count": 6,
                "subject_count": 4,
                "duplicate_rows": 0,
                "missing_by_column": {},
                "invalid_by_column": {},
                "arm_counts": {},
                "leakage_columns": [],
                "ready_for_analysis": True,
            }
        ),
        "report": "# Analysis\nSynthetic results require interpretation and independent review.\n",
    }


@pytest.mark.parametrize(
    ("damage", "expected"),
    [
        ("none", "rejected"),
        ("metric", "verified"),
        ("prediction", "verified"),
        ("malformed-metrics", "unavailable"),
        ("malformed-data", "unavailable"),
        ("unsupported-plan", "unavailable"),
        ("constant-predictor", "unavailable"),
        ("overlapping-groups", "unavailable"),
    ],
)
def test_statistical_review_separates_inconsistent_outputs_from_unavailable_checks(
    tmp_path: Path, damage: str, expected: str
) -> None:
    values = _values()
    if damage == "metric":
        values["metrics"] = json.dumps({**json.loads(values["metrics"]), "test_rmse": 100})
    elif damage == "prediction":
        values["predictions"] = values["predictions"].replace("c,1,11", "c,1,12")
    elif damage == "malformed-metrics":
        values["metrics"] = "not JSON"
    elif damage == "malformed-data":
        values["data"] = "a,b\nwrong-shape\n"
    elif damage == "unsupported-plan":
        values["plan"] = json.dumps({**json.loads(values["plan"]), "features": ["post_outcome"]})
    elif damage == "constant-predictor":
        values["data"] = (
            values["data"]
            .replace("a,2,2", "a,2,1")
            .replace("b,1,3", "b,1,1")
            .replace("b,2,4", "b,2,1")
        )
    elif damage == "overlapping-groups":
        values["data"] = values["data"].replace("c,1,5,11", "a,3,5,11")
    artifacts = {
        name: f"{name}.txt" for name in ("data", "dictionary", "plan", "metrics", "predictions")
    }
    for name, path in artifacts.items():
        (tmp_path / path).write_text(values[name])
    gateway = SessionGateway(project=ProjectContext(tmp_path))
    snapshot = gateway.prepare_research_review(artifacts)
    submission = ReviewSubmission(
        review_id="statistical-review-1",
        reviewer_id="statistical-reviewer",
        snapshot_sha256=snapshot.fingerprint,
        candidates=(
            ReviewCandidate(
                candidate_id="metrics-wrong",
                condition="baseline-result-inconsistent",
                category="statistical",
                severity="critical",
                summary="There is definitely data leakage.",
                artifact_ids=("metrics", "predictions"),
            ),
        ),
    )
    finding = gateway.assess_research_review(snapshot, [submission]).findings[0]
    assert finding.verification == expected
    assert finding.verified_claim == (
        "The baseline results disagree with the independently recomputed analysis."
        if expected == "verified"
        else None
    )
    assert finding.disposition == ("open" if expected == "verified" else "not_actionable")
    assert {item.artifact_id for item in finding.evidence} == artifacts.keys()
    assert not (tmp_path / ".heartwood").exists()


@pytest.mark.parametrize(
    "evaluator", ["research.analysis-plan", "research.baseline", "research.readiness"]
)
def test_declared_data_roles_work_without_fixture_column_names(evaluator: str) -> None:
    assert evaluate_research_check(evaluator, _values()) == "passed"


def test_nonperfect_baseline_matches_independently_calculated_metrics() -> None:
    values = _values()
    values["data"] = (
        values["data"]
        .replace("a,1,1,3,fit", "a,1,1,2,fit")
        .replace("b,1,3,7,fit", "b,1,3,8,fit")
        .replace("b,2,4,9,fit", "b,2,4,8,fit")
    )
    metrics = json.loads(values["metrics"])
    metrics.update(
        {
            "intercept": 0.5,
            "slope": 2.1,
            "test_rmse": math.sqrt(0.005),
            "test_mae": 0.05,
            "test_r2": 0.995,
            "mean_baseline_rmse": math.sqrt(40.0625),
            "sensitivity_rmse": {"a": math.sqrt(17), "b": math.sqrt(12.5)},
        }
    )
    values["metrics"] = json.dumps(metrics)
    values["predictions"] = "person,visit,prediction\nd,1,13.1\nc,1,11\n"
    assert evaluate_research_check("research.baseline", values) == "passed"
    metrics["test_r2"] = 1.0
    values["metrics"] = json.dumps(metrics)
    assert evaluate_research_check("research.baseline", values) == "failed"


@pytest.mark.parametrize("damage", ["model", "outcome", "group", "split", "duplicate-feature"])
def test_plan_rejects_undeclared_analysis_roles(damage: str) -> None:
    values = _values()
    plan = json.loads(values["plan"])
    changes: dict[str, dict[str, object]] = {
        "model": {"features": ["post_outcome"]},
        "outcome": {"outcome": "signal"},
        "group": {"group_column": "visit"},
        "split": {"split_column": "person"},
        "duplicate-feature": {"features": ["signal", "signal"]},
    }
    values["plan"] = json.dumps({**plan, **changes[damage]})
    assert evaluate_research_check("research.analysis-plan", values) == "failed"
    assert evaluate_research_check("research.baseline", values) == "failed"


@pytest.mark.parametrize(
    "damage", ["overlap", "duplicate-key", "extra-partition", "duplicate-header", "ragged", "empty"]
)
def test_ambiguous_data_cannot_pass_a_plan_or_baseline(damage: str) -> None:
    values = _values()
    text = values["data"]
    changes = {
        "overlap": text.replace("c,1,5,11", "a,3,5,11"),
        "duplicate-key": text.replace("a,2,2,5", "a,1,2,5"),
        "extra-partition": text.replace("b,2,4,9,fit", "b,2,4,9,unknown"),
        "duplicate-header": text.replace("person,visit", "person,person", 1),
        "ragged": text.replace("a,1,1,3,fit", "a,1,1,3"),
        "empty": "",
    }
    values["data"] = changes[damage]
    assert evaluate_research_check("research.analysis-plan", values) == "failed"
    assert evaluate_research_check("research.baseline", values) == "failed"


@pytest.mark.parametrize(
    "damage",
    ["metrics", "sensitivity", "predictions", "duplicate-prediction", "nonfinite", "degenerate"],
)
def test_correctness_is_recomputed_instead_of_trusting_reported_success(damage: str) -> None:
    values = _values()
    if damage in {"metrics", "sensitivity"}:
        metrics = json.loads(values["metrics"])
        metrics.update({"slope": 3.0} if damage == "metrics" else {"sensitivity_rmse": {"a": 0.0}})
        values["metrics"] = json.dumps(metrics)
    elif damage == "predictions":
        values["predictions"] = values["predictions"].replace("c,1,11", "c,1,12")
    elif damage == "duplicate-prediction":
        values["predictions"] = "person,visit,prediction\nc,1,11\nc,1,11\n"
    elif damage == "nonfinite":
        values["data"] = values["data"].replace("a,1,1,3,fit", "a,1,NaN,3,fit")
    else:
        values["data"] = values["data"].replace("d,1,6,13,holdout", "d,1,6,11,holdout")
    assert evaluate_research_check("research.baseline", values) == "failed"


def test_readiness_rejects_claims_that_hide_invalid_data() -> None:
    values = _values()
    values["data"] = values["data"].replace("a,1,1,3,fit", "a,1,-1,3,fit")
    assert evaluate_research_check("research.readiness", values) == "failed"
    report = json.loads(values["readiness"])
    report.update({"invalid_by_column": {"signal": 1}, "ready_for_analysis": False})
    values["readiness"] = json.dumps(report)
    assert evaluate_research_check("research.readiness", values) == "passed"
    assert evaluate_research_check("research.analysis-plan", values) == "failed"
    assert evaluate_research_check("research.baseline", values) == "failed"


@pytest.mark.parametrize("damage", ["bounds", "roles", "predictor", "partitions", "primary-key"])
def test_contradictory_metadata_does_not_produce_valid_evidence(damage: str) -> None:
    values = _values()
    dictionary = json.loads(values["dictionary"])
    changes: dict[str, dict[str, object]] = {
        "bounds": {"validity": {"signal": {"minimum": 10, "maximum": 1}}},
        "roles": {"outcome": "person"},
        "predictor": {"permitted_predictors": ["target"]},
        "partitions": {"split": {"column": "partition", "train": "fit", "test": "fit"}},
        "primary-key": {"primary_key": ["visit"]},
    }
    values["dictionary"] = json.dumps({**dictionary, **changes[damage]})
    for evaluator in ("research.readiness", "research.analysis-plan", "research.baseline"):
        assert evaluate_research_check(evaluator, values) == "failed"


@pytest.mark.parametrize("value", ["", "NaN", "Infinity", "not-a-number"])
def test_baseline_requires_finite_complete_analysis_values(value: str) -> None:
    values = _values()
    values["data"] = values["data"].replace("a,1,1,3,fit", f"a,1,1,{value},fit")
    assert evaluate_research_check("research.analysis-plan", values) == "failed"
    assert evaluate_research_check("research.baseline", values) == "failed"


def test_readiness_checks_declared_categories_and_balance() -> None:
    values = _values()
    dictionary = json.loads(values["dictionary"])
    dictionary["arm_column"] = "partition"
    dictionary["validity"]["partition"] = {"values": ["fit", "holdout"]}
    values["dictionary"] = json.dumps(dictionary)
    report = json.loads(values["readiness"])
    report["arm_counts"] = {"fit": 4, "holdout": 2}
    values["readiness"] = json.dumps(report)
    assert evaluate_research_check("research.readiness", values) == "passed"
    report["arm_counts"] = {"fit": 3, "holdout": 3}
    values["readiness"] = json.dumps(report)
    assert evaluate_research_check("research.readiness", values) == "failed"


def test_tabular_and_sensitivity_work_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    values = _values()
    monkeypatch.setattr("heartwood.core_adapter.research_checks.MAX_SENSITIVITY_GROUPS", 1)
    assert evaluate_research_check("research.baseline", values) == "failed"
    values["data"] = "person,visit,signal,target,partition\n" + "a,1,1,3,fit\n" * 10001
    assert evaluate_research_check("research.readiness", values) == "failed"


def test_static_artifact_checks_do_not_execute_code_or_establish_reproduction() -> None:
    assert evaluate_research_check("python.syntax", {"program": _values()["program"]}) == "passed"
    assert evaluate_research_check("python.syntax", {"program": "def invalid syntax:"}) == "failed"
    assert evaluate_research_check("artifact.nonempty", {"report": "  "}) == "failed"
    assert evaluate_research_check("execution.reproduction", _values()) == "not_run"
    assert evaluate_research_check("not.registered", _values()) == "not_run"
    assert (
        evaluate_research_check("artifact.nonempty", {"report": "a" * (512 * 1024 + 1)}) == "failed"
    )


def _prepare(root: Path) -> tuple[SessionGateway, dict[str, str]]:
    values = _values()
    (root / "data.csv").write_text(values["data"])
    (root / "dictionary.json").write_text(values["dictionary"])
    (root / "results").mkdir()
    for name, artifact in (
        ("plan.json", "plan"),
        ("analysis.py", "program"),
        ("metrics.json", "metrics"),
        ("predictions.csv", "predictions"),
        ("report.md", "report"),
    ):
        (root / "results" / name).write_text(values[artifact])
    return SessionGateway(project=ProjectContext(root), env={}), {
        "data": "data.csv",
        "dictionary": "dictionary.json",
        "question": values["question"],
    }


def test_gateway_binds_inputs_and_checks_plan_and_execution_without_a_model(tmp_path: Path) -> None:
    gateway, inputs = _prepare(tmp_path)
    try:
        binding = gateway.prepare_research_workflow(
            "baseline-analysis", inputs=inputs, output_directory="results"
        )
        assert (
            next(item for item in binding.inputs if item.input_id == "question").value
            == inputs["question"]
        )
        plan = gateway.evaluate_research_stage(binding, "plan", model_status="success")
        assert plan.assessment.evidence_satisfied
        assert plan.assessment.researcher_review_required
        executed = gateway.evaluate_research_stage(binding, "execute", model_status="success")
        assert executed.assessment.evidence_satisfied
        assert all(check.status == "passed" for check in executed.checks)
        assert (
            gateway.evaluate_research_stage(binding, "execute", model_status="success") == executed
        )
        assert "Synthetic results" not in executed.model_dump_json()
        assert "Does signal" not in executed.model_dump_json()
        assert not gateway._services
    finally:
        gateway.stop()


@pytest.mark.parametrize("change", [None, "accepted-stage", "corrected-output", "original-output"])
def test_corrected_bindings_preserve_dependencies_and_drive_the_same_checks(
    tmp_path: Path, change: str | None
) -> None:
    gateway, inputs = _prepare(tmp_path)
    try:
        binding = gateway.prepare_research_workflow(
            "baseline-analysis", inputs=inputs, output_directory="results"
        )
        accepted = gateway.evaluate_research_stage(binding, "plan", model_status="success")
        source = tmp_path / binding.artifact_path("program")
        valid = source.read_text()
        source.write_text("def broken(\n")
        evaluator = ResearchStageEvaluator(gateway.workspace_inspector)
        snapshot = evaluator.prepare_review(binding, "execute")
        submission = ReviewSubmission.associate(
            ReviewProposals(
                candidates=(
                    ReviewCandidate(
                        candidate_id="syntax",
                        condition="python-source-invalid",
                        category="coding",
                        severity="high",
                        summary="Source does not parse.",
                        artifact_ids=("program",),
                    ),
                )
            ),
            review_id="native-review",
            reviewer_id="statistical-reviewer",
            snapshot=snapshot,
        )
        review = ResearchReviewRun(
            review_id="review-one",
            snapshot=snapshot,
            reviewer_ids=("statistical-reviewer",),
            started_sequence=2,
            status="assessed",
            submissions=(submission,),
            assessment=gateway.assess_research_review(snapshot, (submission,)),
        )
        run = WorkflowRun(
            run_id="run",
            revision=3,
            binding=binding,
            stage_id="execute",
            phase="blocked",
            completed=(accepted,),
            created_at=datetime.now(UTC),
            research_review=review,
        )
        correction = gateway.prepare_research_correction(review, output_directory="correction-one")
        destination = tmp_path / correction.outputs[0].path
        destination.parent.mkdir()
        destination.write_text(valid)
        assessment = gateway.assess_research_correction(review, correction)
        if change == "accepted-stage":
            run = run.model_copy(
                update={
                    "completed": (
                        *run.completed,
                        gateway.evaluate_research_stage(binding, "execute", model_status="success"),
                    )
                }
            )
        elif change == "corrected-output":
            destination.write_text("different(\n")
        elif change == "original-output":
            source.write_text(valid)
        if change is not None:
            with pytest.raises(ValueError, match=r"accepted|changed|evidence"):
                evaluator.correction_binding(run, correction, assessment)
            return
        corrected = evaluator.correction_binding(run, correction, assessment)
        assert corrected.artifact_path("program") == correction.outputs[0].path
        assert corrected.inputs == binding.inputs
        assert {
            item.artifact_id: item.path
            for item in corrected.artifacts
            if item.artifact_id != "program"
        } == {
            item.artifact_id: item.path
            for item in binding.artifacts
            if item.artifact_id != "program"
        }
        assert run.binding == binding
        assert source.read_text() == "def broken(\n"
        assert evaluator.evaluate(corrected, "plan", model_status="success") == accepted
        assert evaluator.evaluate(
            corrected, "execute", model_status="success"
        ).assessment.evidence_satisfied
        assert not evaluator.evaluate(
            binding, "execute", model_status="success"
        ).assessment.evidence_satisfied
        candidate_run = run.model_copy(update={"binding": corrected})
        assert corrected.artifact_path("program") in workflow_stage_prompt(candidate_run)
        reproduction = workflow_reproduction_spec(corrected, "verify")
        assert reproduction is not None
        assert reproduction.program == corrected.artifact_path("program")
        definition = evaluator.experiment_definition(
            candidate_run,
            session_id="research",
            actor_id="researcher",
            invocation="Synthetic stage",
        )
        assert definition.code_output_paths == (corrected.artifact_path("program"),)
        from heartwood.cli._interactive import format_workflow_artifact_lines
        from heartwood.gateway._session_projection import project_session
        from heartwood.notebook import build_view_model, build_widget_spec

        assert any(
            corrected.artifact_path("program") in line
            for line in format_workflow_artifact_lines(corrected)
        )
        view = project_session((), session_id="research").model_copy(
            update={"workflow": candidate_run}
        )
        artifacts = next(
            item
            for item in build_widget_spec(build_view_model(view))
            if item.title == "Analysis Artifacts"
        )
        assert f"program: {corrected.artifact_path('program')}" in artifacts.items
    finally:
        gateway.stop()


@pytest.mark.parametrize(
    "damage", ["missing", "unknown", "duplicate", "input", "private", "parent"]
)
def test_invalid_bound_paths_are_rejected_before_inspection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    gateway, inputs = _prepare(tmp_path)
    try:
        binding = gateway.prepare_research_workflow(
            "baseline-analysis", inputs=inputs, output_directory="results"
        )
        artifacts = list(binding.artifacts)
        if damage == "missing":
            artifacts.pop()
        elif damage == "unknown":
            artifacts[0] = artifacts[0].model_copy(update={"artifact_id": "not-declared"})
        elif damage == "duplicate":
            artifacts.append(artifacts[0])
        else:
            path = {"input": "data.csv", "private": ".heartwood/secrets", "parent": "results"}[
                damage
            ]
            artifacts[0] = artifacts[0].model_copy(update={"path": path})
        binding = binding.model_copy(update={"artifacts": tuple(artifacts)})

        def unexpected_read(*_args: object, **_kwargs: object) -> object:
            pytest.fail("Invalid artifact bindings must fail before reading files")

        monkeypatch.setattr(gateway.workspace_inspector, "file", unexpected_read)
        with pytest.raises(ValueError, match=r"artifacts|unique|overlap|private"):
            gateway.evaluate_research_stage(binding, "execute", model_status="success")
    finally:
        gateway.stop()


@pytest.mark.parametrize(
    "damage",
    [
        "input-changed",
        "input-missing",
        "output-missing",
        "model-failed",
        "definition-changed",
        "input-symlink",
        "output-symlink",
    ],
)
def test_stage_cannot_pass_missing_changed_or_untrusted_evidence(
    tmp_path: Path, damage: str
) -> None:
    gateway, inputs = _prepare(tmp_path)
    try:
        binding = gateway.prepare_research_workflow(
            "baseline-analysis", inputs=inputs, output_directory="results"
        )
        if damage == "definition-changed":
            binding = binding.model_copy(update={"workflow_fingerprint": "a" * 64})
            with pytest.raises(ValueError, match="definition changed"):
                gateway.evaluate_research_stage(binding, "plan", model_status="success")
            return
        if damage == "input-changed":
            (tmp_path / "data.csv").write_text(_values()["data"] + "\n")
        elif damage == "input-missing":
            (tmp_path / "data.csv").unlink()
        elif damage == "output-missing":
            (tmp_path / "results/plan.json").unlink()
        elif damage.endswith("symlink"):
            path = tmp_path / ("data.csv" if damage == "input-symlink" else "results/plan.json")
            path.unlink()
            path.symlink_to(tmp_path / "dictionary.json")
        result = gateway.evaluate_research_stage(
            binding, "plan", model_status="failed" if damage == "model-failed" else "success"
        )
        assert not result.assessment.evidence_satisfied
    finally:
        gateway.stop()


def test_changes_during_inspection_invalidate_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway, inputs = _prepare(tmp_path)
    try:
        binding = gateway.prepare_research_workflow(
            "baseline-analysis", inputs=inputs, output_directory="results"
        )
        original = gateway.workspace_inspector.file
        reads = 0

        def changing_file(path: str) -> WorkspaceFileResponse:
            nonlocal reads
            result = original(path)
            if path == "results/plan.json":
                reads += 1
                if reads == 1:
                    (tmp_path / path).write_text("{}")
            return result

        monkeypatch.setattr(gateway.workspace_inspector, "file", changing_file)
        result = gateway.evaluate_research_stage(binding, "plan", model_status="success")
        assert not result.assessment.evidence_satisfied
        assert all(check.status == "not_run" for check in result.checks)
    finally:
        gateway.stop()


@pytest.mark.parametrize(
    "output", ["../elsewhere", ".heartwood/private", "results/../data.csv", "data.csv", "DATA.CSV"]
)
def test_binding_rejects_private_paths_and_input_output_overlap(
    tmp_path: Path, output: str
) -> None:
    gateway, inputs = _prepare(tmp_path)
    try:
        with pytest.raises(ValueError, match=r"normalized|private project|overlap"):
            gateway.prepare_research_workflow(
                "baseline-analysis", inputs=inputs, output_directory=output
            )
    finally:
        gateway.stop()


@pytest.mark.parametrize(
    "damage", ["missing", "extra", "traversal", "private", "oversized", "binary", "symlink"]
)
def test_input_preparation_fails_before_any_model_or_tool_work(tmp_path: Path, damage: str) -> None:
    gateway, inputs = _prepare(tmp_path)
    try:
        if damage == "missing":
            inputs.pop("data")
        elif damage == "extra":
            inputs["undeclared"] = "data.csv"
        elif damage == "traversal":
            inputs["data"] = "../outside.csv"
        elif damage == "private":
            inputs["data"] = ".heartwood/config.json"
        elif damage == "oversized":
            (tmp_path / "data.csv").write_text("a" * (512 * 1024 + 1))
        elif damage == "binary":
            (tmp_path / "data.csv").write_bytes(b"\x00\xff")
        else:
            (tmp_path / "data.csv").unlink()
            (tmp_path / "data.csv").symlink_to(tmp_path / "dictionary.json")
        with pytest.raises(ValueError, match=r"declared workflow input|unavailable"):
            gateway.prepare_research_workflow(
                "baseline-analysis", inputs=inputs, output_directory="results"
            )
        assert not gateway._services
    finally:
        gateway.stop()


def test_missing_output_directory_and_matching_copies_are_not_execution_evidence(
    tmp_path: Path,
) -> None:
    gateway, inputs = _prepare(tmp_path)
    try:
        binding = gateway.prepare_research_workflow(
            "baseline-analysis", inputs=inputs, output_directory="missing"
        )
        assert not gateway.evaluate_research_stage(
            binding, "plan", model_status="success"
        ).assessment.evidence_satisfied
        binding = gateway.prepare_research_workflow(
            "baseline-analysis", inputs=inputs, output_directory="results"
        )
        (tmp_path / "results/reproduced").mkdir()
        for name in ("metrics.json", "predictions.csv"):
            (tmp_path / "results/reproduced" / name).write_bytes(
                (tmp_path / "results" / name).read_bytes()
            )
        (tmp_path / "results/verification.json").write_text(
            json.dumps(
                {
                    "status": "reproduced",
                    "matching_artifacts": ["metrics.json", "predictions.csv"],
                    "mismatched_artifacts": [],
                }
            )
        )
        result = gateway.evaluate_research_stage(binding, "verify", model_status="success")
        assert not result.assessment.evidence_satisfied
        assert result.checks[0].status == "not_run"
    finally:
        gateway.stop()
