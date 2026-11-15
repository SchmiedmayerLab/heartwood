# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Describe and verify retained native qualification evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from xml.etree import ElementTree

MANIFEST = "qualification.json"
REPORT = "junit.xml"
CHECKSUMS = "SHA256SUMS"
DRILLS = "drill-results.json"
# Private gateway state and generated credentials share the output directory and stay excluded.
RETAINED = (
    MANIFEST,
    REPORT,
    DRILLS,
    "qualification.log",
    "controller-preflight.log",
    "network.json",
    "*/evidence.json",
    "*/cleanup.json",
    "*/session-diagnostics.json",
    "*/gateway.log",
    "*/infrastructure-otlp.jsonl",
    "*/infrastructure-otlp-status.json",
    "*/checkpoint-evidence.json",
    "*/records-result.json",
    "*/credential-expiry.json",
    "*/provider-profile.json",
    "*/model-qualification.json",
    "*/worker-container.json",
)
_NATIVE = "test_worker_native_integration::"
_EXTERNAL = "test_worker_external_integration::"
EXPECTED_CASES = {
    "synthetic-native": frozenset(
        {
            _NATIVE + "test_failed_gateway_retains_logs_until_verified_cleanup",
            _NATIVE + "test_records_transport_authorization",
            _NATIVE + "test_native_expired_provider_denies_retained_placeholder[infrastructure0]",
            _NATIVE + "test_native_rejects_symlink_workspace",
            _NATIVE + "test_native_workflow_and_protected_state_restore[local-audit-workflow]",
            _NATIVE + "test_native_workflow_and_protected_state_restore[local-audit-hostile]",
            _NATIVE + "test_native_workflow_and_protected_state_restore[retained-audit-workflow]",
            _NATIVE + "test_native_workflow_and_protected_state_restore[retained-audit-hostile]",
            _NATIVE + "test_native_unapproved_action_does_not_execute[denied]",
            _NATIVE + "test_native_unapproved_action_does_not_execute[shutdown-pending]",
            _NATIVE + "test_lost_create_retains_writer_until_native_revocation",
            _EXTERNAL + "test_native_outside_effect_requires_retained_authorization[approved]",
            _EXTERNAL + "test_native_outside_effect_requires_retained_authorization[denied]",
            _EXTERNAL + "test_native_outside_effect_requires_retained_authorization[pending]",
            _EXTERNAL + "test_native_outside_effect_requires_retained_authorization[lost-reply]",
            _EXTERNAL + "test_native_contained_action_is_automatic_and_outside_effect_waits",
            _EXTERNAL + "test_native_contained_action_is_refused_after_revocation",
            "test_worker_pair_integration::"
            "test_two_native_workers_keep_authority_and_effects_separate",
        }
    ),
    "capable-model-native": frozenset({"test_worker_model_integration::test_native_capable_model"}),
}
_OUTCOMES = {"failure": "failed", "error": "error", "skipped": "skipped"}
# Cases that retain their records outside a directory named after the case.
_CASE_DIRECTORIES = {"test_native_capable_model": "capable-model"}
_DRILL_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\.v[1-9][0-9]{0,3}")


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def image_source(inspected: dict[str, Any], build_records: dict[str, Any]) -> dict[str, Any]:
    """Bind an image to its source through its own metadata rather than its tag."""
    return {
        "revision": (inspected["Config"]["Labels"] or {}).get("org.opencontainers.image.revision"),
        "repo_digests": inspected["RepoDigests"],
        "build_records": build_records,
    }


def cases(report: Path) -> list[dict[str, Any]]:
    """Read each executed case and its outcome from the test report."""
    if not report.is_file():
        return []
    return [
        {
            "id": f"{case.get('classname')}::{case.get('name')}",
            "outcome": next(
                (_OUTCOMES[child.tag] for child in case if child.tag in _OUTCOMES), "passed"
            ),
            "seconds": float(case.get("time", "0")),
        }
        for case in ElementTree.parse(report).iter("testcase")
    ]


def failures(manifest: dict[str, Any]) -> list[str]:
    """List every reason a recorded run does not qualify its source commit."""
    commit = manifest["source"]["commit"]
    reasons = []
    if not manifest["source"]["tree_clean"]:
        reasons.append("the source tree differs from its commit")
    for name, image in manifest["image_sources"].items():
        if {image["revision"], image["build_records"]["application"]["commit"]} != {commit}:
            reasons.append(f"the {name} image was not built from the source commit")
    if not manifest["qualification_sources_unchanged"]:
        reasons.append("qualification sources changed during the run")
    if manifest["returncode"] != 0:
        reasons.append("the test command failed")
    if manifest["network_retained"]:
        reasons.append("the qualification network retained attached resources")
    executed = [case["id"] for case in manifest["cases"]]
    if sorted(executed) != sorted(EXPECTED_CASES[manifest["qualification_kind"]]):
        reasons.append("the executed cases differ from the expected set")
    if any(case["outcome"] != "passed" for case in manifest["cases"]):
        reasons.append("not every case passed")
    return reasons


