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
skill_revision="$(git rev-parse HEAD:vendor/heartwood-skills)"
if [[ ! -f vendor/heartwood-skills/skills/verified/aggregate-export/SKILL.md ]]; then
  echo "heartwood-skills submodule is not initialized" >&2
  exit 69
fi
if [[ "$(git -C vendor/heartwood-skills rev-parse HEAD)" != "${skill_revision}" ]]; then
  echo "heartwood-skills checkout does not match the pinned repository revision" >&2
  exit 65
fi
mkdir -p "${workspace}/heartwood/vendor/heartwood-skills"
git -C vendor/heartwood-skills archive --format=tar "${skill_revision}" \
  | tar -xf - -C "${workspace}/heartwood/vendor/heartwood-skills"
if ! command -v npm >/dev/null 2>&1; then
  echo "npm is required to build the native browser assets" >&2
  exit 69
fi
(
  cd "${workspace}/heartwood/packages/webui"
  npm ci --no-audit --fund=false
  npm run build
)
rm -rf "${workspace}/heartwood/packages/webui/node_modules"
if [[ ! -f "${workspace}/heartwood/packages/webui/dist/index.html" ]]; then
  echo "native browser assets were not produced" >&2
  exit 70
fi
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
