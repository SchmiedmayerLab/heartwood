# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Resolve content-addressed image manifests for the kind runner's Linux platform."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

# The release runner has the checkout and a system Python without gateway dependencies.
sys.path.insert(
    0, str(Path(__file__).resolve().parents[2] / "packages/gateway/src/heartwood/gateway")
)
from worker_image_identity import check_runtime as check_runtime
from worker_image_identity import manifest_chain


def node_identity(node: str, reference: str, architecture: str) -> dict[str, Any]:
    """Read the exact manifests from a kind node's immutable content store."""
    blobs = manifest_chain(
        reference,
        architecture,
        lambda digest: subprocess.check_output(
            ["docker", "exec", node, "ctr", "-n", "k8s.io", "content", "get", digest], timeout=30
        ),
    )

    inspected = subprocess.check_output(
        ["docker", "exec", node, "crictl", "inspecti", "--output", "json", reference], timeout=30
    )
    return {"manifests": blobs, "config_id": json.loads(inspected)["status"]["id"]}


def main() -> None:
    """Prepare digest evidence for the native worker cases inside their unprivileged controller."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--images", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    nodes = json.loads(
        subprocess.check_output(["kubectl", "--context", args.context, "get", "nodes", "-ojson"])
    )["items"]
    if not nodes or any(
        n["status"]["nodeInfo"]["architecture"] != "amd64"
        or n["status"]["nodeInfo"]["operatingSystem"] != "linux"
        for n in nodes
    ):
        raise ValueError("qualification requires Linux amd64 kind nodes")
    refs = json.loads(args.images.read_text())["image_references"]
    evidence = {}
    for item in nodes:
        node = item["metadata"]["name"]
        images = {}
        for role in ("worker", "replacement_worker", "supervisor", "sandbox"):
            reference = refs[role]
            subprocess.run(
                ["docker", "exec", node, "crictl", "pull", reference], check=True, timeout=300
            )
            images[reference] = {
                "role": "worker" if role == "replacement_worker" else role,
                **node_identity(node, reference, "amd64"),
            }
        evidence[node] = {"platform": "linux/amd64", "images": images}
    args.output.write_text(json.dumps(evidence))


if __name__ == "__main__":
    main()
