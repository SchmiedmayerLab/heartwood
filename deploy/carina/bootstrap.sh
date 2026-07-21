#!/usr/bin/env bash
# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

set -euo pipefail

root=""
installer_state=""
while (($#)); do
  case "$1" in
    --environment-root) root="${2:?missing environment root}"; shift 2 ;;
    --installer-state) installer_state="${2:?missing installer state}"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 64 ;;
  esac
done
: "${root:?--environment-root is required}"
: "${installer_state:?--installer-state is required}"

umask 077
root="$(mkdir -p "${root}" && cd "${root}" && pwd -P)"
installer_state="$(mkdir -p "${installer_state}" && cd "${installer_state}" && pwd -P)"
installer_directories=(
  "${installer_state}"
  "${installer_state}/home"
  "${installer_state}/tmp"
  "${installer_state}/cache/uv"
  "${installer_state}/cache/xdg"
  "${installer_state}/cache/mamba"
  "${installer_state}/cache/pip"
  "${installer_state}/cache/huggingface"
  "${installer_state}/cache/torch"
  "${installer_state}/cache/cuda"
  "${installer_state}/cache/numba"
  "${installer_state}/cache/triton"
  "${installer_state}/config"
  "${installer_state}/data"
  "${installer_state}/state"
)
mkdir -p "${installer_directories[@]}"
chmod 700 "${installer_directories[@]}"
export HOME="${installer_state}/home"
export TMPDIR="${installer_state}/tmp"
export TMP="${TMPDIR}"
export TEMP="${TMPDIR}"
export XDG_CACHE_HOME="${installer_state}/cache/xdg"
export XDG_CONFIG_HOME="${installer_state}/config"
export XDG_DATA_HOME="${installer_state}/data"
export XDG_STATE_HOME="${installer_state}/state"
export UV_CACHE_DIR="${installer_state}/cache/uv"
export MAMBA_ROOT_PREFIX="${installer_state}/cache/mamba"
export PIP_CACHE_DIR="${installer_state}/cache/pip"
export HF_HOME="${installer_state}/cache/huggingface"
export TORCH_HOME="${installer_state}/cache/torch"
export CUDA_CACHE_PATH="${installer_state}/cache/cuda"
export NUMBA_CACHE_DIR="${installer_state}/cache/numba"
export TRITON_CACHE_DIR="${installer_state}/cache/triton"

initialize_module_command() {
  if type module >/dev/null 2>&1; then
    return 0
  fi
  local candidate
  local -a candidates=()
  if [[ -n "${HEARTWOOD_MODULE_INIT:-}" ]]; then
    candidates+=("${HEARTWOOD_MODULE_INIT}")
  fi
  candidates+=(
    /etc/profile.d/modules.sh
    /etc/profile.d/lmod.sh
    /usr/share/lmod/lmod/init/profile
    /usr/share/lmod/lmod/init/bash
    /usr/share/Modules/init/bash
  )
  for candidate in "${candidates[@]}"; do
    if [[ -r "${candidate}" ]]; then
      # shellcheck source=/dev/null
      source "${candidate}"
      if type module >/dev/null 2>&1; then
        return 0
      fi
    fi
  done
  return 1
}

if ! command -v micromamba >/dev/null 2>&1; then
  module_name="${HEARTWOOD_MICROMAMBA_MODULE:-micromamba/2.3.3}"
  if initialize_module_command && module load "${module_name}"; then
    printf 'Loaded %s for the native installation.\n' "${module_name}"
  else
    echo "micromamba is unavailable; load ${module_name} and retry" >&2
    exit 69
  fi
fi
if [[ ! -f pyproject.toml || ! -f uv.lock ]]; then
  echo "run this command from the Heartwood repository root" >&2
  exit 66
fi

mkdir -p "${root}"
if [[ -x "${root}/bootstrap/bin/uv" && -d "${root}/bootstrap/conda-meta" ]]; then
  printf 'Updating the bootstrap environment; dependency solving can take several minutes.\n'
  micromamba install --yes --prefix "${root}/bootstrap" --file deploy/carina/environment.yml
else
  rm -rf "${root}/bootstrap"
  printf 'Creating the bootstrap environment; dependency solving can take several minutes.\n'
  micromamba create --yes --prefix "${root}/bootstrap" --file deploy/carina/environment.yml
fi
bootstrap_python="${root}/bootstrap/bin/python"
if [[ ! -x "${bootstrap_python}" ]]; then
  echo "bootstrap Python is unavailable: ${bootstrap_python}" >&2
  exit 69
fi
export UV_PYTHON_DOWNLOADS=never
export UV_PYTHON_PREFERENCE=only-system
printf 'Installing the locked Heartwood application environment.\n'
export UV_PROJECT_ENVIRONMENT="${root}/heartwood"
"${root}/bootstrap/bin/uv" sync \
  --locked --no-dev --all-extras --python "${bootstrap_python}"
printf 'Installing the locked vLLM environment.\n'
images/gpu/install_runtime.sh \
  --target "${root}/vllm" \
  --python "${bootstrap_python}" \
  --uv "${root}/bootstrap/bin/uv"

export PATH="${root}/bootstrap/bin:${PATH}"
export LD_LIBRARY_PATH="${root}/bootstrap/lib${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
export VLLM_USE_FLASHINFER_SAMPLER=0

printf 'Heartwood: %s\n' "$("${root}/heartwood/bin/heartwood" --version)"
HEARTWOOD_VLLM_ROOT="${root}/vllm" \
HEARTWOOD_VLLM_PYTHON="${root}/vllm/bin/python" \
HEARTWOOD_VLLM_EXECUTABLE="${root}/vllm/bin/heartwood-vllm" \
  images/gpu/verify_runtime.sh "${root}/vllm"
