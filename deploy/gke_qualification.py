# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Assemble a release artifact from native qualification records and a live session binding."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tomllib
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from xml.etree import ElementTree

from qualification_evidence import (
    CHECKSUMS,
    DRILLS,
    MANIFEST,
    REPORT,
    drill_results,
    integrity,
    verify,
    write_checksums,
)
from release_drills import (
    ROUTES,
    candidate_images,
    capable_result,
    host_result,
    manifest_chain,
    native_host_cases,
    require_managed_cleanup,
    supported_routes,
)
from worker_model_profile import QualificationModelProfile

BOUNDARY = "test_kubernetes_workflow_boundary_and_revocation/evidence.json"
MODEL = "capable-model/model-qualification.json"


def read(path: Path) -> dict[str, Any]:
    """Read one native JSON record."""
    return json.loads(path.read_bytes())


def write(path: Path, value: object) -> None:
    """Write a structured evidence file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def fields(value: dict[str, Any], *keys: str) -> dict[str, Any]:
    """Select required fields from an observation."""
    return {key: value[key] for key in keys}


def bind() -> dict[str, Any]:
    """Resolve the authenticated deployed session before its namespace is removed."""

    def kube(*args: str) -> Any:  # noqa: ANN401
        return json.loads(subprocess.check_output(["kubectl", *args], timeout=30))

    env = os.environ
    if kube("auth", "whoami", "-ojson")["status"]["userInfo"]["username"] != env["KUBE_IDENTITY"]:
        raise ValueError("unexpected Kubernetes caller")
    session = env["SESSION_NAMESPACE"]
    if not re.fullmatch(r"hw-s-[a-f0-9]{16}", session):
        raise ValueError("expected a deployed Heartwood session namespace")
    session_uid = kube("get", "namespace", session, "-ojson")["metadata"]["uid"]
    pod = kube("-n", env["GATEWAY_NAMESPACE"], "get", "pod", env["GATEWAY_POD"], "-ojson")
    if pod["spec"]["serviceAccountName"] != "hw-gw-" + session.removeprefix("hw-s-"):
        raise ValueError("gateway service account does not own the selected session")
    base = "/etc/heartwood/deployment/"
    args = (
        "-n",
        env["GATEWAY_NAMESPACE"],
        "exec",
        env["GATEWAY_POD"],
        "-c",
        env["GATEWAY_CONTAINER"],
        "--",
    )
    # Read only public fields from the gateway's mounted deployment and launch context.
    code = (
        "import json,tomllib,pathlib; p=pathlib.Path(" + repr(base) + ");"
        "d=tomllib.loads((p/'deployment.toml').read_text());"
        "c=json.loads((p/'launch-context.json').read_text());"
        "print(json.dumps([c['project_id'],d['worker']['namespace'],d['identity']['subject'],"
        "d['identity']['issuer'],"
        "d['worker']['image_repository']+'@'+d['worker']['image_digest'],"
        "d['worker']['runtime_images']]))"
    )
    actual = kube(*args, "/opt/heartwood/.venv/bin/python", "-I", "-c", code)
    if actual[:4] != [
        env["PLATFORM_PROJECT"],
        session,
        env["RESEARCHER_SUBJECT"],
        env["RESEARCHER_ISSUER"],
    ]:
        raise ValueError("deployed session, project or researcher differs")
    after = kube("-n", env["GATEWAY_NAMESPACE"], "get", "pod", env["GATEWAY_POD"], "-ojson")
    if after["metadata"]["uid"] != pod["metadata"]["uid"]:
        raise ValueError("gateway was replaced during observation")
    if kube("get", "namespace", session, "-ojson")["metadata"]["uid"] != session_uid:
        raise ValueError("session was replaced during observation")
    version = kube("version", "-ojson")["serverVersion"]["gitVersion"]
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+-gke\.[0-9]+", version):
        raise ValueError("deployed server is not GKE")
    return {
        "worker_ref": actual[4],
        "inventory": actual[5],
        "namespace": session,
        "namespace_uid": session_uid,
        "kube_system_uid": kube("get", "namespace", "kube-system", "-ojson")["metadata"]["uid"],
        "kubernetes": version,
    }


def project_suite(source: Path, target: Path, *, managed: bool) -> None:
    """Retain the case inventory and observations used by the release consumer."""
    manifest = read(source / MANIFEST)
    included = fields(
        manifest,
        "qualification_kind",
        "release_version",
        "source",
        "qualification_sources_unchanged",
        "requested_model_profile",
        "returncode",
        "cases",
        "passed",
        "records_service",
    )
    included["source"] = fields(manifest["source"], "commit", "tree_clean")
    included["host"] = fields(manifest["host"], "os", "architecture")
    included["image_sources"] = {
        key: {
            "revision": row["revision"],
            "build_records": {"application": fields(row["build_records"]["application"], "commit")},
        }
        for key, row in manifest["image_sources"].items()
    }
    if managed:
        included.update(fields(manifest, "execution_id", "network_retained"))
        write(
            target / "network.json",
            fields(read(source / "network.json"), "retained_for_attached_resources"),
        )
        cleanup = "capable-model/cleanup.json"
        write(target / cleanup, fields(read(source / cleanup), "complete"))
    else:
        included.update(fields(manifest, "driver", "workflow_run", "session_removed"))
        included["cluster"] = {
            "kubernetes": manifest["cluster"]["kubernetes"],
            "nodes": [
                fields(n, "name", "architecture", "operatingSystem")
                for n in manifest["cluster"]["nodes"]
            ],
        }
    if included["records_service"] is not None:
        write(target / "records-service/records-result.json", included["records_service"])
    write(target / MANIFEST, included)
    # Failure messages and captured streams can contain provider or project content.
    suite = ElementTree.Element("testsuite")
    for case in ElementTree.parse(source / REPORT).iter("testcase"):
        item = ElementTree.SubElement(
            suite, "testcase", fields(case.attrib, "classname", "name", "time")
        )
        for child in case:
            if child.tag in {"failure", "error", "skipped"}:
                ElementTree.SubElement(item, child.tag)
    ElementTree.ElementTree(suite).write(target / REPORT)
    for drill in read(source / DRILLS)["drills"]:
        name = drill["case"].split("::")[-1]
        if managed:
            original = read(source / MODEL)
            record = fields(
                original,
                "drill_id",
                "execution_id",
                "file_input_observed",
                "file_input_outcome",
                "requested_profile",
                "tool_output_observed",
                "tool_output_outcome",
            )
            record.update(fields(drill, "accepted_actions", "rejected_actions"))
            if "direct_network" in original:
                record["direct_network"] = (
                    fields(original["direct_network"], "drill_id")
                    if "drill_id" in original["direct_network"]
                    else {}
                )
            write(target / MODEL, record)
        else:
            path = Path(name) / "evidence.json"
            if not (source / path).is_file():
                continue
            original = read(source / path)
            record = {k: original[k] for k in ("drill_id",) if k in original}
            if "drill_id" in original.get("direct_network", {}):
                record["direct_network"] = fields(original["direct_network"], "drill_id")
            if str(path) == BOUNDARY:
                record.update(fields(original, "images", "route_probes", "model_route_requests"))
                record["kubernetes_identities"] = {
                    key: fields(original["kubernetes_identities"][key], "uid", "name", "node")
                    for key in ("workload_pod", "supervisor_pod")
                }
            write(target / path, record)
    write(target / DRILLS, drill_results(target))
    write_checksums(target)


def assemble(
    source: Path,
    output: Path,
    published: Path,
    binding: dict[str, Any],
    commit: str,
    version: str,
    run: int,
    routes: tuple[str, ...],
) -> None:
    """Verify and atomically assemble all records for this release producer."""
    if output.exists() or not routes or run < 1:
        raise ValueError("require a fresh destination, a producer run and declared routes")
    native = source / "gke-native"
    manifest = read(native / MANIFEST)
    if problems := verify(native, commit):
        raise ValueError("invalid GKE suite: " + "; ".join(problems))
    ids = manifest["kubernetes_identities"]
    boundary = read(native / BOUNDARY)
    if (
        any(binding[k] != ids[k] for k in ("namespace", "namespace_uid", "kube_system_uid"))
        or boundary["kubernetes_identities"]["namespace_uid"] != binding["namespace_uid"]
        or manifest["cluster"]["kubernetes"] != binding["kubernetes"]
    ):
        raise ValueError("suite did not observe the authenticated deployed session")
    published_images = read(published)["images"]
    references = {
        role: published_images[role]["repository"] + "@" + published_images[role]["digest"]
        for role in ("worker", "runtime")
    }
    pins = candidate_images(published, commit)
    if pins["agent"] != binding["worker_ref"]:
        raise ValueError("deployed worker differs from the attested candidate")
    roles = {
        "agent": "worker",
        "workspace-init": "worker",
        "openshell-sandbox-bootstrap": "sandbox",
        "supervisor": "supervisor",
    }
    for kind in ("workload", "supervisor"):
        pod = boundary["kubernetes_identities"][kind + "_pod"]
        node = binding["inventory"][pod["node"]]
        for name, row in boundary["gke_inspection"][kind]["containers"].items():
            expected = node["images"][pins[name]]
            chain = manifest_chain(
                pins[name], "amd64", lambda digest, row=row: row["manifests"][digest].encode()
            )
            if chain != row["manifests"]:
                raise ValueError("unexpected manifest outside the observed OCI chain")
            if (
                row["platform"] != node["platform"]
                or expected["role"] != roles[name]
                or fields(row, "config_id", "manifests")
                != fields(expected, "config_id", "manifests")
            ):
                raise ValueError("controller did not observe the deployed node inventory")
    outcomes = {"ECONNREFUSED": "refused", "ETIMEDOUT": "timed-out", "timeout": "timed-out"}
    with TemporaryDirectory(dir=output.parent) as scratch:
        artifact = Path(scratch) / "artifact"
        hostfile = artifact / "gke.json"
        write(
            hostfile,
            {
                **fields(boundary["gke_inspection"], "workload", "supervisor", "outside_worker"),
                "host": "gke",
                "run_id": run,
                "run_attempt": 1,
                "commit": commit,
                "release_version": version,
                "model_route_requests": boundary["model_route_requests"],
                "worker_routes": {k: outcomes[v] for k, v in boundary["route_probes"].items()},
            },
        )
        host = host_result(hostfile, version, commit, pins, run)
        native_host_cases(native, host)
        project_suite(native, artifact / "gke-native", managed=False)
        native_host_cases(artifact / "gke-native", host)
        for number, route in enumerate(routes):
            name = f"route-{number + 1}"
            src, dst = source / name, artifact / name
            if integrity(src):
                raise ValueError("incomplete managed run")
            require_managed_cleanup(src)
            covered = {
                line.split("  ", 1)[1] for line in (src / CHECKSUMS).read_text().splitlines()
            }
            if MODEL not in covered:
                raise ValueError("managed scenario observation is outside the native checksums")
            managed = read(src / MANIFEST)
            for role, reference in references.items():
                if (
                    managed["invocation"][role + "_image"] != reference
                    or reference not in managed["image_sources"][role]["repo_digests"]
                ):
                    raise ValueError("managed native run used another published image")
            result = read(src / DRILLS)
            profile = QualificationModelProfile(**managed["requested_model_profile"])
            if profile.mapping() != managed["requested_model_profile"] or (
                result["commit"],
                result["release_version"],
                result["model_route"],
                result["qualification_kind"],
            ) != (commit, version, route, "capable-model-native"):
                raise ValueError("managed execution differs from the declared route or candidate")
            project_suite(src, dst, managed=True)
            record = read(dst / MODEL)
            write(
                dst / "scenario-outcomes.json",
                {
                    **fields(result, "commit", "release_version", "model_route"),
                    "outcomes": {
                        scenario: {
                            "execution_id": record["execution_id"],
                            "model_input_observed": record[prefix + "_observed"],
                            "outcome": record[prefix + "_outcome"],
                        }
                        for scenario, prefix in (
                            ("file-input-injection.v1", "file_input"),
                            ("tool-output-injection.v1", "tool_output"),
                        )
                    },
                },
            )
            write_checksums(
                dst,
                (
                    MANIFEST,
                    REPORT,
                    DRILLS,
                    MODEL,
                    "scenario-outcomes.json",
                    "network.json",
                    "capable-model/cleanup.json",
                ),
            )
            if integrity(dst) or not capable_result(dst, read(dst / DRILLS), commit)[
                "execution_id"
            ].startswith(f"{run}.1."):
                raise ValueError("managed execution belongs to another producer")
        artifact.rename(output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("bind", "assemble"))
    parser.add_argument("--directory", required=True, type=Path)
    args = parser.parse_args()
    root = args.directory
    if args.operation == "bind":
        write(root / "binding.json", bind())
    else:
        assemble(
            root / "observations",
            root / "artifact",
            root / "published-images.json",
            read(root / "binding.json"),
            os.environ["GITHUB_SHA"],
            tomllib.loads(Path("VERSION.toml").read_text())["version"],
            int(os.environ["GITHUB_RUN_ID"]),
            supported_routes(ROUTES),
        )
