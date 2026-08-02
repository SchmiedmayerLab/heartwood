#!/usr/bin/env bash

# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

set -euo pipefail

expected_sha="${1:?Expected workflow revision is required}"
current_main="${2:?Current main revision is required}"
github_output="${GITHUB_OUTPUT:?GitHub output path is required}"

if [[ ! "${expected_sha}" =~ ^[0-9a-f]{40}$ ]]; then
  echo "Invalid expected main revision: ${expected_sha}" >&2
  exit 1
fi
if [[ ! "${current_main}" =~ ^[0-9a-f]{40}$ ]]; then
  echo "Unable to resolve the current main revision" >&2
  exit 1
fi

if [ "${current_main}" = "${expected_sha}" ]; then
  echo "current=true" >> "${github_output}"
  exit 0
fi

echo "current=false" >> "${github_output}"
echo "::notice title=Superseded main workflow::Skipping moving image tag promotion for ${expected_sha}; main is ${current_main}."
