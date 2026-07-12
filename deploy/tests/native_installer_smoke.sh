#!/usr/bin/env bash
# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

set -euo pipefail

assets="${1:?asset directory is required}"
workspace="$(mktemp -d)"
cleanup() {
  rm -rf "${workspace}"
}
trap cleanup EXIT

mkdir -p "${workspace}/bin"
cat >"${workspace}/bin/uv" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
: "${UV_PROJECT_ENVIRONMENT:?UV_PROJECT_ENVIRONMENT is required}"
mkdir -p "${UV_PROJECT_ENVIRONMENT}/bin"
cat >"${UV_PROJECT_ENVIRONMENT}/bin/heartwood" <<'COMMAND'
#!/usr/bin/env bash
printf '%s|%s|%s|%s\n' "${HEARTWOOD_INSTALL_ROOT}" "${HEARTWOOD_NATIVE_VERSION}" "${HEARTWOOD_VERSION}" "${HEARTWOOD_HOME}"
COMMAND
chmod +x "${UV_PROJECT_ENVIRONMENT}/bin/heartwood"
EOF
chmod +x "${workspace}/bin/uv"

PATH="${workspace}/bin:${PATH}" "${assets}/heartwood-installer" \
  --bundle "${assets}/heartwood-native.tar.gz" \
  --checksums "${assets}/SHA256SUMS" \
  --root "${workspace}/installation" \
  --platform generic

test -x "${workspace}/installation/bin/heartwood"
test -L "${workspace}/installation/current"
output="$("${workspace}/installation/bin/heartwood")"
case "${output}" in
  "${workspace}/installation/versions/"*"|"*"|"*"|${workspace}/installation/state") ;;
  *) echo "installed command did not receive native installation metadata" >&2; exit 1 ;;
esac

printf '%064d  heartwood-native.tar.gz\n' 0 >"${workspace}/invalid-SHA256SUMS"
if "${assets}/heartwood-installer" \
  --bundle "${assets}/heartwood-native.tar.gz" \
  --checksums "${workspace}/invalid-SHA256SUMS" \
  --root "${workspace}/invalid" \
  --platform generic; then
  echo "installer accepted a corrupted checksum" >&2
  exit 1
fi
test ! -e "${workspace}/invalid"

digest="$(cut -d ' ' -f 1 "${assets}/SHA256SUMS")"
printf '%s  ../heartwood-native.tar.gz\n' "${digest}" >"${workspace}/unsafe-SHA256SUMS"
if "${assets}/heartwood-installer" \
  --bundle "${assets}/heartwood-native.tar.gz" \
  --checksums "${workspace}/unsafe-SHA256SUMS" \
  --root "${workspace}/unsafe" \
  --platform generic; then
  echo "installer accepted an unsafe checksum manifest" >&2
  exit 1
fi
test ! -e "${workspace}/unsafe"
