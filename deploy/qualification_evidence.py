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
# The Kubernetes driver's run retains its own records: what admission read about the pods,
# and what the run observed about the cluster and the session namespace.
KUBERNETES_RETAINED = (
    MANIFEST,
    REPORT,
    DRILLS,
    "qualification.log",
    "cluster.json",
    "images.json",
    "gateway.log",
    "*/launch-failures.log",
    "*/evidence.json",
    "*/cleanup.json",
    "*/session-diagnostics.json",
    "*/provider-profile.json",
    "*/worker-pods.json",
)
_NATIVE = "test_worker_native_integration::"
_EXTERNAL = "test_worker_external_integration::"
_LIFECYCLE = "test_worker_lifecycle_integration::"
_KUBERNETES = "test_worker_kubernetes_integration::"
EXPECTED_CASES = {
    "synthetic-native": frozenset(
        {
            _NATIVE + "test_failed_gateway_retains_logs_until_verified_cleanup",
            _NATIVE + "test_records_transport_authorization",
            _NATIVE + "test_records_service_qualification",
            _NATIVE + "test_native_expired_provider_denies_retained_placeholder[infrastructure0]",
            _NATIVE + "test_native_rejects_symlink_workspace",
            _NATIVE + "test_native_workflow_and_protected_state_restore[local-audit-workflow]",
            _NATIVE + "test_native_workflow_and_protected_state_restore[local-audit-hostile]",
            _NATIVE + "test_native_workflow_and_protected_state_restore[retained-audit-workflow]",
            _NATIVE + "test_native_workflow_and_protected_state_restore[retained-audit-hostile]",
            _NATIVE + "test_native_unapproved_action_does_not_execute[denied]",
            _NATIVE + "test_native_unapproved_action_does_not_execute[shutdown-pending]",
            _NATIVE + "test_lost_create_retains_writer_until_native_revocation",
            _NATIVE + "test_native_unsent_creation_is_confirmed_absent",
            _NATIVE + "test_native_revoked_worker_is_replaced_for_the_next_command",
            _NATIVE + "test_native_forwarder_ends_with_the_gateway_process",
            _EXTERNAL + "test_native_outside_effect_requires_retained_authorization[approved]",
            _EXTERNAL + "test_native_outside_effect_requires_retained_authorization[denied]",
            _EXTERNAL + "test_native_outside_effect_requires_retained_authorization[pending]",
            _EXTERNAL + "test_native_outside_effect_requires_retained_authorization[lost-reply]",
            _EXTERNAL + "test_native_outside_effect_requires_retained_authorization[forbidden]",
            _EXTERNAL + "test_native_contained_action_is_automatic_and_outside_effect_waits",
            _EXTERNAL + "test_native_contained_action_is_refused_after_revocation",
            "test_worker_pair_integration::"
            "test_two_native_workers_keep_authority_and_effects_separate",
            _LIFECYCLE + "test_native_gateway_restart_reconciles_the_session[settled]",
            _LIFECYCLE + "test_native_gateway_restart_reconciles_the_session[pending]",
            _LIFECYCLE + "test_native_cancellation_settles_without_repeating_the_effect",
            _LIFECYCLE + "test_native_new_worker_image_and_rotated_credential_after_restart",
            _LIFECYCLE + "test_native_advisory_specialist_runs_inside_the_parent_worker",
            _LIFECYCLE + "test_native_migrated_project_serves_through_the_operator_manifest",
        }
    ),
    "capable-model-native": frozenset({"test_worker_model_integration::test_native_capable_model"}),
    "synthetic-kubernetes": frozenset(
        _KUBERNETES + name
        for name in (
            "test_kubernetes_gateway_credential_is_confined",
            "test_kubernetes_workflow_boundary_and_revocation",
            "test_kubernetes_conflicting_deletion_is_retried_until_absence",
            "test_kubernetes_unsent_creation_is_confirmed_absent",
            "test_kubernetes_repeated_creation_is_not_executed_after_revocation",
            "test_kubernetes_admission_refuses_another_supervisor_image",
            "test_kubernetes_admission_refuses_an_added_network_policy",
            "test_kubernetes_staging_requires_the_default_deny_policy",
            "test_kubernetes_staging_name_workspace_creates_no_pod",
            "test_kubernetes_staging_transfers_a_large_input_within_the_request_limit",
            "test_kubernetes_lost_creation_reply_is_revoked_during_provisioning",
            "test_kubernetes_revoked_worker_is_replaced_for_the_next_command",
            "test_kubernetes_gateway_restart_reconciles_the_session[settled]",
            "test_kubernetes_gateway_restart_reconciles_the_session[pending]",
            "test_kubernetes_cancellation_settles_without_repeating_the_effect",
            "test_kubernetes_symlink_workspace_image_runs_without_its_working_directory",
            "test_kubernetes_worker_container_is_never_restarted",
            "test_kubernetes_new_worker_image_and_rotated_credential_after_restart",
            "test_kubernetes_migrated_project_serves_through_the_operator_manifest",
            "test_kubernetes_project_data_is_read_only_at_its_declared_path",
            "test_kubernetes_admission_refuses_project_data_out_of_its_declaration[writable-volume]",
            "test_kubernetes_admission_refuses_project_data_out_of_its_declaration[another-project]",
            "test_kubernetes_admission_refuses_project_data_out_of_its_declaration[undeclared-path]",
            "test_kubernetes_data_catalog_research_task",
        )
    ),
}
# What the Docker run reads from the worker container and a pod does not show. Each entry
# names the platform requirement that takes the place of the inspected value.
KUBERNETES_PLATFORM_REQUIREMENTS = (
    {
        "docker_evidence": "HostConfig.NetworkMode none on the worker container",
        "kubernetes_evidence": "the driver's NetworkPolicies at admission, and route probes",
        "platform_requirement": "a network plugin that enforces ingress and egress NetworkPolicy",
    },
    {
        "docker_evidence": "HostConfig.PidsLimit on the worker container",
        "kubernetes_evidence": None,
        "platform_requirement": "a kubelet process limit for each pod (podPidsLimit)",
    },
    {
        "docker_evidence": "HostConfig.CgroupnsMode private on the worker container",
        "kubernetes_evidence": None,
        "platform_requirement": "cgroup v2 nodes with a private cgroup namespace per container",
    },
    {
        "docker_evidence": "AppArmor profile and SELinux label in HostConfig.SecurityOpt",
        "kubernetes_evidence": "the restricted Pod Security level of the namespace at admission",
        "platform_requirement": "the node's default AppArmor profile or SELinux container policy",
    },
)
# Cases of the Docker run that the Kubernetes run does not repeat, with the reason.
KUBERNETES_CASES_NOT_RUN = {
    "records transport, records service and retained-audit cases": (
        "independent of the compute driver; the Docker run qualifies them"
    ),
    "hostile approved action, expired provider credential and telemetry outage": (
        "sandbox supervisor behavior that does not depend on the compute driver; "
        "the Docker run qualifies it"
    ),
    "outside effects, two workers and advisory specialist": (
        "gateway behavior that does not depend on the compute driver; the Docker run qualifies it"
    ),
    "forwarder ends with the gateway process": (
        "the same forwarding process on both drivers; the Docker run qualifies it"
    ),
}
_OUTCOMES = {"failure": "failed", "error": "error", "skipped": "skipped"}
# Cases that retain their records outside a directory named after the case.
_CASE_DIRECTORIES = {"test_native_capable_model": "capable-model"}
_DRILL_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\.v[1-9][0-9]{0,3}")
RECORDS_RESULT = "records-service/records-result.json"


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


