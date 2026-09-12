# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Evidence gates distinguish research quality from connectivity and test doubles."""

from __future__ import annotations

import traceback
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from heartwood.compliance.evaluation import assess_research_evidence
from heartwood.compliance.evaluation_store import EvaluationStore
from heartwood.persistence import DurableFileError
from heartwood.schemas.evaluation import (
    EvaluationCase,
    EvaluationCheck,
    EvaluationConfiguration,
    EvaluationDimension,
    EvaluationPolicy,
    EvaluationRun,
    EvaluationRuntimeObservation,
    EvaluationSuite,
    RequiredEvaluationCheck,
)
from heartwood.schemas.execution import ExecutionUsage

NOW = datetime(2026, 9, 10, tzinfo=UTC)


def _runtime() -> EvaluationRuntimeObservation:
    return EvaluationRuntimeObservation(
        backend="openhands-sdk",
        source="production",
        request_model="test/model",
        openhands_version="1.46.0",
        model_options_fingerprint="a" * 64,
        platform="generic",
        policy_fingerprint="f" * 64,
        action_confirmation="always-confirm",
        max_input_tokens=32768,
        max_output_tokens=4096,
    )


def _configuration() -> EvaluationConfiguration:
    return EvaluationConfiguration(
        provider="synthetic",
        model="test/model",
        request_model="test/model",
        model_revision="a" * 40,
        platform="generic",
        hardware=("cpu",),
        runtime="llama.cpp-test",
        openhands_version="1.46.0",
        precision="q4",
        context_tokens=32768,
        output_tokens=4096,
        tool_parser="native",
        skill_tree_digest="b" * 64,
        harness_revision="c" * 64,
        runtime_fingerprint=_runtime().fingerprint,
    )


def _suite() -> EvaluationSuite:
    return EvaluationSuite(
        suite_id="synthetic-research.v1",
        cases=tuple(
            EvaluationCase(
                case_id=workflow,
                workflow_id=workflow,
                fixture_digest="d" * 64,
                required_checks=tuple(
                    RequiredEvaluationCheck(check_id=dimension.value, dimension=dimension)
                    for dimension in EvaluationDimension
                ),
            )
            for workflow in ("dataset-readiness", "baseline-analysis", "result-verification")
        ),
    )


def _runs() -> list[EvaluationRun]:
    return [
        EvaluationRun(
            run_id=uuid4(),
            suite_id=_suite().suite_id,
            suite_fingerprint=_suite().fingerprint,
            case_id=case.case_id,
            fixture_digest=case.fixture_digest,
            seed=repeat,
            execution="live_model",
            configuration=_configuration(),
            runtime_observation=_runtime(),
            started_at=NOW - timedelta(minutes=10 - repeat),
            finished_at=NOW - timedelta(minutes=9 - repeat),
            checks=tuple(
                EvaluationCheck(check_id=check.check_id, dimension=check.dimension, status="passed")
                for check in case.required_checks
            ),
            usage=ExecutionUsage(elapsed_seconds=60),
        )
        for case in _suite().cases
        for repeat in range(3)
    ]


