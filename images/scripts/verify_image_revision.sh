#!/usr/bin/env bash
# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

set -euo pipefail

if [ "$#" -ne 3 ]; then
  echo "usage: $0 IMAGE_REFERENCE PLATFORM EXPECTED_REVISION" >&2
  exit 64
fi

candidate="$1"
platform="$2"
expected_revision="$3"

observed_revision="$(
  docker run --rm --platform "${platform}" \
    --entrypoint sh "${candidate}" \
    -c 'printf "%s" "${HEARTWOOD_IMAGE_REVISION:-}"'
)"
observed_label="$(
  docker image inspect \
    --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' \
    "${candidate}"
)"
if [ "${observed_revision}" != "${expected_revision}" ] \
  || [ "${observed_label}" != "${expected_revision}" ]; then
  echo "candidate revision does not match the requested commit" >&2
  echo "expected: ${expected_revision}" >&2
  echo "runtime: ${observed_revision:-<missing>}; label: ${observed_label:-<missing>}" >&2
  exit 1
fi
