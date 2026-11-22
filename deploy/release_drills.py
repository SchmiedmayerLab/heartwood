# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""State the agent safety drill results of one release for every supported model route."""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from qualification_evidence import CHECKSUMS, DRILLS, EXPECTED_CASES, MANIFEST, integrity, verify

# Release verification runs from the checkout before the gateway wheel is installed.
sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "packages/gateway/src/heartwood/gateway")
)
from worker_image_identity import check_runtime, manifest_chain

ROUTES = Path(__file__).with_name("release-model-routes.toml")
_ROUTE = re.compile(r"openai/[A-Za-z0-9_./:-]{2,248}@[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?")


def supported_routes(path: Path) -> tuple[str, ...]:
    """Read the checked-in capable-model routes a release states drill results for."""
    routes = tomllib.loads(path.read_text()).get("routes")
    if (
        not isinstance(routes, list)
        or not routes
        or any(not isinstance(route, str) or not _ROUTE.fullmatch(route) for route in routes)
        or len(set(routes)) != len(routes)
    ):
        raise ValueError(f"{path} must list one or more unique routes named MODEL@HOST")
    return tuple(routes)


# Route names used by the reference platform's Kubernetes boundary qualification.
BLOCKED_ROUTES = frozenset(
    {
        "controller-canary",
        "model-route-direct",
        "model-route-pod-direct",
        "kubernetes-api",
        "cluster-dns-tcp",
        "cluster-dns-udp",
        "sandbox-gateway",
        "metadata-ipv4",
        "public-address",
    }
)
SCENARIOS = {"file-input-injection.v1", "tool-output-injection.v1"}


def candidate_images(published: Path, commit: str) -> dict[str, str]:
    """Read the attested worker digest and the OpenShell pins from the candidate checkout."""
    record = json.loads(published.read_text())
    if record["commit"] != commit:
        raise ValueError("published image record describes another commit")
    chart = Path("reference-platform/chart/heartwood-platform/files/openshell-session.json")
    configs = json.loads(chart.read_text())["items"]
    (gateway,) = (
        item["data"]["gateway.toml"] for item in configs if "gateway.toml" in item.get("data", {})
    )
    driver = tomllib.loads(gateway)["openshell"]["drivers"]["kubernetes"]
    image = record["images"]["worker"]
    worker = f"{image['repository']}@{image['digest']}"
    pins = {"agent": worker, "workspace-init": worker}
    for container, key in (
        ("openshell-sandbox-bootstrap", "sandbox_runtime_image"),
        ("supervisor", "supervisor_image"),
    ):
        pins[container] = driver[key]
    if not all(
        re.fullmatch(r"[a-z0-9][a-z0-9./:_-]+@sha256:[0-9a-f]{64}", ref) for ref in pins.values()
    ):
        raise ValueError("invalid candidate image digest")
    return pins


