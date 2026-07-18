#!/usr/bin/env bash
# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

set -euo pipefail

output_dir="${1:-dist}"
version="${2:-$(git describe --tags --always --dirty)}"
if [[ ! "${version}" =~ ^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$ ]]; then
  echo "native package version is unsafe: ${version}" >&2
  exit 64
fi
archive="${output_dir}/heartwood-native.tar.gz"
workspace="$(mktemp -d)"
cleanup() {
  rm -rf "${workspace}"
}
trap cleanup EXIT

mkdir -p "${output_dir}" "${workspace}/heartwood"
git archive --format=tar HEAD | tar -xf - -C "${workspace}/heartwood"
printf '%s\n' "${version}" >"${workspace}/heartwood/HEARTWOOD_VERSION"
COPYFILE_DISABLE=1 tar --no-xattrs -czf "${archive}" -C "${workspace}" heartwood
(
  cd "${output_dir}"
  sha256sum "$(basename "${archive}")" >SHA256SUMS
)
sed "s/__HEARTWOOD_RELEASE_VERSION__/${version}/g" \
  deploy/install.sh >"${output_dir}/heartwood-installer"
if grep --quiet '__HEARTWOOD_RELEASE_VERSION__' "${output_dir}/heartwood-installer"; then
  echo "native installer release placeholder was not replaced" >&2
  exit 1
fi
chmod +x "${output_dir}/heartwood-installer"
