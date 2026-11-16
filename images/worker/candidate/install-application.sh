#!/usr/bin/env bash
# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

# Install the hash-pinned Heartwood application wheels into an existing environment.
set -euo pipefail
if [[ $# != 3 ]]; then
  echo "Usage: $0 UV PYTHON APPLICATION_DIRECTORY" >&2
  exit 2
fi
# Bundled Skill trees are verified against release-pinned complete-tree digests, so no
# bytecode may be written into them; the runtime sets PYTHONDONTWRITEBYTECODE as well.
UV_COMPILE_BYTECODE=0 "$1" pip install --no-config --python "$2" \
  --no-index --no-deps --require-hashes --link-mode copy --find-links "$3" \
  --requirements "$3/application-requirements.txt"
