#!/usr/bin/env bash
# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

# Deletes a kind cluster that create-cluster.sh created.
# Usage: delete-cluster.sh NAME

set -euo pipefail

kind delete cluster --name "${1:?cluster name is required}"
