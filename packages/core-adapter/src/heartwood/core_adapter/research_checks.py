# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Bounded tabular checks, independent of benchmark fixtures and generated programs."""

from __future__ import annotations

import ast
import csv
import io
import math
from collections import Counter
from collections.abc import Mapping
from statistics import linear_regression, mean
from typing import Literal

from heartwood.schemas.research import (
    AnalysisPlan,
    BaselineResult,
    ReadinessResult,
    ResearchDictionary,
    ResultVerification,
)

MAX_RESEARCH_TEXT_BYTES = 512 * 1024
MAX_RESEARCH_ROWS = 10_000
MAX_SENSITIVITY_GROUPS = 128


def compare_reproduction_artifacts(artifacts: Mapping[str, str], *, require_match: bool) -> bool:
    """Check an honest byte comparison; this function does not establish execution."""
    try:
        if any(len(text.encode("utf-8")) > MAX_RESEARCH_TEXT_BYTES for text in artifacts.values()):
            return False
        reported = ResultVerification.model_validate_json(artifacts["verification"])
        matching: list[str] = []
        mismatched: list[str] = []
        for name, filename in (("metrics", "metrics.json"), ("predictions", "predictions.csv")):
            target = matching if artifacts[name] == artifacts[f"reproduced-{name}"] else mismatched
            target.append(filename)
        return (
            sorted(reported.matching_artifacts) == sorted(matching)
            and sorted(reported.mismatched_artifacts) == sorted(mismatched)
            and reported.status == ("discrepancy" if mismatched else "reproduced")
            and (not require_match or not mismatched)
        )
    except (ValueError, KeyError):
        return False


def evaluate_research_check(
    evaluator_id: str, artifacts: Mapping[str, str]
) -> Literal["passed", "failed", "not_run"]:
    """Evaluate supplied bounded text; never read files or execute generated code.

    Execution reproduction needs a gateway-owned execution witness, not matching
    text. Unsupported evaluators cannot become successful checks by default.
    """
    try:
        if any(len(text.encode("utf-8")) > MAX_RESEARCH_TEXT_BYTES for text in artifacts.values()):
            return "failed"
        match evaluator_id:
            case "artifact.nonempty":
                passed = bool(artifacts) and all(text.strip() for text in artifacts.values())
            case "python.syntax":
                passed = bool(artifacts) and all(
                    ast.parse(text).body for text in artifacts.values()
                )
            case "research.analysis-plan":
                passed = _valid_plan(artifacts)
            case "research.readiness":
                passed = _valid_readiness(artifacts)
            case "research.baseline":
                passed = _valid_baseline(artifacts)
            case _:
                return "not_run"
        return "passed" if passed else "failed"
    except (
        ValueError,
        KeyError,
        TypeError,
        ArithmeticError,
        csv.Error,
        SyntaxError,
        RecursionError,
    ):
        return "failed"


def _table(text: str) -> tuple[list[str], list[dict[str, str]]]:
    reader = csv.reader(io.StringIO(text), strict=True)
    headers = next(reader, [])
    if (
        not headers
        or len(headers) > 128
        or any(not name.strip() for name in headers)
        or len(set(headers)) != len(headers)
    ):
        raise ValueError("A table requires distinct nonempty column names")
    rows: list[dict[str, str]] = []
    for values in reader:
        if len(values) != len(headers) or len(rows) >= MAX_RESEARCH_ROWS:
            raise ValueError("A table exceeds its shape or row limit")
        rows.append(dict(zip(headers, values, strict=True)))
    if not rows:
        raise ValueError("A table requires observations")
    return headers, rows


