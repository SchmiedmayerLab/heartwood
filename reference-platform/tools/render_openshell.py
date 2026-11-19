#!/usr/bin/env python3
# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Render the OpenShell objects of one session namespace from the pinned chart.

The control service applies the objects without Helm. The render carries a namespace
placeholder and a gateway image placeholder, which the control service replaces.
RBAC creation is switched off in the chart: the platform chart ships fixed ClusterRoles.

Usage: render_openshell.py [--check] [--chart ARCHIVE]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

CHART = "oci://ghcr.io/nvidia/openshell/helm-chart"
CHART_VERSION = "0.1.9"
CHART_SHA256 = "d665bdb473d6808473e5abca25ae18301ff8c0ed242f5cae2be7d4fc482d6339"

NAMESPACE_PLACEHOLDER = "hw-s-placeholder"
GATEWAY_IMAGE_PLACEHOLDER = "heartwood.invalid/openshell-gateway:placeholder"

_TOOLS = Path(__file__).resolve().parent
VALUES = _TOOLS / "openshell-values.yaml"
OUTPUT = _TOOLS.parent / "chart" / "heartwood-platform" / "files" / "openshell-session.json"

# The kinds that the ClusterRole heartwood-session-control lets the control service apply.
_KINDS = frozenset(
    {"ConfigMap", "Job", "NetworkPolicy", "Service", "ServiceAccount", "StatefulSet"}
)
# The chart renders these with the certificate Job. The control service binds the
# ClusterRole heartwood-openshell-certgen in their place.
_REMOVED = frozenset({("Role", "openshell-certgen"), ("RoleBinding", "openshell-certgen")})

# Every string that names the session namespace, with its number of occurrences.
# A changed set means the chart refers to the namespace in a new place.
NAMESPACE_OCCURRENCES = {
    "ConfigMap/openshell-config:data.gateway.toml": 3,
    "Job/openshell-certgen:spec.template.spec.containers.0.args.5": 1,
    "Job/openshell-certgen:spec.template.spec.containers.0.args.6": 1,
    "NetworkPolicy/openshell-sandbox-ssh:"
    "spec.ingress.0.from.0.namespaceSelector.matchLabels.kubernetes.io/metadata.name": 1,
    "StatefulSet/openshell:spec.template.spec.containers.0.env.8.value": 1,
}


class RenderError(Exception):
    """The chart rendered something the session objects cannot hold."""


def _strings(value: object, path: str = "") -> list[tuple[str, str]]:
    if isinstance(value, str):
        return [(path, value)]
    if isinstance(value, dict):
        items = [(f"{path}.{key}" if path else str(key), item) for key, item in value.items()]
    elif isinstance(value, list):
        items = [(f"{path}.{index}", item) for index, item in enumerate(value)]
    else:
        return []
    return [found for item_path, item in items for found in _strings(item, item_path)]


def session_objects(rendered: str) -> list[dict[str, Any]]:
    """Turn the chart's output into the objects of one session namespace."""
    objects: list[dict[str, Any]] = []
    for document in yaml.safe_load_all(rendered):
        if document is None:
            continue
        kind, metadata = document["kind"], document["metadata"]
        if (kind, metadata["name"]) in _REMOVED:
            continue
        if kind not in _KINDS:
            raise RenderError(f"unexpected object {kind}/{metadata['name']}")
        metadata["namespace"] = NAMESPACE_PLACEHOLDER
        # Helm hook annotations have no effect outside Helm.
        annotations = {
            key: value
            for key, value in metadata.get("annotations", {}).items()
            if not key.startswith("helm.sh/hook")
        }
        metadata.pop("annotations", None)
        if annotations:
            metadata["annotations"] = annotations
        objects.append(document)
    objects.sort(key=lambda item: (item["kind"], item["metadata"]["name"]))

    occurrences: Counter[str] = Counter()
    images = 0
    for item in objects:
        prefix = f"{item['kind']}/{item['metadata']['name']}:"
        for path, value in _strings(item):
            if path != "metadata.namespace" and NAMESPACE_PLACEHOLDER in value:
                occurrences[prefix + path] = value.count(NAMESPACE_PLACEHOLDER)
            if path.endswith(".image"):
                images += 1
                if value != GATEWAY_IMAGE_PLACEHOLDER:
                    raise RenderError(f"unexpected image {value} in {prefix}{path}")
    if dict(occurrences) != NAMESPACE_OCCURRENCES:
        raise RenderError(f"namespace occurrences changed: {dict(sorted(occurrences.items()))}")
    if images != 2:
        raise RenderError(f"expected the gateway and the certificate Job image, found {images}")
    return objects


def serialize(objects: list[dict[str, Any]]) -> str:
    """Serialize the objects as one List that kubectl applies."""
    document = {"apiVersion": "v1", "kind": "List", "items": objects}
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def _run(*command: str) -> str:
    return subprocess.run(command, check=True, capture_output=True, text=True).stdout


def _pull(directory: Path) -> Path:
    _run("helm", "pull", CHART, "--version", CHART_VERSION, "--destination", str(directory))
    (archive,) = directory.glob("*.tgz")
    return archive


def render(archive: Path | None) -> str:
    """Render the pinned chart, pulling it unless an archive is given."""
    with tempfile.TemporaryDirectory() as directory:
        chart = archive or _pull(Path(directory))
        digest = hashlib.sha256(chart.read_bytes()).hexdigest()
        if digest != CHART_SHA256:
            raise RenderError(f"chart archive has SHA-256 {digest}, expected {CHART_SHA256}")
        rendered = _run(
            "helm",
            "template",
            "openshell",
            str(chart),
            "--namespace",
            NAMESPACE_PLACEHOLDER,
            "--values",
            str(VALUES),
        )
    return serialize(session_objects(rendered))


def main(arguments: list[str] | None = None) -> int:
    """Write the render, or compare it with the committed one."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="compare with the committed render")
    parser.add_argument("--chart", type=Path, help="use this chart archive and do not pull")
    options = parser.parse_args(arguments)
    try:
        rendered = render(options.chart)
    except (RenderError, subprocess.CalledProcessError) as error:
        detail = error.stderr if isinstance(error, subprocess.CalledProcessError) else error
        print(f"render failed: {detail}", file=sys.stderr)
        return 1
    if not options.check:
        OUTPUT.write_text(rendered, encoding="utf-8")
        return 0
    if OUTPUT.read_text(encoding="utf-8") != rendered:
        print(f"{OUTPUT} differs from a fresh render; run render_openshell.py", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
