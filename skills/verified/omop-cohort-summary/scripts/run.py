# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Build an aggregate target-condition cohort summary from synthetic OMOP-like tables."""

from __future__ import annotations

import argparse
import csv
import json
from collections.abc import Sequence
from pathlib import Path
from statistics import median
from typing import Any

_SKILL_ID = "heartwood.synthetic.omop-cohort-summary"
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


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        msg = "value must be positive"
        raise argparse.ArgumentTypeError(msg)
    return parsed


def _non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        msg = "value must be non-negative"
        raise argparse.ArgumentTypeError(msg)
    return parsed


def build_summary(
    data_root: Path,
    *,
    target_condition_concept_id: int = _DEFAULT_TARGET_CONCEPT_ID,
    minimum_age: int = 18,
    aggregate_count_floor: int = 20,
) -> dict[str, Any]:
    """Build a reproducible aggregate cohort and data-quality summary."""
    if target_condition_concept_id <= 0:
        msg = "target condition concept id must be positive"
        raise ValueError(msg)
    if minimum_age < 0:
        msg = "minimum age must be non-negative"
        raise ValueError(msg)
    if aggregate_count_floor < 0:
        msg = "aggregate count floor must be non-negative"
        raise ValueError(msg)

    person_rows = _read_table(
        data_root / "person.csv",
        {"person_id", "year_of_birth"},
    )
    condition_rows = _read_table(
        data_root / "condition_occurrence.csv",
        {
            "condition_occurrence_id",
            "person_id",
            "condition_concept_id",
            "condition_start_year",
        },
    )

    birth_years: dict[str, int] = {}
    duplicate_person_ids: set[str] = set()
    invalid_birth_years = 0
    for row in person_rows:
        person_id = row.get("person_id", "").strip()
        if not person_id:
            invalid_birth_years += 1
            continue
        if person_id in birth_years:
            duplicate_person_ids.add(person_id)
            continue
        try:
            birth_years[person_id] = int(row.get("year_of_birth", ""))
        except ValueError:
            invalid_birth_years += 1

    known_condition_person_ids: set[str] = set()
    missing_reference_ids: set[str] = set()
    condition_occurrence_ids: set[str] = set()
    duplicate_condition_occurrence_ids: set[str] = set()
    valid_conditions: list[tuple[str, int, int]] = []
    invalid_condition_rows = 0
    chronology_violations = 0
    for row in condition_rows:
        occurrence_id = row.get("condition_occurrence_id", "").strip()
        if not occurrence_id:
            invalid_condition_rows += 1
            continue
        if occurrence_id in condition_occurrence_ids:
            duplicate_condition_occurrence_ids.add(occurrence_id)
            continue
        condition_occurrence_ids.add(occurrence_id)
        person_id = row.get("person_id", "").strip()
        if person_id not in birth_years:
            if person_id:
                missing_reference_ids.add(person_id)
            continue
        known_condition_person_ids.add(person_id)
        try:
            concept_id = int(row.get("condition_concept_id", ""))
            start_year = int(row.get("condition_start_year", ""))
        except ValueError:
            invalid_condition_rows += 1
            continue
        if start_year < birth_years[person_id]:
            chronology_violations += 1
        valid_conditions.append((person_id, concept_id, start_year))

    first_target_year: dict[str, int] = {}
    for person_id, concept_id, start_year in valid_conditions:
        if concept_id != target_condition_concept_id:
            continue
        first_target_year[person_id] = min(first_target_year.get(person_id, start_year), start_year)

    cohort_ages: dict[str, int] = {}
    for person_id, index_year in first_target_year.items():
        age = index_year - birth_years[person_id]
        if age >= minimum_age:
            cohort_ages[person_id] = age

    cohort_ids = set(cohort_ages)
    cohort_conditions = [row for row in valid_conditions if row[0] in cohort_ids]
    target_occurrences = sum(
        concept_id == target_condition_concept_id for _, concept_id, _ in cohort_conditions
    )
    ages = sorted(cohort_ages.values())
    participant_count = len(cohort_ids)
    return {
        "schema_version": "heartwood.skill-output.v1",
        "skill_id": _SKILL_ID,
        "dataset_type": "omop-cdm",
        "cohort_definition": {
            "target_condition_concept_id": target_condition_concept_id,
            "index_event": "first recorded target condition occurrence",
            "minimum_age_at_index": minimum_age,
        },
        "summary": {
            "source_participant_count": len(birth_years),
            "source_condition_occurrence_count": len(condition_rows),
            "participant_count": participant_count,
            "condition_occurrence_count": len(cohort_conditions),
            "target_condition_occurrence_count": target_occurrences,
            "condition_person_coverage_count": len(known_condition_person_ids & cohort_ids),
            "age_at_index": {
                "minimum": min(ages) if ages else None,
                "median": median(ages) if ages else None,
                "maximum": max(ages) if ages else None,
            },
            "exclusions": {
                "without_target_condition": len(birth_years) - len(first_target_year),
                "below_minimum_age": len(first_target_year) - participant_count,
            },
        },
        "quality_checks": {
            "person_table_present": bool(person_rows),
            "condition_table_present": bool(condition_rows),
            "person_ids_unique": not duplicate_person_ids,
            "birth_years_parseable": invalid_birth_years == 0,
            "condition_occurrence_ids_unique": not duplicate_condition_occurrence_ids,
            "condition_years_parseable": invalid_condition_rows == 0,
            "condition_years_not_before_birth": chronology_violations == 0,
            "condition_references_known_persons": not missing_reference_ids,
            "aggregate_only_output": True,
        },
        "export_guard": {
            "aggregate_count_floor": aggregate_count_floor,
            "exportable": participant_count >= aggregate_count_floor,
        },
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build an aggregate target-condition cohort from synthetic OMOP-like tables."
    )
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--target-condition-concept-id",
        default=_DEFAULT_TARGET_CONCEPT_ID,
        type=_positive_int,
    )
    parser.add_argument("--minimum-age", default=18, type=_non_negative_int)
    parser.add_argument("--aggregate-count-floor", default=20, type=_non_negative_int)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the cohort summary Skill."""
    args = _build_parser().parse_args(argv)
    summary = build_summary(
        args.data_root,
        target_condition_concept_id=args.target_condition_concept_id,
        minimum_age=args.minimum_age,
        aggregate_count_floor=args.aggregate_count_floor,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote aggregate cohort summary to {args.output.name}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
