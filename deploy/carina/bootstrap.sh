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
  echo "micromamba is required; load the supported Carina module first" >&2
  exit 69
fi
if [[ ! -f pyproject.toml || ! -f uv.lock ]]; then
  echo "run this command from the Heartwood repository root" >&2
  exit 66
fi

mkdir -p "${root}"
micromamba create --yes --prefix "${root}/bootstrap" --file deploy/carina/environment.yml
export UV_PROJECT_ENVIRONMENT="${root}/heartwood"
"${root}/bootstrap/bin/uv" sync --locked --no-dev --all-extras --python 3.12
"${root}/bootstrap/bin/uv" venv "${root}/vllm" --python 3.12
"${root}/bootstrap/bin/uv" pip sync \
  --python "${root}/vllm/bin/python" images/gpu/vllm-requirements.txt

printf 'Heartwood: %s\n' "$("${root}/heartwood/bin/heartwood" --version)"
printf 'vLLM: %s\n' "$("${root}/vllm/bin/python" -c 'from importlib.metadata import version; print(version("vllm"))')"
