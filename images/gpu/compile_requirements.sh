#!/usr/bin/env bash
# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."

uv pip compile \
  --python-version 3.12 \
  --python-platform x86_64-manylinux_2_28 \
  --generate-hashes \
  --emit-index-url \
  --exclude-newer 2026-09-11T23:00:00Z \
  --override images/gpu/vllm-overrides.txt \
  --exclude images/gpu/vllm-exclusions.txt \
  --custom-compile-command "bash images/gpu/compile_requirements.sh" \
  --output-file images/gpu/vllm-requirements.txt \
  images/gpu/vllm.in \
  --quiet
