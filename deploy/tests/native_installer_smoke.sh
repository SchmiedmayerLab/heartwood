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

file_mode() {
  if stat -c '%a' "$1" >/dev/null 2>&1; then
    stat -c '%a' "$1"
  else
    stat -f '%Lp' "$1"
  fi
}

mkdir -p "${workspace}/bin"
cat >"${workspace}/bin/uv" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
case "${1:-}" in
sync)
  : "${UV_PROJECT_ENVIRONMENT:?UV_PROJECT_ENVIRONMENT is required}"
  mkdir -p "${UV_PROJECT_ENVIRONMENT}/bin"
  cat >"${UV_PROJECT_ENVIRONMENT}/bin/heartwood" <<'COMMAND'
#!/usr/bin/env bash
if [[ "${1:-}" == "--version" ]]; then
  echo "heartwood synthetic"
  exit 0
fi
echo "heartwood synthetic command"
COMMAND
  chmod +x "${UV_PROJECT_ENVIRONMENT}/bin/heartwood"
  ;;
venv)
  runtime="${2:?runtime path is required}"
  mkdir -p "${runtime}/bin"
  cat >"${runtime}/bin/python" <<'COMMAND'
#!/usr/bin/env bash
if [[ "${1:-}" == */heartwood_vllm.py ]]; then
  test "${2:-}" = "__heartwood_verify_runtime__"
  grep --quiet 'GHSA-8fr4-5q9j-m8gm backport verified' "$1"
  echo "Transformers synthetic integration and vLLM GHSA-8fr4-5q9j-m8gm backport verified"
  exit 0
fi
echo "vLLM: synthetic"
echo "PyTorch: synthetic (CUDA 11.8)"
COMMAND
  cat >"${runtime}/bin/vllm" <<'COMMAND'
#!/usr/bin/env bash
echo "synthetic vLLM"
COMMAND
  cat >"${runtime}/bin/hf" <<'COMMAND'
#!/usr/bin/env bash
echo "synthetic Hugging Face CLI"
COMMAND
  chmod +x "${runtime}/bin/python" "${runtime}/bin/vllm" "${runtime}/bin/hf"
  ;;
pip) ;;
*) echo "unexpected uv command: ${1:-}" >&2; exit 64 ;;
esac
EOF
chmod +x "${workspace}/bin/uv"

cat >"${workspace}/bin/micromamba" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
prefix=""
while (($#)); do
  case "$1" in
    --prefix) prefix="${2:?missing prefix}"; shift 2 ;;
    *) shift ;;
  esac
done
: "${prefix:?--prefix is required}"
mkdir -p "${prefix}/bin" "${prefix}/lib" "${prefix}/conda-meta"
cp "$(dirname "$0")/uv" "${prefix}/bin/uv"
chmod +x "${prefix}/bin/uv"
EOF
chmod +x "${workspace}/bin/micromamba"

HEARTWOOD_INSTALL_MINIMUM_FREE_GIB=1 PATH="${workspace}/bin:${PATH}" \
  "${assets}/heartwood-installer" \
  --bundle "${assets}/heartwood-native.tar.gz" \
  --checksums "${assets}/SHA256SUMS" \
  --root "${workspace}/installation" \
  --platform generic

if HEARTWOOD_INSTALL_MINIMUM_FREE_GIB=invalid \
  "${assets}/heartwood-installer" \
  --bundle "${assets}/heartwood-native.tar.gz" \
  --checksums "${assets}/SHA256SUMS" \
  --root "${workspace}/invalid-capacity" \
  --platform generic \
  --dry-run; then
  echo "installer accepted an invalid storage requirement" >&2
  exit 1
fi

test -x "${workspace}/installation/bin/heartwood"
test -L "${workspace}/installation/current"
for directory in versions runtimes bin; do
  test -d "${workspace}/installation/${directory}"
  test "$(file_mode "${workspace}/installation/${directory}")" = "700"
done
for directory in state models cache logs; do
  test ! -e "${workspace}/installation/${directory}"
done
test "$("${workspace}/installation/bin/heartwood")" = "heartwood synthetic command"
if grep --extended-regexp 'HEARTWOOD_(HOME|WORKSPACE|MODEL_CACHE|INSTALL_ROOT|NATIVE_ROOT|NATIVE_VERSION|VERSION)|HF_HOME' \
  "${workspace}/installation/bin/heartwood"; then
  echo "installed command wrapper exports project or release state" >&2
  exit 1
fi

for _ in 1 2; do
  HEARTWOOD_INSTALL_MINIMUM_FREE_GIB=1 PATH="${workspace}/bin:${PATH}" \
    "${assets}/heartwood-installer" \
    --bundle "${assets}/heartwood-native.tar.gz" \
    --checksums "${assets}/SHA256SUMS" \
    --root "${workspace}/carina-installation" \
    --platform carina
done

carina_version="$(basename "$(readlink "${workspace}/carina-installation/current")")"
carina_runtime="${workspace}/carina-installation/runtimes/${carina_version}"
test -x "${carina_runtime}/bootstrap/bin/uv"
test -x "${carina_runtime}/heartwood/bin/heartwood"
test -x "${carina_runtime}/vllm/bin/python"
test -x "${carina_runtime}/vllm/bin/vllm"
test -x "${carina_runtime}/vllm/bin/heartwood-vllm"
test -r "${carina_runtime}/vllm/bin/heartwood_vllm.py"
test -x "${carina_runtime}/vllm/bin/hf"
test "$(file_mode "${carina_runtime}/vllm/bin/heartwood-vllm")" = "555"
test "$(file_mode "${carina_runtime}/vllm/bin/heartwood_vllm.py")" = "444"
"${carina_runtime}/vllm/bin/heartwood-vllm" __heartwood_verify_runtime__ | \
  grep --quiet 'GHSA-8fr4-5q9j-m8gm backport verified'
test -L "${workspace}/carina-installation/bin/hf"
test "$(readlink "${workspace}/carina-installation/bin/hf")" = \
  "${carina_runtime}/vllm/bin/hf"
for directory in versions runtimes bin; do
  test -d "${workspace}/carina-installation/${directory}"
  test "$(file_mode "${workspace}/carina-installation/${directory}")" = "700"
done
for directory in state models cache logs; do
  test ! -e "${workspace}/carina-installation/${directory}"
done
test "$("${workspace}/carina-installation/bin/heartwood")" = "heartwood synthetic command"
if grep --extended-regexp 'HEARTWOOD_(HOME|WORKSPACE|MODEL_CACHE|INSTALL_ROOT|NATIVE_ROOT|NATIVE_VERSION|VERSION)|HF_HOME' \
  "${workspace}/carina-installation/bin/heartwood"; then
  echo "Carina command wrapper exports project or release state" >&2
  exit 1
fi

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
