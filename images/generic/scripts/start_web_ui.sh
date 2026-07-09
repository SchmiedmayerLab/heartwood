#!/usr/bin/env bash
# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

set -euo pipefail

workspace="${HEARTWOOD_WORKSPACE:-/tmp/heartwood-web-session}"
host="${HEARTWOOD_WEB_HOST:-127.0.0.1}"
port="${HEARTWOOD_WEB_PORT:-8767}"
web_root="${HEARTWOOD_WEB_ROOT:-/opt/heartwood/packages/webui/dist}"
base_path="${HEARTWOOD_WEB_BASE_PATH:-/}"

mkdir -p "${workspace}"

exec heartwood \
  --workspace "${workspace}" \
  serve \
  --host "${host}" \
  --port "${port}" \
  --web-root "${web_root}" \
  --base-path "${base_path}"
