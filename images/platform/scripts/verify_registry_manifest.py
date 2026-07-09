# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Verify published platform image tags against their registry manifest contract."""

# ruff: noqa: D101,D102,D103,D107

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DOCKER_IMAGE_MANIFEST = "application/vnd.docker.distribution.manifest.v2+json"
DOCKER_MANIFEST_LIST = "application/vnd.docker.distribution.manifest.list.v2+json"
DOCKER_CONFIG = "application/vnd.docker.container.image.v1+json"
OCI_IMAGE_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
OCI_IMAGE_INDEX = "application/vnd.oci.image.index.v1+json"
OCI_CONFIG = "application/vnd.oci.image.config.v1+json"

IMAGE_MANIFEST_MEDIA_TYPES = {DOCKER_IMAGE_MANIFEST, OCI_IMAGE_MANIFEST}
INDEX_MEDIA_TYPES = {DOCKER_MANIFEST_LIST, OCI_IMAGE_INDEX}
DEFAULT_CONFIG_MEDIA_TYPES = {
    DOCKER_IMAGE_MANIFEST: DOCKER_CONFIG,
    OCI_IMAGE_MANIFEST: OCI_CONFIG,
}


@dataclass(frozen=True)
class ExpectedTag:
    label: str
    tag: str


@dataclass(frozen=True)
class RegistryReference:
    registry: str
    repository: str


@dataclass(frozen=True)
class RegistryResponse:
    content_type: str
    payload: dict[str, Any]


class RegistryClient:
    def __init__(self, image_name: str, scheme: str) -> None:
        reference = parse_image_name(image_name)
        self.registry = reference.registry
        self.repository = reference.repository
        self.scheme = scheme
        self._authorization: str | None = None

    def fetch_json(self, path: str, accept: str | None = None) -> RegistryResponse:
        headers: dict[str, str] = {}
        if accept is not None:
            headers["Accept"] = accept
        if self._authorization is not None:
            headers["Authorization"] = self._authorization

        request = urllib.request.Request(f"{self.scheme}://{self.registry}{path}", headers=headers)
        try:
            return _read_json_response(request)
        except urllib.error.HTTPError as error:
            if error.code != 401 or self._authorization is not None:
                raise registry_error(request.full_url, error) from error
            self._authorization = self._authorize(error.headers.get("WWW-Authenticate"))
            retry_headers = dict(headers)
            retry_headers["Authorization"] = self._authorization
            retry = urllib.request.Request(request.full_url, headers=retry_headers)
            try:
                return _read_json_response(retry)
            except urllib.error.HTTPError as retry_error:
                raise registry_error(request.full_url, retry_error) from retry_error

    def manifest_path(self, tag: str) -> str:
        repository = urllib.parse.quote(self.repository, safe="/")
        encoded_tag = urllib.parse.quote(tag, safe="")
        return f"/v2/{repository}/manifests/{encoded_tag}"

    def blob_path(self, digest: str) -> str:
        repository = urllib.parse.quote(self.repository, safe="/")
        encoded_digest = urllib.parse.quote(digest, safe=":")
        return f"/v2/{repository}/blobs/{encoded_digest}"

    def _authorize(self, challenge: str | None) -> str:
        if challenge is None:
            raise SystemExit(
                f"{self.registry} requires authentication but did not return a challenge"
            )
        values = parse_bearer_challenge(challenge)
        realm = values.get("realm")
        if realm is None:
            raise SystemExit(f"{self.registry} returned an unsupported auth challenge: {challenge}")
        query: dict[str, str] = {}
        if "service" in values:
            query["service"] = values["service"]
        query["scope"] = values.get("scope", f"repository:{self.repository}:pull")
        separator = "&" if urllib.parse.urlparse(realm).query else "?"
        token_url = realm + separator + urllib.parse.urlencode(query)
        request = urllib.request.Request(token_url)
        try:
            response = _read_json_response(request)
        except urllib.error.HTTPError as error:
            raise registry_error(token_url, error) from error
        token = response.payload.get("token") or response.payload.get("access_token")
        if not isinstance(token, str):
            raise SystemExit(f"{self.registry} token response did not contain a token")
        return f"Bearer {token}"


