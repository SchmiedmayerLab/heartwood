#!/usr/bin/env bash
# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

set -euo pipefail

assets="${1:?asset directory is required}"
assets="$(cd "${assets}" && pwd -P)"
workspace="$(mktemp -d)"
workspace="$(cd "${workspace}" && pwd -P)"
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

mkdir -p "${workspace}/outside-home" "${workspace}/outside-tmp"
touch "${workspace}/outside-home/sentinel" "${workspace}/outside-tmp/sentinel"

mkdir -p "${workspace}/bin"
cat >"${workspace}/bin/uv" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
root="${HEARTWOOD_TEST_INSTALL_ROOT:?HEARTWOOD_TEST_INSTALL_ROOT is required}"
for name in HOME TMPDIR TMP TEMP XDG_CACHE_HOME XDG_CONFIG_HOME XDG_DATA_HOME \
  XDG_STATE_HOME UV_CACHE_DIR MAMBA_ROOT_PREFIX PIP_CACHE_DIR HF_HOME TORCH_HOME \
  CUDA_CACHE_PATH NUMBA_CACHE_DIR TRITON_CACHE_DIR; do
  value="${!name:-}"
  case "${value}" in
    "${root}/.installer"/*) ;;
    *) echo "${name} escaped the installation root: ${value}" >&2; exit 1 ;;
  esac
done
bootstrap_python="$(dirname "$0")/python"
if [[ -x "${bootstrap_python}" ]]; then
  if [[ "${UV_PYTHON_DOWNLOADS:-}" != "never" ]]; then
    echo "bootstrap uv permits Python downloads" >&2
    exit 1
  fi
  if [[ "${UV_PYTHON_PREFERENCE:-}" != "only-system" ]]; then
    echo "bootstrap uv does not require the system Python" >&2
    exit 1
  fi
fi
case "${1:-}" in
sync)
  : "${UV_PROJECT_ENVIRONMENT:?UV_PROJECT_ENVIRONMENT is required}"
  if [[ -x "${bootstrap_python}" ]]; then
    case " $* " in
      *" --python ${bootstrap_python} "*) ;;
      *) echo "Heartwood environment does not use bootstrap Python" >&2; exit 1 ;;
    esac
  fi
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
  case " $* " in
    *" --python ${bootstrap_python} "*) ;;
    *) echo "vLLM environment does not use bootstrap Python" >&2; exit 1 ;;
  esac
  case " $* " in
    *" --allow-existing "*) ;;
    *) echo "vLLM environment is not resumable" >&2; exit 1 ;;
  esac
  mkdir -p "${runtime}/bin"
  cat >"${runtime}/bin/python" <<'COMMAND'
#!/usr/bin/env bash
if [[ "${1:-}" == */heartwood_vllm.py ]]; then
  test "${2:-}" = "__heartwood_verify_runtime__"
  grep --quiet 'GHSA-7rgv-gqhr-fxg3' "$1"
  grep --quiet 'GHSA-65pc-fj4g-8rjx' "$1"
  echo "Transformers synthetic integration and GPU security fixes verified"
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
root="${HEARTWOOD_TEST_INSTALL_ROOT:?HEARTWOOD_TEST_INSTALL_ROOT is required}"
for name in HOME TMPDIR TMP TEMP XDG_CACHE_HOME XDG_CONFIG_HOME XDG_DATA_HOME \
  XDG_STATE_HOME UV_CACHE_DIR MAMBA_ROOT_PREFIX PIP_CACHE_DIR HF_HOME TORCH_HOME \
  CUDA_CACHE_PATH NUMBA_CACHE_DIR TRITON_CACHE_DIR; do
  value="${!name:-}"
  case "${value}" in
    "${root}/.installer"/*) ;;
    *) echo "${name} escaped the installation root: ${value}" >&2; exit 1 ;;
  esac
done
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
cat >"${prefix}/bin/python" <<'COMMAND'
#!/usr/bin/env bash
echo "synthetic bootstrap Python"
COMMAND
chmod +x "${prefix}/bin/uv" "${prefix}/bin/python"
EOF
chmod +x "${workspace}/bin/micromamba"

expected_release="$(tar -xOf "${assets}/heartwood-native.tar.gz" heartwood/HEARTWOOD_VERSION)"
grep --fixed-strings --line-regexp --quiet \
  "installer_release=\"${expected_release}\"" "${assets}/heartwood-installer"
if "${assets}/heartwood-installer" --help | grep --quiet -- '--version'; then
  echo "published installer exposes a redundant release version option" >&2
  exit 1
fi
if "${assets}/heartwood-installer" --version "${expected_release}" >/dev/null 2>&1; then
  echo "published installer accepted a redundant release version option" >&2
  exit 1
fi

mismatched_installer="${workspace}/mismatched-installer"
sed 's/^installer_release=.*/installer_release="mismatched-release"/' \
  "${assets}/heartwood-installer" >"${mismatched_installer}"