def host_result(
    path: Path, version: str, commit: str, pins: dict[str, str], producer_run: int
) -> dict[str, Any]:
    """Validate the producer's content-free GKE observations before publication."""
    record = json.loads(path.read_text())
    if set(record) != {
        "host",
        "release_version",
        "commit",
        "workload",
        "supervisor",
        "worker_routes",
        "outside_worker",
        "run_id",
        "run_attempt",
        "model_route_requests",
    } or (record["host"], record["release_version"], record["commit"]) != ("gke", version, commit):
        raise ValueError("missing or mismatched GKE qualification")
    if (
        type(record["run_id"]) is not int
        or record["run_id"] != producer_run
        or type(record["run_attempt"]) is not int
        or record["run_attempt"] != 1
    ):
        raise ValueError("GKE qualification belongs to another producer attempt")
    expected = {
        "run_as_user": 10001,
        "privileged": False,
        "allow_privilege_escalation": False,
        "added_capabilities": [],
        "dropped_capabilities": ["ALL"],
        "seccomp_profile": "RuntimeDefault",
        "host_network": False,
        "host_pid": False,
        "host_ipc": False,
        "host_paths": [],
        "devices": [],
        "service_account_token_mounted": False,
    }
    workload = record["workload"]
    if set(workload) != {*expected, "containers", "pod_uid"} or any(
        type(workload[key]) is not type(value) or workload[key] != value
        for key, value in expected.items()
    ):
        raise ValueError("incomplete or unsafe inspected workload")
    supervisor = record["supervisor"]
    if set(supervisor) != {"containers", "pod_uid"}:
        raise ValueError("incomplete inspected supervisor")
    for pod, names in (
        (workload, ("agent", "workspace-init", "openshell-sandbox-bootstrap")),
        (supervisor, ("supervisor",)),
    ):
        if set(pod["containers"]) != set(names):
            raise ValueError("incomplete inspected container set")
        public = {}
        for name in names:
            row = pod["containers"][name]
            if (
                set(row) != {"image", "image_id", "platform", "config_id", "manifests"}
                or row["image"] != pins[name]
                or row["platform"] != "linux/amd64"
            ):
                raise ValueError("inspected image differs from the expected role or platform")
            try:
                chain = manifest_chain(
                    pins[name],
                    "amd64",
                    lambda digest, blobs=row["manifests"]: blobs[digest].encode(),
                )
                check_runtime(pins[name], row["image_id"], "amd64", chain, row["config_id"])
            except (AttributeError, ValueError, TypeError, KeyError) as error:
                raise ValueError("unverified inspected container identity") from error
            public[name] = {key: row[key] for key in ("image", "image_id", "platform", "config_id")}
            public[name]["manifest_digest"] = next(reversed(chain))
        pod["containers"] = public
        if not re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", pod["pod_uid"]):
            raise ValueError("invalid inspected pod identity")
    if workload["pod_uid"] == supervisor["pod_uid"]:
        raise ValueError("workload and supervisor must identify distinct pods")
    probes = record["worker_routes"]
    if set(probes) != BLOCKED_ROUTES or any(
        v not in ("refused", "timed-out") for v in probes.values()
    ):
        raise ValueError("missing or unsuccessful worker route probe")
    if record["outside_worker"] != {"metadata-ipv4": "reachable"}:
        raise ValueError("missing outside-worker metadata positive control")
    if type(record["model_route_requests"]) is not int or record["model_route_requests"] < 1:
        raise ValueError("missing permitted model route observation")
    return record


def native_host_cases(directory: Path, host: dict[str, Any]) -> list[dict[str, str]]:
    """Verify the GKE suite and match its boundary case to the inspected host observations."""
    if problems := verify(directory, host["commit"]):
        raise ValueError("GKE native suite did not qualify the commit: " + "; ".join(problems))
    manifest = json.loads((directory / MANIFEST).read_text())
    if (
        manifest["qualification_kind"] != "synthetic-kubernetes"
        or manifest["driver"] != "openshell-kubernetes"
        or manifest["release_version"] != host["release_version"]
        or manifest["source"]["tree_clean"] is not True
        or manifest["qualification_sources_unchanged"] is not True
        or manifest["session_removed"] is not True
        or type(manifest["workflow_run"]["id"]) is not int
        or type(manifest["workflow_run"]["attempt"]) is not int
        or manifest["workflow_run"] != {"id": host["run_id"], "attempt": host["run_attempt"]}
        or not re.fullmatch(
            r"v[0-9]+\.[0-9]+\.[0-9]+-gke\.[0-9]+", manifest["cluster"]["kubernetes"]
        )
        or not {"worker", "runtime"} <= manifest["image_sources"].keys()
    ):
        raise ValueError("GKE native suite belongs to another host, release or producer")
    boundary = "test_kubernetes_workflow_boundary_and_revocation/evidence.json"
    if boundary not in {
        line.split("  ", 1)[1] for line in (directory / CHECKSUMS).read_text().splitlines()
    }:
        raise ValueError("GKE native suite omits the boundary case evidence")
    case = json.loads((directory / boundary).read_text())
    if (
        case["drill_id"] != "kubernetes-workflow.v1"
        or type(case["model_route_requests"]) is not int
        or case["model_route_requests"] != host["model_route_requests"]
    ):
        raise ValueError("GKE native boundary case differs from the host summary")
    verdicts = {"timeout": "timed-out", "ETIMEDOUT": "timed-out", "ECONNREFUSED": "refused"}
    if {k: verdicts.get(v) for k, v in case["route_probes"].items()} != host["worker_routes"]:
        raise ValueError("GKE native route observations differ from the host summary")
    nodes = {item["name"]: item for item in manifest["cluster"]["nodes"]}
    if len(nodes) != len(manifest["cluster"]["nodes"]):
        raise ValueError("ambiguous GKE native node identities")
    for role in ("workload", "supervisor"):
        observed = case["kubernetes_identities"][role + "_pod"]
        pod = host[role]
        node = nodes[observed["node"]]
        native_images = case["images"][observed["name"]]
        expected_images = {
            k: {"image": v["image"], "image_id": v["image_id"]}
            for k, v in pod["containers"].items()
        }
        if (
            observed["uid"] != pod["pod_uid"]
            or native_images != expected_images
            or node["operatingSystem"] != "linux"
            or node["architecture"] != "amd64"
        ):
            raise ValueError("GKE native pod observations differ from the host summary")
    return [{"case": c["id"], "outcome": c["outcome"]} for c in manifest["cases"]]


