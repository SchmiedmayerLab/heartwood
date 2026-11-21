#!/usr/bin/env bash
# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

# Installs or upgrades the platform chart on a kind cluster that create-cluster.sh
# created, with the kind values, and waits until every component is ready.
# Further arguments are passed to helm.
# Usage: install-chart.sh NAME [HELM ARGUMENT...]

set -euo pipefail

name="${1:?cluster name is required}"
shift
chart="$(cd "$(dirname "${BASH_SOURCE[0]}")/../chart/heartwood-platform" && pwd)"

helm --kube-context "kind-${name}" upgrade --install heartwood-platform "${chart}" \
  --namespace heartwood-platform --create-namespace \
  --values "${chart}/values-kind.yaml" --wait --timeout 10m "$@"