def test_repeated_results_are_exactly_scoped_and_order_independent() -> None:
    runs = _runs()
    assessment = assess_research_evidence(
        suite=_suite(),
        configuration=_configuration(),
        policy=EvaluationPolicy(),
        now=NOW,
        runs=runs,
    )
    reordered = assess_research_evidence(
        suite=_suite(),
        configuration=_configuration(),
        policy=EvaluationPolicy(),
        now=NOW,
        runs=list(reversed(runs)),
    )

    assert assessment.qualified
    assert len(assessment.evidence_run_ids) == 9
    assert assessment == reordered
    assert assessment.configuration_fingerprint == _configuration().fingerprint
    assert runs[0].usage.reported_cost_usd is None


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"source": "injected"}, "runtime_not_production"),
        ({"source": "deterministic"}, "runtime_not_production"),
        ({"source": "unconfigured"}, "runtime_not_production"),
        ({"request_model": None}, "runtime_identity_unknown"),
        ({"openhands_version": None}, "runtime_identity_unknown"),
        ({"model_options_fingerprint": None}, "runtime_identity_unknown"),
        ({"max_input_tokens": None}, "runtime_context_unknown"),
        ({"max_output_tokens": None}, "runtime_context_unknown"),
        ({"request_model": "other-model"}, "runtime_declaration_mismatch"),
        ({"openhands_version": "1.41.0"}, "runtime_declaration_mismatch"),
        ({"platform": "terra"}, "runtime_declaration_mismatch"),
        ({"max_input_tokens": 65536}, "runtime_declaration_mismatch"),
        ({"max_output_tokens": 8192}, "runtime_declaration_mismatch"),
    ],
)
def test_bound_but_ineligible_runtime_cannot_qualify(
    changes: dict[str, object], reason: str
) -> None:
    runtime = EvaluationRuntimeObservation.model_validate({**_runtime().model_dump(), **changes})
    configuration = _configuration().model_copy(update={"runtime_fingerprint": runtime.fingerprint})
    runs = [
        run.model_copy(update={"runtime_observation": runtime, "configuration": configuration})
        for run in _runs()
    ]
    result = assess_research_evidence(
        suite=_suite(), configuration=configuration, runs=runs, policy=EvaluationPolicy(), now=NOW
    )
    assert not result.qualified
    assert f"dataset-readiness:{reason}" in result.reasons


@pytest.mark.parametrize("condition", ["missing", "substituted", "unbound"])
def test_runtime_binding_is_required_for_qualification(condition: str) -> None:
    configuration = _configuration()
    runtime: EvaluationRuntimeObservation | None = _runtime()
    if condition == "missing":
        runtime = None
        reason = "dataset-readiness:runtime_unobserved"
    elif condition == "substituted":
        runtime = _runtime().model_copy(update={"policy_fingerprint": "b" * 64})
        reason = "dataset-readiness:runtime_mismatch"
    else:
        configuration = configuration.model_copy(update={"runtime_fingerprint": None})
        reason = "runtime_unbound"
    runs = [
        run.model_copy(update={"runtime_observation": runtime, "configuration": configuration})
        for run in _runs()
    ]
    result = assess_research_evidence(
        suite=_suite(), configuration=configuration, runs=runs, policy=EvaluationPolicy(), now=NOW
    )
    assert not result.qualified
    assert reason in result.reasons


@pytest.mark.parametrize("change", ["hardware", "platform", "context_tokens", "harness_revision"])
def test_evidence_does_not_qualify_a_different_configuration(change: str) -> None:
    data = _configuration().model_dump()
    replacements: dict[str, object] = {
        "hardware": ("other-gpu",),
        "platform": "terra",
        "context_tokens": 65536,
        "harness_revision": "e" * 64,
    }
    data[change] = replacements[change]
    configuration = EvaluationConfiguration.model_validate(data)

    result = assess_research_evidence(
        suite=_suite(),
        configuration=configuration,
        runs=_runs(),
        policy=EvaluationPolicy(),
        now=NOW,
    )

    assert not result.qualified
    assert result.evidence_run_ids == ()


@pytest.mark.parametrize("condition", ["deterministic", "stale", "future", "wrong-fixture"])
def test_nonqualifying_trials_do_not_inflate_evidence(condition: str) -> None:
    runs = _runs()
    updates: dict[str, object] = {
        "deterministic": {"execution": "deterministic"},
        "stale": {"started_at": NOW - timedelta(days=40), "finished_at": NOW - timedelta(days=39)},
        "future": {"finished_at": NOW + timedelta(days=1)},
        "wrong-fixture": {"fixture_digest": "e" * 64},
    }
    update = updates[condition]
    assert isinstance(update, dict)
    runs = [run.model_copy(update=update) for run in runs]

    result = assess_research_evidence(
        suite=_suite(),
        configuration=_configuration(),
        runs=runs,
        policy=EvaluationPolicy(),
        now=NOW,
    )

    assert not result.qualified
    assert result.evidence_run_ids == ()


