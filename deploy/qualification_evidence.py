# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Describe and verify retained native qualification evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

MANIFEST = "qualification.json"
REPORT = "junit.xml"
CHECKSUMS = "SHA256SUMS"
# Private gateway state and generated credentials share the output directory and stay excluded.
RETAINED = (
    MANIFEST,
    REPORT,
    "qualification.log",
    "controller-preflight.log",
    "network.json",
    "*/evidence.json",
    "*/cleanup.json",
    "*/gateway.log",
    "*/infrastructure-otlp.jsonl",
    "*/infrastructure-otlp-status.json",
    "*/checkpoint-evidence.json",
    "*/records-result.json",
    "*/credential-expiry.json",
    "*/provider-profile.json",
    "*/model-qualification.json",
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
            "test_worker_pair_integration::"
            "test_two_native_workers_keep_authority_and_effects_separate",
        }
    ),
    "capable-model-native": frozenset({"test_worker_model_integration::test_native_capable_model"}),
}
_OUTCOMES = {"failure": "failed", "error": "error", "skipped": "skipped"}


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


def verify(directory: Path, commit: str | None = None) -> list[str]:
    """List every reason an evidence directory does not establish a qualification pass."""
    directory = directory.resolve()
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
    if not {MANIFEST, REPORT} <= listed.keys():
        problems.append(f"{CHECKSUMS} does not cover the manifest and test report")
    if problems:
        return problems
    manifest = json.loads((directory / MANIFEST).read_text())
    if manifest["cases"] != cases(directory / REPORT):
        problems.append("the recorded cases differ from the test report")
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
