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
  if [[ "${HEARTWOOD_TEST_FAIL_SYNC:-false}" == "true" ]]; then
    echo "synthetic uv sync failure" >&2
    exit 70
  fi
  if [[ -x "${bootstrap_python}" ]]; then
    case " $* " in
      *" --python ${bootstrap_python} "*) ;;
      *) echo "Heartwood environment does not use bootstrap Python" >&2; exit 1 ;;
    esac
  else
    case "${UV_PYTHON_INSTALL_DIR:-}" in
      "${root}"/installations/*/runtime/python) ;;
      *) echo "generic managed Python is not stored in the installation generation" >&2; exit 1 ;;
    esac
    mkdir -p "${UV_PYTHON_INSTALL_DIR}"
    touch "${UV_PYTHON_INSTALL_DIR}/synthetic-python"
  fi
  mkdir -p "${UV_PROJECT_ENVIRONMENT}/bin"
  cat >"${UV_PROJECT_ENVIRONMENT}/bin/heartwood" <<'COMMAND'
#!/usr/bin/env bash
if [[ "${1:-}" == "--version" ]]; then
  if [[ "${HEARTWOOD_TEST_FAIL_PUBLISHED_COMMAND:-false}" == "true" ]]; then
    counter="${HEARTWOOD_TEST_PUBLISH_COUNTER:?HEARTWOOD_TEST_PUBLISH_COUNTER is required}"
    count=0
    if [[ -f "${counter}" ]]; then
      count="$(cat "${counter}")"
    fi
    count=$((count + 1))
    printf '%s\n' "${count}" >"${counter}"
    if [[ "${count}" -ge 4 ]]; then
      echo "synthetic published command failure" >&2
      exit 70
    fi
  fi
  echo "heartwood synthetic"
  exit 0
fi
echo "heartwood synthetic command"
COMMAND
  cat >"${UV_PROJECT_ENVIRONMENT}/bin/jupyter-lab" <<'COMMAND'
#!/usr/bin/env bash
echo "4.0.0"
COMMAND
  chmod +x \
    "${UV_PROJECT_ENVIRONMENT}/bin/heartwood" \
    "${UV_PROJECT_ENVIRONMENT}/bin/jupyter-lab"
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
if [[ "${1:-}" == */verify_vllm.py ]]; then
  grep --quiet 'vllm_version' "$1"
  grep --quiet 'cuda_13_qualified' "$1"
  echo "Heartwood GPU runtime verified: synthetic CUDA 12.9 stack"
  exit 0
fi
echo "0.25.1+cu129 2.11.0+cu129 12.9"
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

for command in git tmux; do
  cat >"${workspace}/bin/${command}" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
  chmod +x "${workspace}/bin/${command}"
done

cat >"${workspace}/bin/micromamba" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
root="${HEARTWOOD_TEST_INSTALL_ROOT:?HEARTWOOD_TEST_INSTALL_ROOT is required}"
if [[ "${MAMBA_EXTRACT_THREADS:-}" != "8" ]]; then
  echo "micromamba extraction workers are not bounded: ${MAMBA_EXTRACT_THREADS:-unset}" >&2
  exit 1
fi
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

cat >"${workspace}/bin/srun" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
expected=(
  "--partition=dev"
  "--cpus-per-task=8"
  "--mem=32G"
  "--time=01:00:00"
)
for argument in "${expected[@]}"; do
  if [[ " $* " != *" ${argument} "* ]]; then
    echo "Carina installer omitted ${argument}" >&2
    exit 1
  fi
done
while (($#)); do
  case "$1" in
    --*) shift ;;
    *) break ;;
  esac
done
: "${1:?installer command is required}"
SLURM_JOB_ID=synthetic SLURM_CPUS_PER_TASK=8 exec "$@"
EOF
chmod +x "${workspace}/bin/srun"

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

malicious_runtime="${workspace}/self-authenticated-llama"
mkdir -p "${malicious_runtime}"
cat >"${malicious_runtime}/llama-server" <<'EOF'
#!/usr/bin/env bash
if [[ "${1:-}" == "--version" ]]; then
  echo "llama.cpp synthetic"
  exit 0
fi
echo "synthetic llama-server"
EOF
chmod +x "${malicious_runtime}/llama-server"
cat >"${malicious_runtime}/.heartwood-runtime" <<'EOF'
version=b9937
asset=attacker-controlled.tar.gz
archive_sha256=attacker-controlled
EOF
(
  cd "${malicious_runtime}"
  sha256sum llama-server .heartwood-runtime >.heartwood-SHA256SUMS
)
if deploy/install-llama-cpp.sh "${malicious_runtime}"; then
  echo "llama.cpp installer trusted a runtime's self-authenticated manifest" >&2
  exit 1
fi

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
test ! -e "${workspace}/invalid-capacity"

