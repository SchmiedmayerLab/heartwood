#!/usr/bin/env bash
# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

set -euo pipefail

assets="${1:?asset directory is required}"
assets="$(cd "${assets}" && pwd -P)"
workspace="$(mktemp -d)"
cleanup() {
  rm -rf "${workspace}"
}
trap cleanup EXIT

installation="${workspace}/installation"
project="${workspace}/synthetic-project"
install_release() {
  "${assets}/heartwood-installer" \
    --bundle "${assets}/heartwood-native.tar.gz" \
    --checksums "${assets}/SHA256SUMS" \
    --root "${installation}" \
    --minimum-free-gib 1 \
    --platform generic
}

install_release

generation="$(cd "${installation}/current" && pwd -P)"
source="${generation}/source"
runtime="${generation}/runtime"
test -x "${runtime}/heartwood/bin/python"
test -x "${runtime}/heartwood/bin/heartwood"
test -x "${runtime}/llama.cpp/llama-server"
test -f "${runtime}/llama.cpp/.heartwood-runtime.tar.gz"
test -x "${installation}/bin/heartwood-jupyter"
test -f "${source}/packages/webui/dist/index.html"
test ! -e "${installation}/.installer"
"${installation}/bin/heartwood" --version | grep --quiet '^heartwood '
"${installation}/bin/heartwood-jupyter" --version | grep --quiet '^[0-9]'
"${installation}/bin/heartwood" models managed | grep --quiet 'Qwen'

mkdir -m 700 "${project}"
(
  cd "${project}"
  "${installation}/bin/heartwood" doctor | grep --quiet 'Readiness: setup-required'
  "${installation}/bin/heartwood" models download llama-cpp-stories260k-ci
  PATH="${runtime}/llama.cpp:${runtime}/heartwood/bin:${PATH}" \
  LD_LIBRARY_PATH="${runtime}/llama.cpp${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}" \
  HEARTWOOD_LOCAL_MODEL_PATH="${project}/.heartwood/models/llama-cpp-stories260k-ci/tinyllamas/stories260K.gguf" \
    bash "${source}/images/generic/scripts/local_inference_smoke.sh"

  "${installation}/bin/heartwood" --interface web --host 127.0.0.1 --port 18767 \
    >"${workspace}/web.log" 2>&1 &
  web_pid="$!"
  jupyter_pid=""
  # shellcheck disable=SC2329  # Invoked by the EXIT trap below.
  cleanup_services() {
    if [[ -n "${jupyter_pid}" ]]; then
      kill "${jupyter_pid}" >/dev/null 2>&1 || true
      wait "${jupyter_pid}" >/dev/null 2>&1 || true
    fi
    kill "${web_pid}" >/dev/null 2>&1 || true
    wait "${web_pid}" >/dev/null 2>&1 || true
  }
  trap cleanup_services EXIT
  "${runtime}/heartwood/bin/python" - <<'PY'
import json
import time
import urllib.error
import urllib.request

deadline = time.time() + 60
last_error = None
while time.time() < deadline:
    try:
        with urllib.request.urlopen("http://127.0.0.1:18767/", timeout=2) as response:
            page = response.read().decode()
        with urllib.request.urlopen(
            "http://127.0.0.1:18767/project/startup?interface=web", timeout=2
        ) as response:
            startup = json.load(response)
        if "<title>Heartwood</title>" in page and startup["interface"] == "web":
            break
    except (OSError, KeyError, urllib.error.URLError) as error:
        last_error = error
        time.sleep(0.2)
else:
    raise SystemExit(f"native browser interface did not become ready: {last_error}")
PY

  "${installation}/bin/heartwood-jupyter" \
    --allow-root \
    --no-browser \
    --ServerApp.ip=127.0.0.1 \
    --ServerApp.port=18768 \
    --ServerApp.port_retries=0 \
    --ServerApp.token=heartwood-native-smoke \
    >"${workspace}/jupyter.log" 2>&1 &
  jupyter_pid="$!"
  "${runtime}/heartwood/bin/python" - <<'PY'
import json
import time
import urllib.error
import urllib.request

from heartwood.notebook import NotebookSession

deadline = time.time() + 60
last_error = None
while time.time() < deadline:
    try:
        with urllib.request.urlopen(
            "http://127.0.0.1:18768/api/status?token=heartwood-native-smoke",
            timeout=2,
        ) as response:
            status = json.load(response)
        if isinstance(status, dict):
            break
    except (OSError, ValueError, urllib.error.URLError) as error:
        last_error = error
        time.sleep(0.2)
else:
    raise SystemExit(f"native Jupyter launcher did not become ready: {last_error}")

with urllib.request.urlopen(
    "http://127.0.0.1:18768/api/kernelspecs?token=heartwood-native-smoke",
    timeout=5,
) as response:
    kernels = json.load(response).get("kernelspecs", {})
if "python3" not in kernels:
    raise SystemExit("native Jupyter launcher did not expose the packaged Python kernel")

start_request = urllib.request.Request(
    "http://127.0.0.1:18768/api/kernels?token=heartwood-native-smoke",
    data=json.dumps({"name": "python3"}).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(start_request, timeout=15) as response:
    kernel_id = json.load(response).get("id")
if not isinstance(kernel_id, str) or not kernel_id:
    raise SystemExit("native Jupyter launcher did not start the packaged Python kernel")
stop_request = urllib.request.Request(
    f"http://127.0.0.1:18768/api/kernels/{kernel_id}?token=heartwood-native-smoke",
    method="DELETE",
)
with urllib.request.urlopen(stop_request, timeout=15) as response:
    if response.status != 204:
        raise SystemExit("native Jupyter launcher did not stop the packaged Python kernel")

with NotebookSession(session_id="native-jupyter-smoke") as session:
    if session.startup_plan()["interface"] != "notebook":
        raise SystemExit("native notebook bridge did not return the notebook startup plan")
PY
)

printf '\nheartwood-runtime-tamper-test\n' >>"${runtime}/llama.cpp/llama-server"
printf '\nheartwood-source-tamper-test\n' >>"${source}/README.md"
previous_generation="${generation}"
install_release
generation="$(cd "${installation}/current" && pwd -P)"
source="${generation}/source"
runtime="${generation}/runtime"
test "${generation}" != "${previous_generation}"
test -d "${previous_generation}"
if grep --binary-files=text --quiet 'heartwood-runtime-tamper-test' \
  "${runtime}/llama.cpp/llama-server"; then
  echo "native installer did not rebuild a modified llama.cpp runtime" >&2
  exit 1
fi
if grep --quiet 'heartwood-source-tamper-test' "${source}/README.md"; then
  echo "native installer did not restore release source from the verified bundle" >&2
  exit 1
fi
"${installation}/bin/heartwood" --version | grep --quiet '^heartwood '

printf 'Real native installer smoke test: ok\n'
