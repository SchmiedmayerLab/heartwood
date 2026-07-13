#!/usr/bin/env bash

# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

set -euo pipefail

mode="${1:-}"
version="${2:-}"
git_sha="${3:-}"
image_name="${4:-ghcr.io/schmiedmayerlab/heartwood}"

if [[ "${mode}" != "verify" && "${mode}" != "promote" ]]; then
  echo "usage: promote-release-images.sh <verify|promote> <version> <git-sha> [image-name]" >&2
  exit 64
fi
python3 "$(dirname "${BASH_SOURCE[0]}")/verify_release_candidate.py" \
  --version "${version}" --version-only >/dev/null
if [[ ! "${git_sha}" =~ ^[0-9a-f]{40}$ ]]; then
  echo "invalid Git commit SHA: ${git_sha}" >&2
  exit 64
fi

declare -a mappings=(
  "sha-${git_sha}|${version//+/_}|multi"
  "sha-${git_sha}-terra|${version//+/_}-terra|single"
  "sha-${git_sha}-gpu-nvidia|${version//+/_}-gpu-nvidia|amd64"
  "sha-${git_sha}-terra-gpu-nvidia|${version//+/_}-terra-gpu-nvidia|single"
)

imagetools_inspect() {
  local output
  for attempt in 1 2 3 4 5; do
    if output="$(docker buildx imagetools inspect "$@")"; then
      printf '%s\n' "${output}"
      return 0
    fi
    echo "registry inspection failed (attempt ${attempt}/5)" >&2
    sleep 10
  done
  return 1
}

imagetools_create() {
  for attempt in 1 2 3 4 5; do
    if docker buildx imagetools create "$@"; then
      return 0
    fi
    echo "registry publication failed (attempt ${attempt}/5)" >&2
    sleep 10
  done
  return 1
}

digest() {
  imagetools_inspect "$1" | awk '$1 == "Digest:" {print $2; exit}'
}

for mapping in "${mappings[@]}"; do
  IFS='|' read -r source_tag target_tag media_shape <<<"${mapping}"
  source_ref="${image_name}:${source_tag}"
  target_ref="${image_name}:${target_tag}"
  echo "verifying release image candidate: ${source_ref}"
  source_digest="$(digest "${source_ref}")"
  if [[ ! "${source_digest}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    echo "release image candidate returned an invalid digest: ${source_ref} (${source_digest:-<empty>})" >&2
    exit 1
  fi

  raw="$(imagetools_inspect --raw "${source_ref}")"
  media_type="$(jq -r '.mediaType // "<missing>"' <<<"${raw}")"
  config_media_type="$(jq -r '.config.mediaType // "<missing>"' <<<"${raw}")"
  platforms="$(jq -r '[.manifests[]? | select(.platform.os == "linux") | "\(.platform.os)/\(.platform.architecture)"] | join(", ")' <<<"${raw}")"
  if [[ "${media_shape}" == "single" ]]; then
    if ! jq -e '
      .mediaType == "application/vnd.docker.distribution.manifest.v2+json"
      and .config.mediaType == "application/vnd.docker.container.image.v1+json"
    ' <<<"${raw}" >/dev/null; then
      echo "release image candidate is not a Docker schema-2 manifest: ${source_ref}" >&2
      echo "observed media type: ${media_type}; config media type: ${config_media_type}" >&2
      exit 1
    fi
  elif [[ "${media_shape}" == "multi" ]]; then
    if ! jq -e '
      ([.manifests[] | select(.platform.os == "linux") | "\(.platform.os)/\(.platform.architecture)"] | sort)
      == ["linux/amd64", "linux/arm64"]
    ' <<<"${raw}" >/dev/null; then
      echo "release image candidate is not an AMD64/ARM64 index: ${source_ref}" >&2
      echo "observed media type: ${media_type}; Linux platforms: ${platforms:-<none>}" >&2
      exit 1
    fi
  else
    if ! jq -e '
      [.manifests[] | select(.platform.os == "linux") | "\(.platform.os)/\(.platform.architecture)"]
      == ["linux/amd64"]
    ' <<<"${raw}" >/dev/null; then
      echo "release image candidate is not an AMD64 index: ${source_ref}" >&2
      echo "observed media type: ${media_type}; Linux platforms: ${platforms:-<none>}" >&2
      exit 1
    fi
  fi

  if [[ "${mode}" == "verify" ]]; then
    continue
  fi
  if target_digest="$(docker buildx imagetools inspect "${target_ref}" 2>/dev/null | awk '$1 == "Digest:" {print $2; exit}')"; then
    if [[ "${target_digest}" != "${source_digest}" ]]; then
      echo "release image tag already exists with a different digest: ${target_ref}" >&2
      exit 1
    fi
    continue
  fi
  if [[ "${media_shape}" == "single" ]]; then
    imagetools_create --prefer-index=false --tag "${target_ref}" "${source_ref}"
  else
    imagetools_create --tag "${target_ref}" "${source_ref}"
  fi
  target_digest="$(digest "${target_ref}")"
  if [[ "${target_digest}" != "${source_digest}" ]]; then
    echo "promoted release image digest does not match ${source_ref}" >&2
    exit 1
  fi
done