def model_route(manifest: dict[str, Any]) -> str:
    """Name the inference route a run used, without its credential or any request content."""
    profile = manifest["requested_model_profile"]
    if profile is None:
        return "synthetic-fixture"
    return f"{profile['model']}@{urlsplit(profile['base_url']).hostname}"


def _drill_ids(record: object) -> list[str]:
    """Read drill identifiers only; a value that is not an identifier is never copied."""
    if not isinstance(record, dict):
        return []
    found = [record.get("drill_id")]
    network = record.get("direct_network")
    if isinstance(network, dict):
        found.append(network.get("drill_id"))
    return sorted({item for item in found if isinstance(item, str) and _DRILL_ID.fullmatch(item)})


def drill_results(directory: Path) -> dict[str, Any]:
    """Summarize each drill by release, model route and outcome, without any run content.

    Only identifiers, fixed outcome values and counts are copied, so results from
    different releases and routes can be compared without reading project data.
    """
    manifest = json.loads((directory / MANIFEST).read_text())
    drills = []
    for case in manifest["cases"]:
        name = case["id"].rpartition("::")[2]
        records = directory / _CASE_DIRECTORIES.get(name, name)
        identifiers: set[str] = set()
        result: dict[str, Any] = {"case": case["id"], "outcome": case["outcome"]}
        for name in ("evidence.json", "model-qualification.json"):
            path = records / name
            if not path.is_file() or path.is_symlink():
                continue
            record = json.loads(path.read_text())
            identifiers.update(_drill_ids(record))
            if name == "model-qualification.json" and isinstance(record, dict):
                # Counts compare how a model route responded to the injected instructions.
                for field in ("accepted_actions", "rejected_actions"):
                    value = record.get(field)
                    count = len(value) if isinstance(value, list) else value
                    if type(count) is int:
                        result[field] = count
        drills.append({**result, "drill_ids": sorted(identifiers)})
    return {
        "schema_version": "heartwood.drill-results.v1",
        "release_version": manifest["release_version"],
        "commit": manifest["source"]["commit"],
        "qualification_kind": manifest["qualification_kind"],
        "model_route": model_route(manifest),
        "host": manifest["host"],
        "drills": drills,
        "passed": manifest["passed"],
    }


def write_checksums(directory: Path) -> None:
    """Record the digest of every retained evidence file."""
    retained = sorted(
        {
            path
            for pattern in RETAINED
            for path in directory.glob(pattern)
            if path.is_file() and not path.is_symlink()
        }
    )
    (directory / CHECKSUMS).write_text(
        "".join(f"{_digest(path)}  {path.relative_to(directory).as_posix()}\n" for path in retained)
    )


def _listed_problems(directory: Path) -> list[str]:
    listed = {}
    for line in (directory / CHECKSUMS).read_text().splitlines():
        expected, name = line.split("  ", maxsplit=1)
        listed[name] = expected
    problems = [
        f"{name} is missing or differs from {CHECKSUMS}"
        for name, expected in listed.items()
        if not (path := (directory / name).resolve()).is_relative_to(directory)
        or not path.is_file()
        or _digest(path) != expected
    ]
    if not {MANIFEST, REPORT, DRILLS} <= listed.keys():
        problems.append(f"{CHECKSUMS} does not cover the manifest, test report and drill results")
    return problems


def integrity(directory: Path) -> list[str]:
    """List every reason an evidence directory is not the complete record of one run."""
    directory = directory.resolve()
    if problems := _listed_problems(directory):
        return problems
    manifest = json.loads((directory / MANIFEST).read_text())
    if manifest["cases"] != cases(directory / REPORT):
        problems.append("the recorded cases differ from the test report")
    if json.loads((directory / DRILLS).read_text()) != drill_results(directory):
        problems.append("the drill results differ from the recorded evidence")
    return problems


def verify(directory: Path, commit: str | None = None) -> list[str]:
    """List every reason an evidence directory does not establish a qualification pass."""
    directory = directory.resolve()
    if problems := _listed_problems(directory):
        return problems
    problems = integrity(directory)
    manifest = json.loads((directory / MANIFEST).read_text())
    problems.extend(failures(manifest))
    if manifest["passed"] is not True:
        problems.append("the manifest does not record a pass")
    if commit is not None and manifest["source"]["commit"] != commit:
        problems.append("the evidence describes another source commit")
    return problems


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=verify.__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--commit", help="Require evidence for this exact source commit")
    args = parser.parse_args()
    if found := verify(args.directory, args.commit):
        raise SystemExit("\n".join(found))
