#!/usr/bin/env bash
# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

# Compile or install the qualification environment; never modify an existing environment.
set -euo pipefail
if [[ $# -lt 2 || $# -gt 3 || ( "$1" != compile && "$1" != install ) ]]; then
  echo "Usage: $0 compile WHEEL_DIRECTORY | install WHEEL_DIRECTORY NEW_ENVIRONMENT" >&2
  exit 2
fi
operation="$1"
wheel_dir="$(cd "$2" && pwd -P)"
uv_bin="$(command -v "${UV_BIN:-uv}")"
uv_bin="$(python3 -c 'import pathlib, sys; print(pathlib.Path(sys.argv[1]).resolve())' "$uv_bin")"
if [[ "$("$uv_bin" --version | awk '{print $2}')" != 0.13.7 ]]; then
  echo "Worker qualification requires uv 0.13.7" >&2
  exit 1
fi
if [[ "$operation" == install ]]; then
  if [[ $# != 3 || -e "$3" || -L "$3" ]]; then
    echo "Installation requires a new environment path" >&2
    exit 2
  fi
  target="$(python3 -c 'import pathlib, sys; print(pathlib.Path(sys.argv[1]).resolve())' "$3")"
elif [[ $# != 2 ]]; then
  echo "Compilation accepts only the wheel directory" >&2
  exit 2
fi
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."
# Match the normal release cutoff, with explicit security/SDK compatibility exceptions.
release_cutoff=(
  --exclude-newer 2026-11-06T00:00:00Z
  --exclude-newer-package filelock=2026-11-13T00:00:00Z
  --exclude-newer-package litellm=2026-11-13T00:00:00Z
)
if [[ "$operation" == compile ]]; then
  "$uv_bin" pip compile --no-config --python-version 3.12 --universal \
    "${release_cutoff[@]}" --find-links "$wheel_dir" --generate-hashes \
    --custom-compile-command 'bash images/worker/candidate/runtime.sh compile WHEEL_DIRECTORY' \
    --output-file images/worker/candidate/runtime-requirements.txt \
    images/worker/candidate/runtime.in --quiet
else
  "$uv_bin" venv --no-config --python 3.12.15 "$target"
  "$uv_bin" pip sync --no-config --python "$target/bin/python" \
    "${release_cutoff[@]}" --find-links "$wheel_dir" --require-hashes \
    --build-constraints images/worker/candidate/build-requirements.txt --require-build-hashes \
    images/worker/candidate/runtime-requirements.txt
  "$uv_bin" pip check --python "$target/bin/python"
fi
