#!/usr/bin/env bash
# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

# Deletes a kind cluster that create-cluster.sh created, and its local registry.
# Usage: delete-cluster.sh NAME

set -euo pipefail

name="${1:?cluster name is required}"

kind delete cluster --name "${name}"
if docker container inspect "${name}-registry" >/dev/null 2>&1; then
  docker rm --force --volumes "${name}-registry" >/dev/null
fi
