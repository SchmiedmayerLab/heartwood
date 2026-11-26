# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Image identities verify only against the exact bytes of their pinned manifest chain."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

import pytest

from heartwood.gateway.worker_image_identity import check_runtime, manifest_chain

_REPOSITORY = "registry.test/heartwood/worker"
_MEDIA = "application/vnd.oci.image.manifest.v1+json"
_INDEX = "application/vnd.oci.image.index.v1+json"
_CONFIG = "sha256:" + "1" * 64


def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _store(*documents: dict[str, Any]) -> tuple[list[str], dict[str, bytes]]:
    blobs = {}
    for document in documents:
        raw = json.dumps(document).encode()
        blobs[_digest(raw)] = raw
    return list(blobs), blobs


def _leaf(config: str = _CONFIG, **changes: object) -> dict[str, Any]:
    return {"schemaVersion": 2, "mediaType": _MEDIA, "config": {"digest": config}, **changes}


def _index(*children: tuple[str, str], media: str = _MEDIA) -> dict[str, Any]:
    return {
        "schemaVersion": 2,
        "mediaType": _INDEX,
        "manifests": [
            {"mediaType": media, "digest": digest, "platform": {"os": "linux", "architecture": a}}
            for digest, a in children
        ],
    }


def _indexed() -> tuple[str, str, dict[str, bytes]]:
    (child, other), blobs = _store(_leaf(), _leaf("sha256:" + "2" * 64))
    (root,), index = _store(_index((child, "amd64"), (other, "arm64")))
    return root, child, {**blobs, **index}


def test_an_index_resolves_to_the_child_of_the_node_platform() -> None:
    root, child, blobs = _indexed()
    chain = manifest_chain(f"{_REPOSITORY}@{root}", "amd64", blobs.__getitem__)
    assert list(chain) == [root, child]
    assert chain[child] == blobs[child].decode()


def test_a_trailing_line_ending_of_the_transport_is_removed_only_to_match_the_pin() -> None:
    (leaf,), blobs = _store(_leaf())
    chain = manifest_chain(f"{_REPOSITORY}@{leaf}", "amd64", lambda d: blobs[d] + b"\n")
    assert chain == {leaf: blobs[leaf].decode()}


def _unindexed(document: dict[str, Any]) -> tuple[str, Callable[[str], bytes]]:
    (leaf,), blobs = _store(document)
    return f"{_REPOSITORY}@{leaf}", blobs.__getitem__


@pytest.mark.parametrize(
    ("chain", "message"),
    [
        (lambda: (f"{_REPOSITORY}@sha256:short", bytes), "invalid manifest digest"),
        (lambda: (f"{_REPOSITORY}@{_CONFIG}", lambda _: b"{}"), "differ from their digest"),
        (lambda: (_CONFIG, bytes), "pinned by repository and digest"),
        (lambda: ("@" + _CONFIG, bytes), "pinned by repository and digest"),
        (lambda: _unindexed(_leaf(schemaVersion=1)), "unexpected manifest schema version"),
        (lambda: _unindexed([_leaf()]), "unexpected manifest schema version"),  # type: ignore[arg-type]
        (lambda: _unindexed(_leaf(mediaType="text/plain")), "invalid image manifest"),
        (lambda: _unindexed(_leaf("sha256:short")), "invalid image manifest"),
    ],
)
def test_unpinned_altered_or_unexpected_manifests_are_refused(
    chain: Callable[[], tuple[str, Callable[[str], bytes]]], message: str
) -> None:
    reference, read = chain()
    with pytest.raises(ValueError, match=message):
        manifest_chain(reference, "amd64", read)


@pytest.mark.parametrize(
    "children",
    [
        [],
        [("amd64", _MEDIA), ("amd64", _MEDIA)],
        [("arm64", _MEDIA)],
        [("amd64", "application/vnd.oci.image.config.v1+json")],
    ],
)
def test_an_index_must_name_exactly_one_manifest_for_the_node_platform(
    children: list[tuple[str, str]],
) -> None:
    (child,), blobs = _store(_leaf())
    entries = [_index((child, a), media=media)["manifests"][0] for a, media in children]
    (root,), index = _store({**_index(), "manifests": entries})
    with pytest.raises(ValueError, match="one manifest for the node platform"):
        manifest_chain(f"{_REPOSITORY}@{root}", "amd64", {**blobs, **index}.__getitem__)


def _manifests(blobs: dict[str, bytes]) -> dict[str, str]:
    return {digest: raw.decode() for digest, raw in blobs.items()}


@pytest.mark.parametrize(
    "image_id",
    [
        "{root}",
        f"docker-pullable://{_REPOSITORY}@{{child}}",
        f"containerd://{_CONFIG}",
        _CONFIG,
    ],
)
def test_the_runtime_may_report_any_digest_of_the_selected_chain(image_id: str) -> None:
    root, child, blobs = _indexed()
    reported = image_id.format(root=f"{_REPOSITORY}@{root}", child=child)
    check_runtime(f"{_REPOSITORY}@{root}", reported, "amd64", _manifests(blobs), _CONFIG)


@pytest.mark.parametrize(
    ("image_id", "config", "message"),
    [
        ("{root}", "sha256:" + "2" * 64, "configuration differs"),
        ("cri-o://{root}", _CONFIG, "unknown runtime image ID format"),
        ("other.test/worker@{child}", _CONFIG, "repository differs"),
        (_REPOSITORY + "@sha256:" + "2" * 64, _CONFIG, "outside the selected manifest chain"),
    ],
)
def test_runtime_identities_outside_the_pinned_chain_are_refused(
    image_id: str, config: str, message: str
) -> None:
    root, child, blobs = _indexed()
    reported = image_id.format(root=f"{_REPOSITORY}@{root}", child=child)
    with pytest.raises(ValueError, match=message):
        check_runtime(f"{_REPOSITORY}@{root}", reported, "amd64", _manifests(blobs), config)
