# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path


def _write_executable(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(contents).lstrip(), encoding="utf-8")
    path.chmod(0o755)


def _write_model_snapshot(root: Path) -> None:
    root.mkdir(parents=True)
    weights = root / "weights.safetensors"
    weights.write_bytes(b"synthetic-carina-model")
    digest = hashlib.sha256(weights.read_bytes()).hexdigest()
    (root / "SHA256SUMS").write_text(f"{digest}  weights.safetensors\n", encoding="utf-8")


def test_carina_launch_handoff_setup_and_cleanup(tmp_path: Path) -> None:
    native_root = tmp_path / "heartwood"
    state_root = native_root / "state"
    model_cache = native_root / "models"
    model_root = model_cache / "synthetic-model"
    scratch = tmp_path / "job-scratch"
    scheduler_bin = tmp_path / "scheduler-bin"
    runtime_root = native_root / "runtimes" / "test"
    srun_log = tmp_path / "srun-export.txt"
    _write_model_snapshot(model_root)
    scratch.mkdir()

    _write_executable(
        scheduler_bin / "sinfo",
        """
        #!/usr/bin/env bash
        set -euo pipefail
        printf 'dev*|gpu:nvidia_l40s:8|up\n'
        """,
    )
    _write_executable(
        scheduler_bin / "srun",
        """
        #!/usr/bin/env bash
        set -euo pipefail
        log="${HEARTWOOD_TEST_SRUN_LOG:?}"
        scratch="${HEARTWOOD_TEST_SCRATCH:?}"
        export_value=""
        command=()
        while (($#)); do
          case "$1" in
            --export=*)
              export_value="${1#--export=}"
              shift
              command=("$@")
              break
              ;;
            *) shift ;;
          esac
        done
        : "${export_value:?missing Slurm export allowlist}"
        ((${#command[@]})) || { echo "missing allocation command" >&2; exit 64; }
        printf '%s\n' "${export_value}" >"${log}"
        clean_environment=()
        IFS=',' read -r -a exports <<<"${export_value}"
        for entry in "${exports[@]}"; do
          if [[ "${entry}" == *=* ]]; then
            clean_environment+=("${entry}")
          elif [[ -n "${!entry-}" ]]; then
            clean_environment+=("${entry}=${!entry}")
          fi
        done
        clean_environment+=(
          "SLURM_JOB_ID=synthetic-job"
          "SLURM_JOB_PARTITION=dev"
          "SLURM_CLUSTER_NAME=carina"
          "LOCAL_SCRATCH_JOB=${scratch}"
          "CUDA_VISIBLE_DEVICES=0"
        )
        exec env -i "${clean_environment[@]}" "${command[@]}"
        """,
    )
    _write_executable(
        runtime_root / "vllm" / "bin" / "python",
        """
        #!/usr/bin/env bash
        set -euo pipefail
        echo '0.14.0 0.25.0'
        """,
    )
    _write_executable(
        runtime_root / "vllm" / "bin" / "vllm",
        r"""
        #!/usr/bin/env python3
        import json
        import os
        import signal
        import sys
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from pathlib import Path

        model_id = sys.argv[sys.argv.index("--served-model-name") + 1]
        state = Path(os.environ["HEARTWOOD_HOME"])
        runtime = state / "runtime"
        runtime.mkdir(parents=True, exist_ok=True)
        (runtime / "synthetic-vllm-environment.json").write_text(
            json.dumps(
                {
                    "heartwood_model_cache": os.environ.get("HEARTWOOD_MODEL_CACHE"),
                    "hf_home": os.environ.get("HF_HOME"),
                    "ld_library_path": os.environ.get("LD_LIBRARY_PATH"),
                    "path": os.environ.get("PATH"),
                    "sampler": os.environ.get("VLLM_USE_FLASHINFER_SAMPLER"),
                    "secret_present": "HEARTWOOD_TEST_SECRET" in os.environ,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path != "/v1/models":
                    self.send_error(404)
                    return
                payload = json.dumps({"data": [{"id": model_id}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format, *args):
                pass

        def stop(_signum, _frame):
            (runtime / "synthetic-vllm-stopped").write_text("stopped\n", encoding="utf-8")
            raise SystemExit(0)

        signal.signal(signal.SIGTERM, stop)
        ThreadingHTTPServer(("127.0.0.1", 8765), Handler).serve_forever()
        """,
    )
    (runtime_root / "bootstrap" / "bin").mkdir(parents=True)
    (runtime_root / "bootstrap" / "lib").mkdir()

    env = {
        **os.environ,
        "PATH": f"{scheduler_bin}{os.pathsep}{os.environ['PATH']}",
        "HEARTWOOD_PLATFORM": "carina",
        "HEARTWOOD_NATIVE_ROOT": str(native_root),
        "HEARTWOOD_NATIVE_VERSION": "test",
        "HEARTWOOD_HOME": str(state_root),
        "HEARTWOOD_MODEL_CACHE": str(model_cache),
        "HF_HOME": str(native_root / "cache" / "huggingface"),
        "HEARTWOOD_TEST_SRUN_LOG": str(srun_log),
        "HEARTWOOD_TEST_SCRATCH": str(scratch),
        "HEARTWOOD_TEST_SECRET": "must-not-cross-allocation",
    }
    completed = subprocess.run(
        (
            sys.executable,
            "-m",
            "heartwood.cli",
            "--workspace",
            str(state_root / "sessions"),
            "--session-id",
            "carina-integration",
            "launch",
            "--model-root",
            str(model_root),
            "--yes-request-allocation",
            "--startup-timeout",
            "10",
            "--plain",
        ),
        check=False,
        capture_output=True,
        text=True,
        input="/status\n/exit\n",
        timeout=30,
        env=env,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "GPU partition: dev" in completed.stdout
    assert "[1/6] Verify the selected model snapshot" in completed.stdout
    assert "[6/6] Open session carina-integration" in completed.stdout
    assert "State: ready" in completed.stdout
    assert "recovery-required" not in completed.stdout
    export_value = srun_log.read_text(encoding="utf-8")
    assert "HEARTWOOD_MODEL_CACHE" in export_value
    assert "HEARTWOOD_TEST_SECRET" not in export_value
    runtime_environment = json.loads(
        (state_root / "runtime" / "synthetic-vllm-environment.json").read_text(encoding="utf-8")
    )
    assert runtime_environment["heartwood_model_cache"] == str(model_cache)
    assert runtime_environment["hf_home"] == str(native_root / "cache" / "huggingface")
    assert runtime_environment["ld_library_path"].startswith(
        str(runtime_root / "bootstrap" / "lib")
    )
    assert runtime_environment["sampler"] == "0"
    assert not runtime_environment["secret_present"]
    assert runtime_environment["path"].startswith(str(runtime_root / "bootstrap" / "bin"))
    setup = json.loads((state_root / "setup.json").read_text(encoding="utf-8"))
    assert setup["platform_id"] == "carina"
    assert setup["model_source"] == "local"
    assert (state_root / "models.json").is_file()
    assert (state_root / "runtime" / "synthetic-vllm-stopped").is_file()
    assert not any(scratch.iterdir())