def require_managed_cleanup(directory: Path) -> None:
    """Require the case teardown and the outside controller's network cleanup."""
    covered = {line.split("  ", 1)[1] for line in (directory / CHECKSUMS).read_text().splitlines()}
    cleanup = "capable-model/cleanup.json"
    if not {MANIFEST, cleanup, "network.json"} <= covered:
        raise ValueError("missing checksummed managed cleanup evidence")
    if (
        json.loads((directory / MANIFEST).read_bytes()).get("network_retained") is not False
        or json.loads((directory / "network.json").read_bytes()).get(
            "retained_for_attached_resources"
        )
        is not False
        or json.loads((directory / cleanup).read_bytes()).get("complete") is not True
    ):
        raise ValueError("managed infrastructure cleanup did not complete")


def capable_result(directory: Path, result: dict[str, Any], commit: str) -> dict[str, Any]:
    """Project a candidate-bound native record and its two observed scenario outcomes."""
    require_managed_cleanup(directory)
    manifest = json.loads((directory / MANIFEST).read_text())
    images = manifest["image_sources"]
    if (
        manifest["source"]["tree_clean"] is not True
        or manifest["qualification_sources_unchanged"] is not True
        or not {"worker", "runtime"} <= images.keys()
        or any(
            image["revision"] != commit or image["build_records"]["application"]["commit"] != commit
            for image in images.values()
        )
    ):
        raise ValueError("managed evidence does not qualify the candidate source and images")
    drills = result["drills"]
    if len(drills) != 1 or drills[0]["case"] not in EXPECTED_CASES["capable-model-native"]:
        raise ValueError("missing capable-model case")
    drill = drills[0]
    if (
        "capable-model-native.v1" not in drill["drill_ids"]
        or drill["outcome"] not in ("passed", "failed", "error")
        or any(
            type(drill.get(k)) is not int or drill[k] < 0
            for k in ("accepted_actions", "rejected_actions")
        )
    ):
        raise ValueError("no executed capable-model drill record")
    # This observation is supplied by the same immutable producer artifact.
    # The file-input outcome also has an independently retained native case.
    scenario_record = json.loads((directory / "scenario-outcomes.json").read_text())
    if set(scenario_record) != {"commit", "model_route", "release_version", "outcomes"} or any(
        scenario_record[key] != result[key] for key in ("commit", "model_route", "release_version")
    ):
        raise ValueError("scenario record differs from the managed run")
    execution = manifest.get("execution_id")
    native = json.loads((directory / "capable-model/model-qualification.json").read_text())
    if (
        not isinstance(execution, str)
        or not re.fullmatch(r"[1-9][0-9]*\.1\.[0-9a-f]{32}", execution)
        or native.get("execution_id") != execution
        or native.get("requested_profile") != manifest["requested_model_profile"]
        or native.get("file_input_observed") is not True
    ):
        raise ValueError("missing native scenario observation for this execution")
    observations = scenario_record["outcomes"]
    if set(observations) != SCENARIOS:
        raise ValueError("missing managed scenario outcomes")
    outcomes = {}
    for scenario, observation in observations.items():
        if (
            not isinstance(observation, dict)
            or set(observation) != {"execution_id", "model_input_observed", "outcome"}
            or observation["execution_id"] != execution
            or observation["model_input_observed"] is not True
            or observation["outcome"] not in ("passed", "failed")
        ):
            raise ValueError("missing or inconsistent managed scenario outcomes")
        outcomes[scenario] = observation["outcome"]
    file_outcome = outcomes["file-input-injection.v1"]
    if file_outcome != native.get("file_input_outcome") or (
        drill["outcome"] == "passed" and file_outcome != "passed"
    ):
        raise ValueError("file-input scenario outcomes disagree")
    return {
        "case_outcome": drill["outcome"],
        "execution_id": execution,
        "outcomes": outcomes,
        "accepted_actions": drill["accepted_actions"],
        "rejected_actions": drill["rejected_actions"],
    }


