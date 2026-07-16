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
project_root="$(pwd)"

if [ "${project_root}" = "${runtime_root}" ] || [ "${project_root}" = "${platform_home}" ]; then
  echo "run the Terra image smoke from a dedicated project directory" >&2
  exit 1
fi

test -x "${runtime_root}/.venv/bin/heartwood"
test -x "${runtime_root}/.venv/bin/python"
test -f "${runtime_root}/docs/terra-jupyter-demo.ipynb"
test -f "${runtime_root}/docs/terra-jupyter-demo.md"
test -d "${runtime_root}/packages/webui/dist"
test -f "${runtime_root}/images/generic/scripts/offline_stack_smoke.sh"
test -f "${runtime_root}/packages/webui/dist/index.html"
heartwood serve --help >/dev/null
test -d "${platform_home}"

heartwood --version
readiness_json="$(heartwood doctor --json)"
HEARTWOOD_EXPECTED_PROJECT="${project_root}" "${heartwood_python}" -c '
import json
import os
import sys
from pathlib import Path

readiness = json.load(sys.stdin)
expected = Path(os.environ["HEARTWOOD_EXPECTED_PROJECT"]).resolve()
checks = {item["check_id"]: item for item in readiness["checks"]}
assert readiness["platform_id"] == "terra"
assert Path(readiness["project_root"]).resolve() == expected
assert checks["terra-project-storage"]["status"] == "pass"
assert checks["terra-gpu-runtime"]["summary"] == (
    "Portable Terra runtime selected; local models use CPU inference"
)
' <<<"${readiness_json}"
HEARTWOOD_EXPECTED_PROJECT="${project_root}" "${heartwood_python}" - <<'PY'
import os
from pathlib import Path

from heartwood.gateway import ProjectContext, SessionGateway
from heartwood.notebook import (
    NotebookSession,
    has_authenticated_jupyter_proxy,
    jupyter_proxy_url,
)

expected = Path(os.environ["HEARTWOOD_EXPECTED_PROJECT"]).resolve()
project = ProjectContext.current()
assert project.root == expected
project.initialize()
assert project.state_root == expected / ".heartwood"
assert project.state_path.is_file()

gateway = SessionGateway()
assert gateway.project.root == expected
assert gateway.project_readiness()["project_root"] == str(expected)

session = NotebookSession(session_id="terra-image-smoke")
assert session.project.root == expected
view = session.detect()
assert view.session_id == "terra-image-smoke"
assert not has_authenticated_jupyter_proxy(env={})
proxy_env = {"GOOGLE_PROJECT": "heartwood-ci", "CLUSTER_NAME": "terra-image-smoke"}
assert has_authenticated_jupyter_proxy(env=proxy_env)
assert jupyter_proxy_url(port=8767, env=proxy_env) == (
    "/proxy/heartwood-ci/terra-image-smoke/jupyter/proxy/8767/"
)
PY

if [ -d "${jupyter_prefix}/share/jupyter/kernels" ]; then
  test -f "${jupyter_prefix}/share/jupyter/kernels/heartwood/kernel.json"
fi

echo "Terra platform image smoke: ok"
