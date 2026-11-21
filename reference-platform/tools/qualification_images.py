# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Observe the kind chart's running containers and check them against a publication record."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Any

from oci_identity import check_runtime, node_identity
from publish_images import read_record


def workloads(session: str) -> dict[str, tuple[str, str, str]]:
    """Identify chart roles independently of the image each role actually runs."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", session):
        raise ValueError("invalid hosted session identifier")
    namespace = f"hw-s-{session}"
    return {
        "platform-services": (
            "heartwood-platform",
            "app.kubernetes.io/component=control",
            "control",
        ),
        "funnel": ("hw-jobs-small", "heartwood.platform/role=funnel-server", "server"),
        "runtime": (
            "heartwood-platform",
            "app.kubernetes.io/component=llama-server",
            "llama-server",
        ),
        "hosted-gateway": (
            "heartwood-sessions",
            f"heartwood.platform/session={session}",
            "notebook",
        ),
        "sandbox-gateway": (namespace, "app.kubernetes.io/name=openshell", "openshell-gateway"),
        "worker": (namespace, "heartwood.role=worker", "agent"),
    }


def observation(pods: list[dict[str, Any]], container: str) -> dict[str, Any]:
    """Keep only the identity of the selected running container, without pod configuration."""
    rows = []
    for pod in pods:
        if pod["metadata"].get("deletionTimestamp"):
            continue
        for spec in pod["spec"]["containers"]:
            if spec["name"] != container:
                continue
            for status in pod["status"].get("containerStatuses", []):
                if status["name"] == container and "running" in status["state"]:
                    rows.append(
                        {
                            "namespace": pod["metadata"]["namespace"],
                            "pod": pod["metadata"]["name"],
                            "node": pod["spec"]["nodeName"],
                            "pod_uid": pod["metadata"]["uid"],
                            "container": container,
                            "image": spec["image"],
                            "image_id": status["imageID"],
                        }
                    )
    if len(rows) != 1:
        raise ValueError(f"expected one running {container} container, observed {len(rows)}")
    return rows[0]


def verify(
    record: dict[str, Any],
    evidence: dict[str, Any],
    commit: str,
    stage: str,
    run: str,
    attempt: str,
) -> None:
    """Check the commit, all six workload roles, requested references and running identities."""
    if record["commit"] != commit or evidence["commit"] != commit:
        raise ValueError("image evidence belongs to another commit")
    if stage not in {"platform", "hosted", "worker"} or evidence["producer"] != {
        "stage": stage,
        "job": "cluster",
        "run": run,
        "attempt": attempt,
    }:
        raise ValueError("image evidence producer differs from the successful stage attempt")
    roles = workloads(evidence["session"])
    observations = evidence["observations"]
    if set(observations) != set(roles) or set(roles) != set(record["images"]):
        raise ValueError("expected evidence for every published workload")
    for name, (namespace, _, container) in roles.items():
        image = record["images"][name]
        expected = f"{image['repository']}@{image['digest']}"
        row = observations[name]
        if (
            row["namespace"] != namespace
            or row["container"] != container
            or not row["pod"]
            or not row["pod_uid"]
            or row["image"] != expected
            or not row["node"]
            or row["platform"] != "linux/amd64"
        ):
            raise ValueError(f"{name} observation does not identify its published image")
        check_runtime(expected, row["image_id"], "amd64", row["manifests"], row["config_id"])


def main() -> None:
    """Collect evidence during qualification or verify a retained observation at release."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("observe", "verify"))
    parser.add_argument("--record", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--stage", required=True, choices=("platform", "hosted", "worker"))
    parser.add_argument("--run", required=True)
    parser.add_argument("--attempt", required=True)
    parser.add_argument("--context")
    parser.add_argument("--session")
    args = parser.parse_args()
    record = read_record(args.record)
    if args.action == "verify":
        verify(
            record,
            json.loads(args.evidence.read_text()),
            args.commit,
            args.stage,
            args.run,
            args.attempt,
        )
        return
    if not args.context or not args.session:
        parser.error("observation requires a cluster context and hosted session")
    rows = {}
    for name, (namespace, selector, container) in workloads(args.session).items():
        pods = subprocess.check_output(
            [
                "kubectl",
                "--context",
                args.context,
                "get",
                "pods",
                "--namespace",
                namespace,
                "--selector",
                selector,
                "--output=json",
            ],
            timeout=30,
        )
        row = observation(json.loads(pods)["items"], container)
        node = row["node"]
        info = json.loads(
            subprocess.check_output(
                ["kubectl", "--context", args.context, "get", "node", node, "-o", "json"],
                timeout=30,
            )
        )["status"]["nodeInfo"]
        row["platform"] = info["operatingSystem"] + "/" + info["architecture"]
        # kind nodes are Docker containers. Read the immutable manifests actually held
        # in this node's content store, without resolving a mutable registry tag.
        row.update(node_identity(node, row["image"], info["architecture"]))
        rows[name] = row
    evidence = {
        "commit": args.commit,
        "session": args.session,
        "observations": rows,
        "producer": {
            "stage": args.stage,
            "job": "cluster",
            "run": args.run,
            "attempt": args.attempt,
        },
    }
    verify(record, evidence, args.commit, args.stage, args.run, args.attempt)
    args.evidence.write_text(json.dumps(evidence, indent=2) + "\n")


if __name__ == "__main__":
    main()
