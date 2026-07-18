#!/usr/bin/env bash
# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

set -euo pipefail

runtime_root="${HEARTWOOD_RUNTIME_ROOT:-/opt/heartwood}"
platform_home="${HEARTWOOD_PLATFORM_HOME:-/home/jupyter}"
platform_user="${HEARTWOOD_PLATFORM_USER:-jupyter}"
jupyter_prefix="${HEARTWOOD_JUPYTER_PREFIX:-/opt/conda}"
jupyter_home="${JUPYTER_HOME:-/etc/jupyter}"
heartwood_python="${HEARTWOOD_PYTHON:-${runtime_root}/.venv/bin/python}"

if [ "$(id -un)" != "${platform_user}" ]; then
  echo "expected user ${platform_user}, got $(id -un)" >&2
  exit 1
fi
if [ "$(pwd)" != "${platform_home}" ]; then
  echo "expected working directory ${platform_home}, got $(pwd)" >&2
  exit 1
fi
if [ "${JUPYTER_PORT:-}" != "8000" ]; then
  echo "expected JUPYTER_PORT=8000, got ${JUPYTER_PORT:-unset}" >&2
  exit 1
fi
touch "${platform_home}/.heartwood-jupyter-contract-write"
rm "${platform_home}/.heartwood-jupyter-contract-write"
mkdir -p "${platform_home}/.ipython"
touch "${platform_home}/.ipython/heartwood-jupyter-contract-write"
rm "${platform_home}/.ipython/heartwood-jupyter-contract-write"

test -x "${jupyter_prefix}/bin/python3"
test -x "${jupyter_prefix}/bin/jupyter"
test -x "${jupyter_prefix}/bin/jupyter-notebook"
test -x "${jupyter_home}/scripts/run-jupyter.sh"
test -f "${jupyter_home}/jupyter_notebook_config.py"

case "$(command -v python)" in
  "${runtime_root}/.venv/bin/python")
    echo "Heartwood venv must not shadow the platform Python" >&2
    exit 1
    ;;
esac
case "$(command -v jupyter)" in
  "${jupyter_prefix}/bin/jupyter")
    ;;
  *)
    echo "expected platform jupyter on PATH, got $(command -v jupyter)" >&2
    exit 1
    ;;
esac

"${jupyter_prefix}/bin/python3" - <<'PY'
import notebook
from notebook.notebookapp import NotebookApp

assert NotebookApp is not None
assert notebook.__version__
PY

kernel_json="$("${jupyter_prefix}/bin/jupyter" kernelspec list --json)"
printf '%s\n' "${kernel_json}" | "${heartwood_python}" -c '
import json
import sys

data = json.load(sys.stdin)
kernels = data.get("kernelspecs", {})
heartwood = kernels.get("heartwood")
if not isinstance(heartwood, dict):
    raise SystemExit("Heartwood Jupyter kernel is not registered")
spec = heartwood.get("spec", {})
argv = spec.get("argv", [])
if not argv or "/opt/heartwood/.venv/bin/python" not in argv[0]:
    raise SystemExit(f"Heartwood kernel does not use the Heartwood Python: {argv}")
environment = spec.get("env", {})
if environment.get("IPYTHONDIR") != "/tmp/heartwood-ipython":
    raise SystemExit(f"Heartwood kernel does not isolate incompatible IPython startup files: {environment}")
'

"${heartwood_python}" - <<'PY'
from pathlib import Path

from heartwood.gateway import ProjectContext

assert ProjectContext.current().root == Path.cwd().resolve()
PY

project_root="$(mktemp -d "${platform_home}/heartwood-contract.XXXXXX")"
cd "${project_root}"

"${heartwood_python}" - <<'PY'
from pathlib import Path

from heartwood.gateway import ProjectContext
from heartwood.notebook import NotebookSession

session = NotebookSession(session_id="terra-jupyter-contract")
assert session.project.root == Path.cwd().resolve()
assert ProjectContext.current().state_root == Path.cwd() / ".heartwood"
view = session.detect()
assert view.session_id == "terra-jupyter-contract"
PY

test -f "${project_root}/.heartwood/state.json"
test ! -e "${platform_home}/.heartwood/state.json"

grep -q "NotebookApp.port = 8000" "${jupyter_home}/jupyter_notebook_config.py"
grep -q 'NotebookApp.base_url = "/notebooks"' "${jupyter_home}/jupyter_notebook_config.py" \
  || grep -q "NotebookApp.base_url = '/notebooks'" "${jupyter_home}/jupyter_notebook_config.py"

echo "Terra Jupyter contract smoke: ok"