def parse_image_name(image_name: str) -> RegistryReference:
    if "://" in image_name:
        raise SystemExit("image name must not include a URL scheme")
    parts = image_name.split("/", 1)
    if len(parts) != 2 or "." not in parts[0]:
        raise SystemExit(f"image name must include an explicit registry host: {image_name}")
    return RegistryReference(registry=parts[0], repository=parts[1])


def parse_bearer_challenge(challenge: str) -> dict[str, str]:
    if not challenge.startswith("Bearer "):
        return {}
    return dict(re.findall(r'([A-Za-z_][A-Za-z0-9_-]*)="([^"]*)"', challenge[len("Bearer ") :]))


def registry_error(url: str, error: urllib.error.HTTPError) -> SystemExit:
    details = error.read().decode("utf-8", "replace")
    return SystemExit(f"{url} returned HTTP {error.code}: {details}")


def _read_json_response(request: urllib.request.Request) -> RegistryResponse:
    with urllib.request.urlopen(request, timeout=30) as response:
        content_type = response.headers.get_content_type()
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit(f"{request.full_url} did not return a JSON object")
    return RegistryResponse(content_type=content_type, payload=payload)


def verify_tag(
    client: RegistryClient,
    tag: ExpectedTag,
    expected_media_type: str,
    expected_platforms: set[str],
    expected_config_media_type: str | None,
    allow_non_platform_manifests: bool,
) -> None:
    image_ref = f"{client.registry}/{client.repository}:{tag.tag}"
    print(f"Verifying {tag.label} platform manifest for {image_ref}")
    response = client.fetch_json(client.manifest_path(tag.tag), accept=expected_media_type)
    manifest = response.payload

    if response.content_type != expected_media_type:
        raise SystemExit(
            f"{image_ref} returned {response.content_type}, expected {expected_media_type}"
        )
    if manifest.get("mediaType") != expected_media_type:
        raise SystemExit(f"{image_ref} manifest media type does not match {expected_media_type}")

    if expected_media_type in IMAGE_MANIFEST_MEDIA_TYPES:
        verify_image_manifest(
            client, image_ref, manifest, expected_platforms, expected_config_media_type
        )
        return
    if expected_media_type in INDEX_MEDIA_TYPES:
        verify_image_index(image_ref, manifest, expected_platforms, allow_non_platform_manifests)
        return
    raise SystemExit(f"{image_ref} uses unsupported manifest media type {expected_media_type}")


def verify_image_manifest(
    client: RegistryClient,
    image_ref: str,
    manifest: dict[str, Any],
    expected_platforms: set[str],
    expected_config_media_type: str | None,
) -> None:
    if "manifests" in manifest:
        raise SystemExit(f"{image_ref} returned an image index instead of one image manifest")
    if len(expected_platforms) != 1:
        raise SystemExit(
            f"{image_ref} is a single manifest but expects {sorted(expected_platforms)}"
        )

    config = manifest.get("config")
    if not isinstance(config, dict):
        raise SystemExit(f"{image_ref} has no config descriptor")
    if (
        expected_config_media_type is not None
        and config.get("mediaType") != expected_config_media_type
    ):
        raise SystemExit(
            f"{image_ref} config media type is {config.get('mediaType')}, "
            f"expected {expected_config_media_type}"
        )
    digest = config.get("digest")
    if not isinstance(digest, str):
        raise SystemExit(f"{image_ref} has no config digest")
    config_response = client.fetch_json(client.blob_path(digest))
    image_config = config_response.payload
    platform = f"{image_config.get('os')}/{image_config.get('architecture')}"
    if platform not in expected_platforms:
        raise SystemExit(f"{image_ref} is {platform}, expected {sorted(expected_platforms)}")


