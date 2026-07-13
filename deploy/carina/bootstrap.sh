#!/usr/bin/env bash
# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

set -euo pipefail

root=""
while (($#)); do
  case "$1" in
    --environment-root) root="${2:?missing environment root}"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 64 ;;
  esac
done
: "${root:?--environment-root is required}"

if ! command -v micromamba >/dev/null 2>&1; then
  module_name="${HEARTWOOD_MICROMAMBA_MODULE:-micromamba/2.3.3}"
  if [[ -r /etc/profile.d/modules.sh ]]; then
    # shellcheck source=/dev/null
    source /etc/profile.d/modules.sh
  fi
  if type module >/dev/null 2>&1 && module load "${module_name}"; then
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
printf 'Installing the locked Heartwood application environment.\n'
export UV_PROJECT_ENVIRONMENT="${root}/heartwood"
"${root}/bootstrap/bin/uv" sync --locked --no-dev --all-extras --python 3.12
printf 'Installing the locked vLLM environment.\n'
"${root}/bootstrap/bin/uv" venv "${root}/vllm" --python 3.12
"${root}/bootstrap/bin/uv" pip sync \
  --require-hashes --python "${root}/vllm/bin/python" images/gpu/vllm-requirements.txt

export PATH="${root}/bootstrap/bin:${PATH}"
export LD_LIBRARY_PATH="${root}/bootstrap/lib${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
export VLLM_USE_FLASHINFER_SAMPLER=0

printf 'Heartwood: %s\n' "$("${root}/heartwood/bin/heartwood" --version)"
"${root}/vllm/bin/python" -c '
import torchcodec
import vllm
from importlib.metadata import version
print(f"TorchCodec: {version('"'"'torchcodec'"'"')}")
print(f"vLLM: {version('"'"'vllm'"'"')}")
'
