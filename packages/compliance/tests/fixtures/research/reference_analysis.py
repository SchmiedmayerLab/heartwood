# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Executable synthetic analysis used only by deterministic conformance tests."""

import argparse
import csv
import json
import math
from pathlib import Path
from statistics import linear_regression, mean


def main() -> None:
    """Fit the prespecified baseline and write deterministic held-out outputs."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    with args.data.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    train = [row for row in rows if row["partition"] == "train"]
    test = [row for row in rows if row["partition"] == "test"]
    fit = linear_regression(
        [float(row["measurement"]) for row in train],
        [float(row["response"]) for row in train],
    )
    truth = [float(row["response"]) for row in test]
    predictions = [fit.intercept + fit.slope * float(row["measurement"]) for row in test]
    errors = [y - p for y, p in zip(truth, predictions, strict=True)]
    train_subjects = sorted({row["subject_id"] for row in train})
    sensitivity = {}
    for subject in train_subjects:
        subset = [row for row in train if row["subject_id"] != subject]
        refit = linear_regression(
            [float(row["measurement"]) for row in subset],
            [float(row["response"]) for row in subset],
        )
        sensitivity[subject] = math.sqrt(
            mean(
                (float(row["response"]) - refit.intercept - refit.slope * float(row["measurement"]))
                ** 2
                for row in test
            )
        )
    metrics = {
        "outcome": "response",
        "features": ["measurement"],
        "n_train": len(train),
        "n_test": len(test),
        "train_subjects": len(train_subjects),
        "test_subjects": len({row["subject_id"] for row in test}),
        "intercept": fit.intercept,
        "slope": fit.slope,
        "test_rmse": math.sqrt(mean(error**2 for error in errors)),
        "test_mae": mean(abs(error) for error in errors),
        "test_r2": 1
        - sum(error**2 for error in errors) / sum((y - mean(truth)) ** 2 for y in truth),
        "mean_baseline_rmse": math.sqrt(
            mean((y - mean(float(row["response"]) for row in train)) ** 2 for y in truth)
        ),
        "sensitivity_rmse": sensitivity,
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "metrics.json").write_text(json.dumps(metrics, sort_keys=True) + "\n")
    with (args.output_dir / "predictions.csv").open("w", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["subject_id", "visit", "prediction"])
        for row, prediction in zip(test, predictions, strict=True):
            writer.writerow([row["subject_id"], row["visit"], prediction])


if __name__ == "__main__":
    main()
