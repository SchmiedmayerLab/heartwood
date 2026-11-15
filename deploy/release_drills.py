# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""State the agent safety drill results of one release for every supported model route."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from qualification_evidence import DRILLS, integrity, verify


def release_drills(
    version: str,
    commit: str,
    synthetic: Path,
    capable: Sequence[Path] = (),
    routes: Sequence[str] = (),
) -> dict[str, Any]:
    """Bind drill results to one release; a supported route without a record is not run.

    The synthetic run must qualify the commit. A capable-model run is an operator record:
    it must be the complete record of a run of the same release and commit, and its
    outcome is reported whether or not it passed.
    """
    if problems := verify(synthetic, commit):
        raise ValueError(
            "synthetic qualification does not qualify the commit: " + "; ".join(problems)
        )
    baseline = json.loads((synthetic / DRILLS).read_text())
    if baseline["release_version"] != version or baseline["model_route"] != "synthetic-fixture":
        raise ValueError("synthetic qualification describes another release or route")
    recorded: dict[str, list[dict[str, Any]]] = {route: [] for route in routes}
    for directory in capable:
        if problems := integrity(directory):
            raise ValueError(f"{directory} is not a complete record: " + "; ".join(problems))
        result = json.loads((directory / DRILLS).read_text())
        if (
            result["qualification_kind"] != "capable-model-native"
            or result["release_version"] != version
            or result["commit"] != commit
        ):
            raise ValueError(f"{directory} records another release or qualification")
        if result["model_route"] not in recorded:
            raise ValueError(f"{directory} records a route this release does not support")
        recorded[result["model_route"]].append(result)
    return {
        "schema_version": "heartwood.release-drills.v1",
        "release_version": version,
        "commit": commit,
        "synthetic": baseline,
        "capable_model": [
            {"model_route": route, "status": "recorded", "results": results}
            if results
            else {"model_route": route, "status": "not-run"}
            for route, results in recorded.items()
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--synthetic", required=True, type=Path)
    parser.add_argument("--capable", action="append", default=[], type=Path)
    parser.add_argument(
        "--route", action="append", default=[], help="A supported route as MODEL@HOST"
    )
    args = parser.parse_args()
    try:
        statement = release_drills(
            args.version, args.commit, args.synthetic, args.capable, args.route
        )
    except ValueError as error:
        raise SystemExit(str(error)) from None
    print(json.dumps(statement, indent=2))