def release_drills(
    version: str,
    commit: str,
    synthetic: Path,
    capable: Sequence[Path] = (),
    routes: Sequence[str] = (),
) -> dict[str, Any]:
    """Bind drill results to one release and require a record for every supported route.

    The synthetic run must qualify the commit. A capable-model run is an operator record:
    it must be the complete record of a run of the same release and commit, and its
    outcome is reported whether or not it passed.
    """
    if not synthetic.is_dir():
        raise ValueError(f"no qualification evidence exists at {synthetic}")
    if problems := verify(synthetic, commit):
        raise ValueError(
            "synthetic qualification does not qualify the commit: " + "; ".join(problems)
        )
    baseline = json.loads((synthetic / DRILLS).read_text())
    if baseline["release_version"] != version or baseline["model_route"] != "synthetic-fixture":
        raise ValueError("synthetic qualification describes another release or route")
    recorded: dict[str, list[dict[str, Any]]] = {route: [] for route in routes}
    for directory in capable:
        if integrity(directory):
            raise ValueError("managed evidence is not a complete record")
        result = json.loads((directory / DRILLS).read_text())
        if (
            result["qualification_kind"] != "capable-model-native"
            or result["release_version"] != version
            or result["commit"] != commit
        ):
            raise ValueError(f"{directory} records another release or qualification")
        if result["model_route"] not in recorded:
            raise ValueError(f"{directory} records a route this release does not support")
        recorded[result["model_route"]].append(capable_result(directory, result, commit))
    if not recorded:
        raise ValueError("the release must declare at least one capable-model route")
    missing = [route for route, results in recorded.items() if not results]
    if missing:
        raise ValueError("missing capable-model drill evidence for " + ", ".join(missing))
    return {
        "schema_version": "heartwood.release-drills.v3",
        "release_version": version,
        "commit": commit,
        "synthetic": {
            "model_route": "synthetic-fixture",
            "cases": [{"case": item["case"], "outcome": "passed"} for item in baseline["drills"]],
        },
        "capable_model": [
            {"model_route": route, "status": "recorded", "results": results}
            for route, results in recorded.items()
        ],
    }


def main() -> None:
    """Write the drill statement of one release, or exit with the reason it cannot exist."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--synthetic", required=True, type=Path)
    parser.add_argument("--capable", action="append", default=[], type=Path)
    parser.add_argument("--routes", default=ROUTES, type=Path, help="The supported routes file")
    parser.add_argument("--published-images", required=True, type=Path)
    parser.add_argument("--producer-run", required=True, type=int)
    parser.add_argument("--artifact-id", required=True, type=int)
    parser.add_argument("--artifact-sha256", required=True)
    parser.add_argument("--host-evidence", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        statement = release_drills(
            args.version,
            args.commit,
            args.synthetic,
            args.capable,
            supported_routes(args.routes),
        )
        if tomllib.loads(args.routes.read_text()).get("hosts") != ["gke"]:
            raise ValueError("the release requires the declared GKE reference host")
        pins = candidate_images(args.published_images, args.commit)
        host = host_result(args.host_evidence, args.version, args.commit, pins, args.producer_run)
        host["cases"] = native_host_cases(args.host_evidence.parent / "gke-native", host)
        statement["hosts"] = [host]
        if min(args.producer_run, args.artifact_id) < 1 or not re.fullmatch(
            r"[0-9a-f]{64}", args.artifact_sha256
        ):
            raise ValueError("invalid qualification artifact identity")
        prefix = f"{args.producer_run}.1."
        for route in statement["capable_model"]:
            if any(not run["execution_id"].startswith(prefix) for run in route["results"]):
                raise ValueError("managed execution belongs to another producer attempt")
        statement["producer"] = {
            "run_id": args.producer_run,
            "run_attempt": 1,
            "artifact_id": args.artifact_id,
            "artifact_sha256": args.artifact_sha256,
        }
    except (KeyError, TypeError, OSError, ValueError) as error:
        raise SystemExit(str(error)) from None
    # The statement appears only once complete, so a failed run never leaves a release asset.
    partial = args.output.with_name(args.output.name + ".partial")
    partial.write_text(json.dumps(statement, indent=2) + "\n")
    partial.replace(args.output)


if __name__ == "__main__":
    main()
