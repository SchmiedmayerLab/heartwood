#!/usr/bin/env bash
# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

set -euo pipefail

output_dir="${1:-dist}"
version="${2:-$(git describe --tags --always --dirty)}"
archive="${output_dir}/heartwood-native.tar.gz"
workspace="$(mktemp -d)"
cleanup() {
  rm -rf "${workspace}"
}
trap cleanup EXIT

mkdir -p "${output_dir}" "${workspace}/heartwood"
git archive --format=tar HEAD | tar -xf - -C "${workspace}/heartwood"
printf '%s\n' "${version}" >"${workspace}/heartwood/HEARTWOOD_VERSION"
tar -czf "${archive}" -C "${workspace}" heartwood
(
  cd "${output_dir}"
  sha256sum "$(basename "${archive}")" >SHA256SUMS
)
cp deploy/install.sh "${output_dir}/heartwood-installer"
chmod +x "${output_dir}/heartwood-installer"
