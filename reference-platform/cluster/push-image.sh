#!/usr/bin/env bash
# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

# Pushes a local image to the registry of a kind cluster that create-cluster.sh created
# and prints the reference, pinned by digest, under which the cluster pulls it.
# Usage: push-image.sh NAME IMAGE

set -euo pipefail

name="${1:?cluster name is required}"
image="${2:?image is required}"
registry="${name}-registry"
crane_image="gcr.io/go-containerregistry/crane:v0.22.1@sha256:1f968817b95790bed063f71175aa6b8ff879fa17064020415f3e18bb6e6a36e1"

repository="${image##*/}"
repository="${repository%%[:@]*}"

# The push runs in a container on the kind network. The Docker daemon cannot reach a
# registry on the host's loopback address when it runs in a virtual machine.
archive="$(mktemp -d)"
trap 'rm -rf "${archive}"' EXIT
docker save --output "${archive}/image.tar" "${image}"
chmod 0755 "${archive}" && chmod 0644 "${archive}/image.tar"
reference="$(docker run --rm --network kind --volume "${archive}:/image:ro" "${crane_image}" \
  push --insecure /image/image.tar "${registry}:5000/${repository}:candidate")"
if [[ ! "${reference}" =~ ^${registry}:5000/${repository}@sha256:[0-9a-f]{64}$ ]]; then
  echo "The registry returned no digest for ${image}." >&2
  exit 1
fi
echo "${reference}"