dry_run_root="${workspace}/dry-run-installation"
"${assets}/heartwood-installer" \
  --bundle "${assets}/heartwood-native.tar.gz" \
  --checksums "${assets}/SHA256SUMS" \
  --root "${dry_run_root}" \
  --platform generic \
  --dry-run
test ! -e "${dry_run_root}"

mkdir -m 755 "${dry_run_root}"
touch "${dry_run_root}/sentinel"
"${assets}/heartwood-installer" \
  --bundle "${assets}/heartwood-native.tar.gz" \
  --checksums "${assets}/SHA256SUMS" \
  --root "${dry_run_root}" \
  --platform generic \
  --dry-run
test "$(file_mode "${dry_run_root}")" = "755"
test "$(find "${dry_run_root}" -mindepth 1 | wc -l | tr -d ' ')" = "1"

redirect_target="${workspace}/redirect-target"
mkdir -m 700 "${redirect_target}"
touch "${redirect_target}/sentinel"
for owned_name in .installer installations; do
  redirected_root="${workspace}/redirected-${owned_name#.}"
  mkdir -m 700 "${redirected_root}"
  ln -s "${redirect_target}" "${redirected_root}/${owned_name}"
  if "${assets}/heartwood-installer" \
    --bundle "${assets}/heartwood-native.tar.gz" \
    --checksums "${assets}/SHA256SUMS" \
    --root "${redirected_root}" \
    --platform generic; then
    echo "installer followed redirected ${owned_name} state" >&2
    exit 1
  fi
  test -L "${redirected_root}/${owned_name}"
  test ! -e "${redirected_root}/.installer.lock"
done
test "$(find "${redirect_target}" -mindepth 1 | wc -l | tr -d ' ')" = "1"

locked_root="${workspace}/locked-installation"
mkdir -m 700 "${locked_root}" "${locked_root}/.installer.lock"
touch "${locked_root}/.installer.lock/existing-owner"
if "${assets}/heartwood-installer" \
  --bundle "${assets}/heartwood-native.tar.gz" \
  --checksums "${assets}/SHA256SUMS" \
  --root "${locked_root}" \
  --platform generic; then
  echo "installer ignored an active installation lock" >&2
  exit 1
fi
test -f "${locked_root}/.installer.lock/existing-owner"
test ! -e "${locked_root}/.installer"

carina_installation="${workspace}/carina-installation"
for _ in 1 2; do
  HOME="${workspace}/outside-home" TMPDIR="${workspace}/outside-tmp" \
    HEARTWOOD_TEST_INSTALL_ROOT="${carina_installation}" \
    PATH="${workspace}/bin:${PATH}" \
    "${assets}/heartwood-installer" \
    --bundle "${assets}/heartwood-native.tar.gz" \
    --checksums "${assets}/SHA256SUMS" \
    --root "${carina_installation}" \
    --minimum-free-gib 1 \
    --platform carina
done

carina_generation="$(cd "${carina_installation}/current" && pwd -P)"
carina_runtime="${carina_generation}/runtime"
carina_source="${carina_generation}/source"
test -x "${carina_runtime}/bootstrap/bin/uv"
test -r "${carina_source}/HEARTWOOD_VERSION"
test ! -e "${carina_installation}/.installer"
test ! -e "${carina_installation}/.installer.lock"
test "$(find "${workspace}/outside-home" -mindepth 1 | wc -l | tr -d ' ')" = "1"
test "$(find "${workspace}/outside-tmp" -mindepth 1 | wc -l | tr -d ' ')" = "1"
test -x "${carina_runtime}/heartwood/bin/heartwood"
test -x "${carina_runtime}/vllm/bin/python"
test -x "${carina_runtime}/vllm/bin/vllm"
test -x "${carina_runtime}/vllm/bin/heartwood-vllm"
test -r "${carina_runtime}/vllm/bin/verify_vllm.py"
test -r "${carina_runtime}/vllm/bin/compatibility.toml"
test ! -e "${carina_runtime}/vllm/bin/heartwood_vllm.py"
test ! -e "${carina_runtime}/vllm/bin/sitecustomize.py"
test -x "${carina_runtime}/vllm/bin/hf"
test "$(file_mode "${carina_runtime}/vllm/bin/heartwood-vllm")" = "555"
test "$(file_mode "${carina_runtime}/vllm/bin/verify_vllm.py")" = "444"
test "$(file_mode "${carina_runtime}/vllm/bin/compatibility.toml")" = "444"
"${carina_runtime}/vllm/bin/heartwood-vllm" __heartwood_verify_runtime__ | \
  grep --quiet 'synthetic CUDA 12.9 stack'
test -L "${carina_installation}/bin/hf"
carina_current_target="$(readlink "${carina_installation}/current")"
test "$(readlink "${carina_installation}/bin/hf")" = \
  "../${carina_current_target}/bin/hf"
