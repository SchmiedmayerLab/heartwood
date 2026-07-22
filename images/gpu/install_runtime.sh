#!/usr/bin/env bash
# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

set -euo pipefail

target=""
python="3.12"
uv="uv"
while (($#)); do
  case "$1" in
    --target) target="${2:?missing runtime target}"; shift 2 ;;
    --python) python="${2:?missing Python interpreter}"; shift 2 ;;
    --uv) uv="${2:?missing uv executable}"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 64 ;;
  esac
done
: "${target:?--target is required}"

runtime_sources="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
"${uv}" venv "${target}" --python "${python}" --allow-existing
"${uv}" pip sync \
  --require-hashes \
  --python "${target}/bin/python" \
  "${runtime_sources}/vllm-requirements.txt"
install -m 0444 "${runtime_sources}/verify_vllm.py" "${target}/bin/verify_vllm.py"
install -m 0444 "${runtime_sources}/compatibility.toml" "${target}/bin/compatibility.toml"
install -m 0555 "${runtime_sources}/heartwood-vllm" "${target}/bin/heartwood-vllm"