@pytest.mark.parametrize("status", ["failed", "not_run", "missing", "wrong_dimension"])
def test_latest_incomplete_or_failed_trial_invalidates_previous_passes(status: str) -> None:
    runs = _runs()
    checks = list(runs[0].checks)
    if status == "missing":
        checks.pop()
    elif status == "wrong_dimension":
        checks[-1] = checks[-1].model_copy(update={"dimension": EvaluationDimension.CONNECTIVITY})
    else:
        checks[-1] = checks[-1].model_copy(update={"status": status})
    latest = runs[0].model_copy(
        update={"run_id": uuid4(), "finished_at": NOW, "checks": tuple(checks)}
    )
    runs.append(latest)

    result = assess_research_evidence(
        suite=_suite(),
        configuration=_configuration(),
        runs=runs,
        policy=EvaluationPolicy(),
        now=NOW,
    )

    assert not result.qualified
    assert latest.run_id in result.evidence_run_ids
    assert any("not_passed" in reason for reason in result.reasons)


def test_duplicate_trial_cannot_count_as_repeated_evidence() -> None:
    runs = _runs()
    with pytest.raises(ValueError, match="Duplicate evaluation run"):
        assess_research_evidence(
            suite=_suite(),
            configuration=_configuration(),
            runs=[*runs, runs[0]],
            policy=EvaluationPolicy(),
            now=NOW,
        )


@pytest.mark.parametrize(
    ("field", "limit", "maximum"),
    [
        ("elapsed_seconds", "seconds", 300),
        ("model_calls", "model_calls", 20),
        ("output_tokens", "tokens", 100_000),
        ("reported_cost_usd", "reported_cost_usd", 1),
        ("proposed_actions", "actions", 30),
    ],
)
@pytest.mark.parametrize("overrun", [False, True])
def test_passing_checks_do_not_override_observed_budget_overruns(
    field: str, limit: str, maximum: int, overrun: bool
) -> None:
    runs = _runs()
    usage = ExecutionUsage.model_validate({"elapsed_seconds": 60, field: maximum + int(overrun)})
    runs[0] = runs[0].model_copy(update={"usage": usage})
    result = assess_research_evidence(
        suite=_suite(),
        configuration=_configuration(),
        runs=runs,
        policy=EvaluationPolicy(),
        now=NOW,
    )
    assert result.qualified is not overrun
    assert result.reasons == ((f"dataset-readiness:budget_exceeded:{limit}",) if overrun else ())


def test_unknown_model_revision_is_not_a_qualified_snapshot() -> None:
    configuration = _configuration().model_copy(update={"model_revision": None})
    runs = [run.model_copy(update={"configuration": configuration}) for run in _runs()]
    result = assess_research_evidence(
        suite=_suite(), configuration=configuration, runs=runs, policy=EvaluationPolicy(), now=NOW
    )
    assert result.reasons == ("model_revision_unknown",)
    assert not result.qualified


def test_connectivity_only_suite_cannot_claim_research_quality() -> None:
    case = (
        _suite()
        .cases[0]
        .model_copy(update={"required_checks": _suite().cases[0].required_checks[:1]})
    )
    with pytest.raises(ValidationError, match="every research evidence dimension"):
        EvaluationSuite(suite_id="connection-only", cases=(case,))


@pytest.mark.parametrize("field", ["prompt", "tool_output", "api_key", "participant_data"])
def test_evidence_schema_does_not_accept_raw_execution_content(field: str) -> None:
    payload = _runs()[0].model_dump()
    payload[field] = "Must not be retained"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        EvaluationRun.model_validate(payload)


@pytest.mark.parametrize("value", [-1, float("inf"), float("nan")])
def test_efficiency_measurements_reject_invalid_values(value: float) -> None:
    with pytest.raises(ValidationError):
        ExecutionUsage(elapsed_seconds=value)


def test_evidence_round_trip_preserves_metadata_and_unknown_cost() -> None:
    run = _runs()[0]
    assert EvaluationRun.model_validate_json(run.model_dump_json()) == run
    assert run.usage.reported_cost_usd is None
    with pytest.raises(ValidationError):
        EvaluationPolicy(minimum_repeats=1)