test -L "${carina_generation}/bin/hf"
test "$(readlink "${carina_generation}/bin/hf")" = "${carina_runtime}/vllm/bin/hf"
test -L "${carina_installation}/bin/heartwood"
test "$(readlink "${carina_installation}/bin/heartwood")" = \
  "../${carina_current_target}/bin/heartwood"
test "$(find "${carina_installation}/installations" -mindepth 1 -maxdepth 1 -type d | wc -l | tr -d ' ')" = "2"
for directory in installations bin; do
  test -d "${carina_installation}/${directory}"
  test "$(file_mode "${carina_installation}/${directory}")" = "700"
done
for directory in state models cache logs; do
  test ! -e "${carina_installation}/${directory}"
done
test "$("${carina_installation}/bin/heartwood")" = "heartwood synthetic command"
grep --fixed-strings --line-regexp --quiet 'export HEARTWOOD_PLATFORM=carina' \
  "${carina_generation}/bin/heartwood"
grep --fixed-strings --quiet \
  "runtime=${carina_runtime}/bootstrap" \
  "${carina_generation}/bin/heartwood"
wrapper_path_export="export PATH=\"\${runtime}/bin:\${runtime}:\${PATH}\""
grep --fixed-strings --quiet "${wrapper_path_export}" \
  "${carina_generation}/bin/heartwood"
if grep --extended-regexp 'HEARTWOOD_(HOME|WORKSPACE|MODEL_CACHE|INSTALL_ROOT|NATIVE_ROOT|NATIVE_VERSION|VERSION)|HF_HOME' \
  "${carina_generation}/bin/heartwood"; then
  echo "Carina command wrapper exports project or release state" >&2
  exit 1
fi

published_current="$(readlink "${carina_installation}/current")"
published_command="$(readlink "${carina_installation}/bin/heartwood")"
published_generation_count="$(find "${carina_installation}/installations" -mindepth 1 -maxdepth 1 -type d | wc -l | tr -d ' ')"
if HOME="${workspace}/outside-home" TMPDIR="${workspace}/outside-tmp" \
  HEARTWOOD_TEST_INSTALL_ROOT="${carina_installation}" \
  HEARTWOOD_TEST_FAIL_SYNC=true \
  PATH="${workspace}/bin:${PATH}" \
  "${assets}/heartwood-installer" \
  --bundle "${assets}/heartwood-native.tar.gz" \
  --checksums "${assets}/SHA256SUMS" \
  --root "${carina_installation}" \
  --minimum-free-gib 1 \
  --platform carina; then
  echo "installer accepted a failed application build" >&2
  exit 1
fi
test "$(readlink "${carina_installation}/current")" = "${published_current}"
test "$(readlink "${carina_installation}/bin/heartwood")" = "${published_command}"
test "$(find "${carina_installation}/installations" -mindepth 1 -maxdepth 1 -type d | wc -l | tr -d ' ')" = "${published_generation_count}"
test ! -e "${carina_installation}/.installer"
test ! -e "${carina_installation}/.installer.lock"
test "$("${carina_installation}/bin/heartwood")" = "heartwood synthetic command"

publish_counter="${workspace}/publish-counter"
publish_failure_log="${workspace}/publish-failure.log"
published_hf="$(readlink "${carina_installation}/bin/hf")"
if HOME="${workspace}/outside-home" TMPDIR="${workspace}/outside-tmp" \
  HEARTWOOD_TEST_INSTALL_ROOT="${carina_installation}" \
  HEARTWOOD_TEST_FAIL_PUBLISHED_COMMAND=true \
  HEARTWOOD_TEST_PUBLISH_COUNTER="${publish_counter}" \
  PATH="${workspace}/bin:${PATH}" \
  "${assets}/heartwood-installer" \
  --bundle "${assets}/heartwood-native.tar.gz" \
  --checksums "${assets}/SHA256SUMS" \
  --root "${carina_installation}" \
  --minimum-free-gib 1 \
  --platform carina >"${publish_failure_log}" 2>&1; then
  echo "installer accepted a failed published command" >&2
  exit 1
fi
test "$(cat "${publish_counter}")" = "4"
grep --quiet 'published Heartwood command cannot start' "${publish_failure_log}"
test "$(readlink "${carina_installation}/current")" = "${published_current}"
test "$(readlink "${carina_installation}/bin/heartwood")" = "${published_command}"
test "$(readlink "${carina_installation}/bin/hf")" = "${published_hf}"
test "$(find "${carina_installation}/installations" -mindepth 1 -maxdepth 1 -type d | wc -l | tr -d ' ')" = "${published_generation_count}"
test ! -e "${carina_installation}/.installer"
test ! -e "${carina_installation}/.installer.lock"
test "$("${carina_installation}/bin/heartwood")" = "heartwood synthetic command"

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
