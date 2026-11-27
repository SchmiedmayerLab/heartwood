# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Verify an image identity against the pinned manifest and its Linux platform child."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from typing import Any

INDEX = {
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
}
MANIFEST = {
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
}


def manifest_chain(reference: str, platform: str, read: Callable[[str], bytes]) -> dict[str, str]:
    """Keep the exact bytes of the pinned manifest and its platform child, if indexed."""
    blobs = {}

    def load(digest: str) -> dict[str, Any]:
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise ValueError("invalid manifest digest")
        raw = read(digest)
        # Some CLI transports append a line ending. Only accept bytes matching the pin.
        for candidate in (raw, raw.removesuffix(b"\n")):
            if "sha256:" + hashlib.sha256(candidate).hexdigest() == digest:
                blobs[digest] = candidate.decode()
                document = json.loads(candidate)
                if not isinstance(document, dict) or document.get("schemaVersion") != 2:
                    raise ValueError("unexpected manifest schema version")
                return document
        raise ValueError("manifest bytes differ from their digest")

    repository, separator, root = reference.partition("@")
    if not separator or not repository:
        raise ValueError("expected an image pinned by repository and digest")
    manifest = load(root)
    if manifest.get("mediaType") in INDEX:
        children = [
            d
            for d in manifest["manifests"]
            if d.get("platform", {}).get("os") == "linux"
            and d["platform"].get("architecture") == platform
            and d["mediaType"] in MANIFEST
        ]
        if len(children) != 1:
            raise ValueError("index must identify one manifest for the node platform")
        manifest = load(children[0]["digest"])
    if manifest.get("mediaType") not in MANIFEST or not re.fullmatch(
        r"sha256:[0-9a-f]{64}", manifest["config"]["digest"]
    ):
        raise ValueError("invalid image manifest")
    return blobs


def check_runtime(
    reference: str, image_id: str, platform: str, manifests: dict[str, str], config_id: str
) -> None:
    """Accept a pinned manifest, its selected child, or that child's image configuration."""
    checked = manifest_chain(reference, platform, lambda digest: manifests[digest].encode())
    leaf = json.loads(next(reversed(checked.values())))
    if config_id != leaf["config"]["digest"]:
        raise ValueError("node image configuration differs from the selected manifest")
    scheme, prefix, value = image_id.partition("://")
    if not prefix:
        value = image_id
    elif scheme not in {"docker-pullable", "containerd"}:
        raise ValueError("unknown runtime image ID format")
    repository, separator, digest = value.rpartition("@")
    if separator and repository != reference.partition("@")[0]:
        raise ValueError("runtime repository differs from the requested image")
    if digest not in {*checked, leaf["config"]["digest"]}:
        raise ValueError("runtime digest is outside the selected manifest chain")