chmod +x "${mismatched_installer}"
if "${mismatched_installer}" \
  --bundle "${assets}/heartwood-native.tar.gz" \
  --checksums "${assets}/SHA256SUMS" \
  --root "${workspace}/mismatched-installation" \
  --platform generic \
  --dry-run; then
  echo "installer accepted a bundle from another release" >&2
  exit 1
fi

mkdir -p "${workspace}/installation"
(
  cd "${workspace}/installation"
  HOME="${workspace}/outside-home" TMPDIR="${workspace}/outside-tmp" \
    HEARTWOOD_TEST_INSTALL_ROOT="${workspace}/installation" \
    PATH="${workspace}/bin:${PATH}" \
    "${assets}/heartwood-installer" \
    --bundle "${assets}/heartwood-native.tar.gz" \
    --checksums "${assets}/SHA256SUMS" \
    --minimum-free-gib 1 \
    --platform generic
)

if "${assets}/heartwood-installer" \
  --bundle "${assets}/heartwood-native.tar.gz" \
  --checksums "${assets}/SHA256SUMS" \
  --root "${workspace}/invalid-capacity" \
  --minimum-free-gib invalid \
  --platform generic \
  --dry-run; then
  echo "installer accepted an invalid storage requirement" >&2
  exit 1
fi

test -x "${workspace}/installation/bin/heartwood"
test -L "${workspace}/installation/current"
test ! -e "${workspace}/installation/.installer"
test "$(find "${workspace}/outside-home" -mindepth 1 | wc -l | tr -d ' ')" = "1"
test "$(find "${workspace}/outside-tmp" -mindepth 1 | wc -l | tr -d ' ')" = "1"
for directory in versions runtimes bin; do
  test -d "${workspace}/installation/${directory}"
  test "$(file_mode "${workspace}/installation/${directory}")" = "700"
done
for directory in state models cache logs; do
  test ! -e "${workspace}/installation/${directory}"
done
test "$("${workspace}/installation/bin/heartwood")" = "heartwood synthetic command"
if grep --quiet '^export HEARTWOOD_PLATFORM=' "${workspace}/installation/bin/heartwood"; then
  echo "generic command wrapper unexpectedly binds a managed platform" >&2
  exit 1
fi
if grep --extended-regexp 'HEARTWOOD_(HOME|WORKSPACE|MODEL_CACHE|INSTALL_ROOT|NATIVE_ROOT|NATIVE_VERSION|VERSION)|HF_HOME' \
  "${workspace}/installation/bin/heartwood"; then
  echo "installed command wrapper exports project or release state" >&2
  exit 1
fi

for _ in 1 2; do
  HOME="${workspace}/outside-home" TMPDIR="${workspace}/outside-tmp" \
    HEARTWOOD_TEST_INSTALL_ROOT="${workspace}/carina-installation" \
    PATH="${workspace}/bin:${PATH}" \
    "${assets}/heartwood-installer" \
    --bundle "${assets}/heartwood-native.tar.gz" \
    --checksums "${assets}/SHA256SUMS" \
    --root "${workspace}/carina-installation" \
    --minimum-free-gib 1 \
    --platform carina
done

carina_version="$(basename "$(readlink "${workspace}/carina-installation/current")")"
carina_runtime="${workspace}/carina-installation/runtimes/${carina_version}"
test -x "${carina_runtime}/bootstrap/bin/uv"
test ! -e "${workspace}/carina-installation/.installer"
test "$(find "${workspace}/outside-home" -mindepth 1 | wc -l | tr -d ' ')" = "1"
test "$(find "${workspace}/outside-tmp" -mindepth 1 | wc -l | tr -d ' ')" = "1"
test -x "${carina_runtime}/heartwood/bin/heartwood"
test -x "${carina_runtime}/vllm/bin/python"
test -x "${carina_runtime}/vllm/bin/vllm"
test -x "${carina_runtime}/vllm/bin/heartwood-vllm"
test -r "${carina_runtime}/vllm/bin/heartwood_vllm.py"
test -r "${carina_runtime}/vllm/bin/sitecustomize.py"
test -x "${carina_runtime}/vllm/bin/hf"
test "$(file_mode "${carina_runtime}/vllm/bin/heartwood-vllm")" = "555"
test "$(file_mode "${carina_runtime}/vllm/bin/heartwood_vllm.py")" = "444"
test "$(file_mode "${carina_runtime}/vllm/bin/sitecustomize.py")" = "444"
"${carina_runtime}/vllm/bin/heartwood-vllm" __heartwood_verify_runtime__ | \
  grep --quiet 'GPU security fixes verified'
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
grep --fixed-strings --line-regexp --quiet 'export HEARTWOOD_PLATFORM=carina' \
  "${workspace}/carina-installation/bin/heartwood"
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
