# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Fit a deterministic age-only logistic baseline on synthetic OMOP-like tables."""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections.abc import Sequence
from pathlib import Path
from typing import Any

_SKILL_ID = "heartwood.synthetic.baseline-model"
_DEFAULT_TARGET_CONCEPT_ID = 201826


def _read_table(path: Path, required_columns: set[str]) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file)
        columns = set(reader.fieldnames or ())
        missing = sorted(required_columns - columns)
        if missing:
            msg = f"{path.name} is missing required columns: {', '.join(missing)}"
            raise ValueError(msg)
        return list(reader)


def _sigmoid(value: float) -> float:
    if value >= 0:
        return 1.0 / (1.0 + math.exp(-value))
    scaled = math.exp(value)
    return scaled / (1.0 + scaled)


def _training_auc(labels: list[int], predictions: list[float]) -> float:
    positive = [score for label, score in zip(labels, predictions, strict=True) if label == 1]
    negative = [score for label, score in zip(labels, predictions, strict=True) if label == 0]
    if not positive or not negative:
        return 0.5
    comparisons = [
        1.0 if positive_score > negative_score else 0.5 if positive_score == negative_score else 0.0
        for positive_score in positive
        for negative_score in negative
    ]
    return sum(comparisons) / len(comparisons)


def build_model(
    data_root: Path,
    *,
    target_condition_concept_id: int = _DEFAULT_TARGET_CONCEPT_ID,
    as_of_year: int = 2025,
) -> dict[str, Any]:
    """Fit a dependency-free logistic baseline and emit aggregate training diagnostics."""
    if target_condition_concept_id <= 0:
        msg = "target condition concept id must be positive"
        raise ValueError(msg)
    if as_of_year <= 0:
        msg = "as-of year must be positive"
        raise ValueError(msg)

    person_rows = _read_table(data_root / "person.csv", {"person_id", "year_of_birth"})
    condition_rows = _read_table(
        data_root / "condition_occurrence.csv",
        {"person_id", "condition_concept_id"},
    )
    birth_years = {
        row["person_id"]: int(row["year_of_birth"])
        for row in person_rows
        if row.get("person_id") and row.get("year_of_birth")
    }
    target_ids = {
        row["person_id"]
        for row in condition_rows
        if row.get("person_id") in birth_years
        and int(row["condition_concept_id"]) == target_condition_concept_id
    }
    if len(birth_years) < 2 or not target_ids or len(target_ids) == len(birth_years):
        msg = "baseline model requires at least two participants and both outcome classes"
        raise ValueError(msg)

    person_ids = sorted(birth_years, key=int)
    ages = [as_of_year - birth_years[person_id] for person_id in person_ids]
    if any(age < 0 for age in ages):
        msg = "year of birth cannot be later than the as-of year"
        raise ValueError(msg)
    labels = [int(person_id in target_ids) for person_id in person_ids]
    mean_age = sum(ages) / len(ages)
    features = [(age - mean_age) / 10.0 for age in ages]

    intercept = 0.0
    age_weight = 0.0
    learning_rate = 0.15
    l2_penalty = 0.05
    iterations = 400
    for _ in range(iterations):
        predictions = [_sigmoid(intercept + age_weight * value) for value in features]
        errors = [prediction - label for prediction, label in zip(predictions, labels, strict=True)]
        intercept -= learning_rate * (sum(errors) / len(errors))
        age_weight -= learning_rate * (
            sum(error * value for error, value in zip(errors, features, strict=True)) / len(errors)
            + l2_penalty * age_weight
        )

    predictions = [_sigmoid(intercept + age_weight * value) for value in features]
    brier_score = sum(
        (prediction - label) ** 2 for prediction, label in zip(predictions, labels, strict=True)
    ) / len(labels)
    positive_count = sum(labels)
    return {
        "schema_version": "heartwood.skill-output.v1",
        "skill_id": _SKILL_ID,
        "model": {
            "model_type": "synthetic-logistic-condition-history",
            "target_condition_concept_id": target_condition_concept_id,
            "feature_names": ["age_at_as_of_year"],
            "feature_center": {"age_years": round(mean_age, 4)},
            "coefficients": {
                "intercept": round(intercept, 6),
                "age_per_10_years": round(age_weight, 6),
            },
        },
        "training_summary": {
            "participant_count": len(labels),
            "positive_count": positive_count,
            "prevalence": round(positive_count / len(labels), 6),
            "brier_score": round(brier_score, 6),
            "roc_auc": round(_training_auc(labels, predictions), 6),
            "iterations": iterations,
        },
        "quality_checks": {
            "synthetic_only": True,
            "aggregate_only_output": True,
            "holdout_evaluation_performed": False,
            "requires_network": False,
        },
    }


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        msg = "value must be positive"
        raise argparse.ArgumentTypeError(msg)
    return parsed


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fit a deterministic synthetic condition-history baseline."
    )
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--target-condition-concept-id",
        default=_DEFAULT_TARGET_CONCEPT_ID,
        type=_positive_int,
    )
    parser.add_argument("--as-of-year", default=2025, type=_positive_int)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the baseline-model Skill."""
    args = _build_parser().parse_args(argv)
    model = build_model(
        args.data_root,
        target_condition_concept_id=args.target_condition_concept_id,
        as_of_year=args.as_of_year,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(model, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote baseline model summary to {args.output.name}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