def records_service(directory: Path) -> dict[str, Any] | None:
    """Read the store and create-only verdict the records-service case retained."""
    path = directory / RECORDS_RESULT
    if not path.is_file():
        return None
    result = json.loads(path.read_text())
    return {name: result.get(name) for name in ("store", "conditional_write_enforcement", "passed")}


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
    if manifest["qualification_kind"] != "synthetic-kubernetes" and manifest["network_retained"]:
        reasons.append("the qualification network retained attached resources")
    if manifest["qualification_kind"] == "synthetic-kubernetes" and not manifest["session_removed"]:
        reasons.append("the session namespace was not removed")
    executed = [case["id"] for case in manifest["cases"]]
    if sorted(executed) != sorted(EXPECTED_CASES[manifest["qualification_kind"]]):
        reasons.append("the executed cases differ from the expected set")
    if any(case["outcome"] != "passed" for case in manifest["cases"]):
        reasons.append("not every case passed")
    records = manifest.get("records_service") or {}
    if manifest["qualification_kind"] == "synthetic-native" and not (
        records.get("passed") is True and records.get("conditional_write_enforcement") is True
    ):
        reasons.append("the records store did not enforce create-only publication")
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
        for record_name in ("evidence.json", "model-qualification.json"):
            path = records / record_name
            if not path.is_file() or path.is_symlink():
                continue
            record = json.loads(path.read_text())
            identifiers.update(_drill_ids(record))
            if record_name == "model-qualification.json" and isinstance(record, dict):
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


def write_checksums(directory: Path, patterns: tuple[str, ...] = RETAINED) -> None:
    """Record the digest of every retained evidence file."""
    retained = sorted(
        {
            path
            for pattern in patterns
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
    if manifest.get("records_service") != records_service(directory):
        problems.append(f"the recorded records result differs from {RECORDS_RESULT}")
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