def verify_image_index(
    image_ref: str,
    manifest: dict[str, Any],
    expected_platforms: set[str],
    allow_non_platform_manifests: bool,
) -> None:
    entries = manifest.get("manifests")
    if not isinstance(entries, list):
        raise SystemExit(f"{image_ref} image index has no manifest list")

    platforms: set[str] = set()
    extras: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise SystemExit(f"{image_ref} image index contains a malformed entry")
        platform = entry.get("platform")
        if not isinstance(platform, dict):
            extras.append(str(entry.get("mediaType", "unknown")))
            continue
        os_name = platform.get("os")
        architecture = platform.get("architecture")
        if not isinstance(os_name, str) or not isinstance(architecture, str):
            extras.append(str(entry.get("mediaType", "unknown")))
            continue
        platform_name = f"{os_name}/{architecture}"
        if platform_name == "unknown/unknown":
            extras.append(str(entry.get("mediaType", "unknown")))
            continue
        platforms.add(platform_name)

    if extras and not allow_non_platform_manifests:
        raise SystemExit(f"{image_ref} contains non-platform manifest entries: {extras}")
    if platforms != expected_platforms:
        raise SystemExit(
            f"{image_ref} platforms are {sorted(platforms)}, expected {sorted(expected_platforms)}"
        )


def expected_tags(platform: dict[str, Any], image_channel: str, git_sha: str) -> list[ExpectedTag]:
    tags: list[ExpectedTag] = []
    for key, label in (
        ("runtime_tag", "runtime moving tag"),
        ("commit_runtime_tag", "runtime commit tag"),
        ("smoke_tag", "smoke moving tag"),
        ("commit_smoke_tag", "smoke commit tag"),
    ):
        value = platform.get(key)
        if not isinstance(value, str):
            continue
        rendered = value.replace("<git-sha>", git_sha).replace("<image-channel>", image_channel)
        tags.append(ExpectedTag(label=label, tag=rendered))
    if not tags:
        raise SystemExit("platform manifest does not define any runtime or smoke tags")
    return tags


def load_platform(manifest_path: Path, platform_name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    platforms = manifest.get("platforms")
    if not isinstance(platforms, dict) or platform_name not in platforms:
        raise SystemExit(f"{manifest_path} does not define platform {platform_name}")
    platform = platforms[platform_name]
    if not isinstance(platform, dict):
        raise SystemExit(f"{manifest_path} platform {platform_name} is malformed")
    return manifest, platform


def parse_platforms(platform: dict[str, Any]) -> set[str]:
    supported = platform.get("supported_platforms")
    if not isinstance(supported, list) or not supported:
        raise SystemExit("platform manifest must define supported_platforms")
    platforms = set()
    for item in supported:
        if not isinstance(item, str) or "/" not in item:
            raise SystemExit(f"invalid supported platform entry: {item}")
        platforms.add(item)
    return platforms


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path("images/platforms.toml"))
    parser.add_argument("--platform", required=True)
    parser.add_argument("--image-name")
    parser.add_argument("--image-channel", default="edge")
    parser.add_argument("--git-sha", required=True)
    parser.add_argument("--registry-scheme", choices=["https", "http"], default="https")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    manifest, platform = load_platform(args.manifest, args.platform)
    image_name = args.image_name or manifest.get("image_name")
    if not isinstance(image_name, str):
        raise SystemExit("image name must be supplied by --image-name or the platform manifest")
    expected_media_type = platform.get("manifest_media_type")
    if not isinstance(expected_media_type, str):
        raise SystemExit("platform manifest must define manifest_media_type")
    expected_config_media_type = platform.get(
        "config_media_type", DEFAULT_CONFIG_MEDIA_TYPES.get(expected_media_type)
    )
    if expected_config_media_type is not None and not isinstance(expected_config_media_type, str):
        raise SystemExit("platform manifest config_media_type must be a string")

    supported_platforms = parse_platforms(platform)
    tags = expected_tags(platform, image_channel=args.image_channel, git_sha=args.git_sha)
    allow_non_platform_manifests = bool(platform.get("allow_non_platform_manifests", False))
    client = RegistryClient(image_name=image_name, scheme=args.registry_scheme)
    for tag in tags:
        verify_tag(
            client=client,
            tag=tag,
            expected_media_type=expected_media_type,
            expected_platforms=supported_platforms,
            expected_config_media_type=expected_config_media_type,
            allow_non_platform_manifests=allow_non_platform_manifests,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