def _data(artifacts: Mapping[str, str]) -> tuple[ResearchDictionary, list[dict[str, str]]]:
    dictionary = ResearchDictionary.model_validate_json(artifacts["dictionary"])
    headers, rows = _table(artifacts["data"])
    required = {
        *dictionary.primary_key,
        dictionary.outcome,
        dictionary.split.column,
        *dictionary.permitted_predictors,
        *dictionary.validity,
    }
    if dictionary.arm_column is not None:
        required.add(dictionary.arm_column)
    if not required <= set(headers):
        raise ValueError("A declared column is unavailable")
    return dictionary, rows


def _partitions(
    dictionary: ResearchDictionary, rows: list[dict[str, str]]
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    split = dictionary.split
    if any(row[split.column] not in {split.train, split.test} for row in rows):
        raise ValueError("Every observation needs a declared partition")
    train = [row for row in rows if row[split.column] == split.train]
    test = [row for row in rows if row[split.column] == split.test]
    if not train or not test or any(not row[dictionary.grouping_key].strip() for row in rows):
        raise ValueError("Both partitions and grouping identifiers are required")
    groups = {row[dictionary.grouping_key] for row in train}
    if groups.intersection(row[dictionary.grouping_key] for row in test):
        raise ValueError("Training and test groups overlap")
    keys = [tuple(row[name] for name in dictionary.primary_key) for row in rows]
    if len(set(keys)) != len(keys) or any(not value.strip() for key in keys for value in key):
        raise ValueError("Primary keys must be complete and unique")
    return train, test


def _valid_plan(artifacts: Mapping[str, str]) -> bool:
    dictionary, rows = _data(artifacts)
    plan = AnalysisPlan.model_validate_json(artifacts["plan"])
    _partitions(dictionary, rows)
    return bool(artifacts["question"].strip()) and _compatible_plan(dictionary, plan, rows)


def _compatible_plan(
    dictionary: ResearchDictionary, plan: AnalysisPlan, rows: list[dict[str, str]]
) -> bool:
    matches = (
        plan.outcome == dictionary.outcome
        and plan.group_column == dictionary.grouping_key
        and plan.split_column == dictionary.split.column
        and len(plan.features) == len(set(plan.features)) == 1
        and set(plan.features) <= set(dictionary.permitted_predictors)
    )
    if not matches or any(_invalid_counts(dictionary, rows).values()):
        return False
    for row in rows:
        for name in (plan.outcome, *plan.features):
            _number(row[name])
    return True


def _valid_readiness(artifacts: Mapping[str, str]) -> bool:
    dictionary, rows = _data(artifacts)
    report = ReadinessResult.model_validate_json(artifacts["readiness"])
    missing = {name: sum(not row[name].strip() for row in rows) for name in rows[0]}
    invalid = _invalid_counts(dictionary, rows)
    duplicates = len(rows) - len({tuple(row.items()) for row in rows})
    leakage = sorted(set(dictionary.excluded_predictors).intersection(rows[0]))
    ready = (
        not duplicates and not any(missing.values()) and not any(invalid.values()) and not leakage
    )
    try:
        _partitions(dictionary, rows)
    except ValueError:
        ready = False
    arms = (
        dict(Counter(row[dictionary.arm_column] for row in rows)) if dictionary.arm_column else {}
    )
    return (
        report.row_count == len(rows)
        and report.subject_count
        == len(
            {row[dictionary.grouping_key] for row in rows if row[dictionary.grouping_key].strip()}
        )
        and report.duplicate_rows == duplicates
        and _nonzero(report.missing_by_column) == _nonzero(missing)
        and _nonzero(report.invalid_by_column) == _nonzero(invalid)
        and report.arm_counts == arms
        and sorted(report.leakage_columns) == leakage
        and report.ready_for_analysis == ready
    )


def _invalid_counts(dictionary: ResearchDictionary, rows: list[dict[str, str]]) -> dict[str, int]:
    invalid: dict[str, int] = {}
    for name, rule in dictionary.validity.items():
        count = 0
        for row in rows:
            value = row[name]
            if not value.strip():
                continue
            valid = rule.values is None or value in rule.values
            if rule.minimum is not None or rule.maximum is not None:
                try:
                    number = _number(value)
                    valid = valid and (rule.minimum is None or number >= rule.minimum)
                    valid = valid and (rule.maximum is None or number <= rule.maximum)
                except ValueError:
                    valid = False
            count += not valid
        invalid[name] = count
    return invalid


def _number(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Research metrics require finite numeric values")
    return result


def _nonzero(counts: Mapping[str, int]) -> dict[str, int]:
    return {name: count for name, count in counts.items() if count}


def _close(actual: float, expected: float) -> bool:
    return math.isfinite(expected) and math.isclose(actual, expected, rel_tol=1e-6, abs_tol=1e-6)


def _valid_baseline(artifacts: Mapping[str, str]) -> bool:
    dictionary, rows = _data(artifacts)
    plan = AnalysisPlan.model_validate_json(artifacts["plan"])
    if not _compatible_plan(dictionary, plan, rows):
        return False
    train, test = _partitions(dictionary, rows)
    feature = plan.features[0]
    x = [_number(row[feature]) for row in train]
    y = [_number(row[plan.outcome]) for row in train]
    heldout_x = [_number(row[feature]) for row in test]
    truth = [_number(row[plan.outcome]) for row in test]
    fit = linear_regression(x, y)
    predictions = [fit.intercept + fit.slope * value for value in heldout_x]
    errors = [actual - predicted for actual, predicted in zip(truth, predictions, strict=True)]
    groups = {row[plan.group_column] for row in train}
    if len(groups) > MAX_SENSITIVITY_GROUPS:
        raise ValueError("Group-omission sensitivity exceeds the evaluation limit")
    sensitivity = {}
    for group in sorted(groups):
        subset = [row for row in train if row[plan.group_column] != group]
        refit = linear_regression(
            [_number(row[feature]) for row in subset],
            [_number(row[plan.outcome]) for row in subset],
        )
        sensitivity[group] = math.sqrt(
            mean(
                (actual - refit.intercept - refit.slope * value) ** 2
                for actual, value in zip(truth, heldout_x, strict=True)
            )
        )
    expected = BaselineResult(
        outcome=plan.outcome,
        features=plan.features,
        n_train=len(train),
        n_test=len(test),
        train_subjects=len(groups),
        test_subjects=len({row[plan.group_column] for row in test}),
        intercept=fit.intercept,
        slope=fit.slope,
        test_rmse=math.sqrt(mean(error * error for error in errors)),
        test_mae=mean(abs(error) for error in errors),
        test_r2=1
        - sum(error * error for error in errors)
        / sum((value - mean(truth)) ** 2 for value in truth),
        mean_baseline_rmse=math.sqrt(mean((value - mean(y)) ** 2 for value in truth)),
        sensitivity_rmse=sensitivity,
    )
    actual = BaselineResult.model_validate_json(artifacts["metrics"])
    for name, value in expected.model_dump().items():
        observed = actual.model_dump()[name]
        if isinstance(value, float):
            if not _close(observed, value):
                return False
        elif name == "sensitivity_rmse":
            if actual.sensitivity_rmse.keys() != sensitivity.keys() or any(
                not _close(actual.sensitivity_rmse[key], value)
                for key, value in sensitivity.items()
            ):
                return False
        elif observed != value:
            return False
    headers, reported = _table(artifacts["predictions"])
    if headers != [*dictionary.primary_key, "prediction"] or len(reported) != len(test):
        return False
    expected_rows = {
        tuple(row[name] for name in dictionary.primary_key): value
        for row, value in zip(test, predictions, strict=True)
    }
    actual_rows = {
        tuple(row[name] for name in dictionary.primary_key): _number(row["prediction"])
        for row in reported
    }
    return actual_rows.keys() == expected_rows.keys() and all(
        _close(actual_rows[key], value) for key, value in expected_rows.items()
    )
