#!/usr/bin/env bash
# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

set -euo pipefail

runtime_root="${HEARTWOOD_RUNTIME_ROOT:-/opt/heartwood}"
platform_home="${HEARTWOOD_PLATFORM_HOME:-/home/jupyter}"
jupyter_prefix="${HEARTWOOD_JUPYTER_PREFIX:-/opt/conda}"
heartwood_python="${HEARTWOOD_PYTHON:-${runtime_root}/.venv/bin/python}"

cd "${runtime_root}"

test -x "${runtime_root}/.venv/bin/heartwood"
test -x "${runtime_root}/.venv/bin/python"
test -f "${runtime_root}/docs/terra-jupyter-demo.ipynb"
test -f "${runtime_root}/docs/terra-jupyter-demo.md"
test -d "${runtime_root}/packages/webui/dist"
test -f "${runtime_root}/images/generic/scripts/offline_stack_smoke.sh"
test -f "${runtime_root}/images/generic/scripts/start_web_ui.sh"
test -d "${platform_home}"

heartwood --version
"${heartwood_python}" - <<'PY'
from pathlib import Path

from heartwood.notebook import NotebookSession, jupyter_proxy_url

workspace = Path("/tmp/heartwood-platform-smoke")
session = NotebookSession(workspace=workspace, session_id="terra-image-smoke")
view = session.detect()
assert view.session_id == "terra-image-smoke"
assert jupyter_proxy_url(port=8767).endswith("/proxy/8767/")
PY

if [ -d "${jupyter_prefix}/share/jupyter/kernels" ]; then
  test -f "${jupyter_prefix}/share/jupyter/kernels/heartwood/kernel.json"
fi

echo "Terra platform image smoke: ok"