def test_changed_suite_does_not_inherit_old_results() -> None:
    suite = _suite()
    cases = list(suite.cases)
    cases[0] = cases[0].model_copy(update={"workflow_id": "changed-baseline"})
    changed = suite.model_copy(update={"cases": tuple(cases)})
    result = assess_research_evidence(
        suite=changed,
        configuration=_configuration(),
        runs=_runs(),
        policy=EvaluationPolicy(),
        now=NOW,
    )
    assert not result.qualified
    assert result.evidence_run_ids == ()
    assert result.suite_fingerprint == changed.fingerprint


def test_recovered_results_must_replace_the_full_recent_trial_window() -> None:
    runs = _runs()
    failed = runs[0].model_copy(
        update={"run_id": uuid4(), "finished_at": NOW - timedelta(minutes=1), "checks": ()}
    )
    runs.append(failed)
    for seed in range(3):
        runs.append(
            runs[0].model_copy(update={"run_id": uuid4(), "finished_at": NOW, "seed": 10 + seed})
        )
    result = assess_research_evidence(
        suite=_suite(),
        configuration=_configuration(),
        runs=runs,
        policy=EvaluationPolicy(),
        now=NOW,
    )
    assert result.qualified
    assert failed.run_id not in result.evidence_run_ids
    assert result.assessed_at == NOW
    assert result.policy == EvaluationPolicy()


def test_assessment_rejects_an_ambiguous_clock() -> None:
    with pytest.raises(ValueError, match="explicit timezone"):
        assess_research_evidence(
            suite=_suite(),
            configuration=_configuration(),
            runs=_runs(),
            policy=EvaluationPolicy(),
            now=NOW.replace(tzinfo=None),
        )


def test_trial_rejects_duplicate_checks_and_backwards_time() -> None:
    run = _runs()[0]
    for change in (
        {"checks": (*run.checks, run.checks[0])},
        {"finished_at": run.started_at - timedelta(seconds=1)},
    ):
        with pytest.raises(ValidationError):
            EvaluationRun.model_validate({**run.model_dump(), **change})


def _incomplete(run: EvaluationRun) -> EvaluationRun:
    return EvaluationRun.model_validate(
        {
            **run.model_dump(),
            "status": "incomplete",
            "finished_at": run.started_at,
            "usage": {"elapsed_seconds": 0},
            "checks": [{**check.model_dump(), "status": "not_run"} for check in run.checks],
        }
    )


def test_trial_store_retains_pending_evidence_and_finalizes_once(tmp_path: Path) -> None:
    store = EvaluationStore(tmp_path / "evaluations")
    run = _runs()[0]
    pending = _incomplete(run)
    store.begin(pending)

    assert EvaluationStore(store.root).records() == (pending,)
    assert store.root.stat().st_mode & 0o777 == 0o700
    assert (store.root / f"{run.run_id}.json").stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="already exists"):
        store.begin(pending)
    store.complete(run)
    store.complete(run)
    assert EvaluationStore(store.root).records() == (run,)
    changed = run.model_copy(update={"finished_at": NOW})
    with pytest.raises(ValueError, match="cannot be replaced"):
        store.complete(changed)
    assert store.records() == (run,)


@pytest.mark.parametrize(
    "field", ["configuration", "seed", "session_id", "checks", "runtime_observation"]
)
def test_trial_finalization_cannot_substitute_reserved_contract(tmp_path: Path, field: str) -> None:
    run = _runs()[0]
    store = EvaluationStore(tmp_path)
    pending = _incomplete(run)
    store.begin(pending)
    changes: dict[str, object] = {
        "configuration": {**run.configuration.model_dump(), "model": "replacement"},
        "seed": 999,
        "session_id": "other-session",
        "checks": [check.model_dump() for check in run.checks[1:]],
        "runtime_observation": {**_runtime().model_dump(), "policy_fingerprint": "b" * 64},
    }
    changed = EvaluationRun.model_validate({**run.model_dump(), field: changes[field]})

    with pytest.raises(ValueError, match="changed the reserved"):
        store.complete(changed)
    assert store.records() == (pending,)


def test_incomplete_evidence_cannot_claim_passed_checks() -> None:
    with pytest.raises(ValidationError, match="Incomplete evaluation"):
        EvaluationRun.model_validate({**_runs()[0].model_dump(), "status": "incomplete"})


def test_recent_interrupted_trial_invalidates_older_successes(tmp_path: Path) -> None:
    runs = _runs()
    interrupted = _incomplete(
        runs[0].model_copy(update={"run_id": uuid4(), "started_at": NOW, "finished_at": NOW})
    )
    store = EvaluationStore(tmp_path)
    for run in runs:
        store.begin(_incomplete(run))
        store.complete(run)
    store.begin(interrupted)

    result = assess_research_evidence(
        suite=_suite(),
        configuration=_configuration(),
        runs=store.records(),
        policy=EvaluationPolicy(),
        now=NOW,
    )

    assert not result.qualified
    assert interrupted.run_id in result.evidence_run_ids
    assert f"{interrupted.case_id}:incomplete_trial" in result.reasons


@pytest.mark.parametrize("after_replace", [False, True])
def test_interrupted_final_write_retains_a_complete_valid_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, after_replace: bool
) -> None:
    store = EvaluationStore(tmp_path)
    run = _runs()[0]
    pending = _incomplete(run)
    store.begin(pending)
    original = Path.replace

    def interrupt(source: Path, target: Path) -> Path:
        if after_replace:
            original(source, target)
        raise OSError("synthetic interruption")

    monkeypatch.setattr(Path, "replace", interrupt)
    with pytest.raises(OSError, match="synthetic interruption"):
        store.complete(run)
    monkeypatch.setattr(Path, "replace", original)

    assert EvaluationStore(tmp_path).records() == ((run,) if after_replace else (pending,))
    store.complete(run)
    assert store.records() == (run,)


@pytest.mark.parametrize("damage", ["malformed", "renamed", "other-suffix", "symlink"])
def test_trial_loader_never_silently_drops_damaged_evidence(tmp_path: Path, damage: str) -> None:
    store = EvaluationStore(tmp_path)
    pending = _incomplete(_runs()[0])
    store.begin(pending)
    path = tmp_path / f"{pending.run_id}.json"
    if damage == "malformed":
        path.write_text("{broken")
    elif damage == "renamed":
        path.rename(tmp_path / f"{uuid4()}.json")
    elif damage == "other-suffix":
        path.rename(path.with_suffix(".bak"))
    else:
        saved = tmp_path / "saved"
        path.rename(saved)
        path.symlink_to(saved)

    with pytest.raises((ValueError, DurableFileError)):
        store.records()


def test_invalid_report_diagnostic_does_not_disclose_record_contents(tmp_path: Path) -> None:
    store = EvaluationStore(tmp_path)
    run = _incomplete(_runs()[0])
    store.begin(run)
    path = tmp_path / f"{run.run_id}.json"
    path.write_text('{"unexpected":"synthetic-private-canary"}')

    with pytest.raises(ValueError, match="record is invalid") as caught:
        store.records()
    assert "synthetic-private-canary" not in "".join(traceback.format_exception(caught.value))


def test_abandoned_atomic_temporary_does_not_hide_pending_trial(tmp_path: Path) -> None:
    store = EvaluationStore(tmp_path)
    run = _incomplete(_runs()[0])
    store.begin(run)
    (tmp_path / f".{run.run_id}.json-abcdefgh").write_text("incomplete temporary write")

    assert store.records() == (run,)


def test_renaming_a_trial_to_temporary_format_does_not_remove_its_evidence(tmp_path: Path) -> None:
    store = EvaluationStore(tmp_path)
    run = _incomplete(_runs()[0])
    store.begin(run)
    (tmp_path / f"{run.run_id}.json").rename(tmp_path / f".{run.run_id}.json-abcdefgh")

    with pytest.raises(ValueError, match="Orphaned"):
        store.records()
