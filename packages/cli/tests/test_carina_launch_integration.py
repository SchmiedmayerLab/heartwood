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
import tomllib
from pathlib import Path

from heartwood.adapters.platform import select_platform_adapter
from heartwood.gateway import (
    ProjectConfig,
    ProjectConfigStore,
    ProjectContext,
    persist_deployment_profile,
)


def _write_executable(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(contents).lstrip(), encoding="utf-8")
    path.chmod(0o755)


def _write_python_executable(path: Path, contents: str) -> None:
    script = textwrap.dedent(contents).lstrip()
    _write_executable(path, f"#!{sys.executable}\n{script}")


def _write_model_snapshot(root: Path) -> None:
    root.mkdir(parents=True)
    weights = root / "weights.safetensors"
    weights.write_bytes(b"synthetic-carina-model")
    digest = hashlib.sha256(weights.read_bytes()).hexdigest()
    (root / "SHA256SUMS").write_text(f"{digest}  weights.safetensors\n", encoding="utf-8")


def test_carina_launch_handoff_setup_and_cleanup(tmp_path: Path) -> None:
    project_root = tmp_path / "agent-project"
    project_root.mkdir()
    project = ProjectContext(project_root)
    project.initialize()
    model_root = project.models_dir / "synthetic-model"
    scratch = tmp_path / "job-scratch"
    scheduler_bin = tmp_path / "scheduler-bin"
    runtime_root = tmp_path / "runtimes" / "test"
    srun_log = tmp_path / "srun-export.txt"
    _write_model_snapshot(model_root)
    scratch.mkdir()

    carina_env = {"HEARTWOOD_PLATFORM": "carina"}
    persist_deployment_profile(project, model_source="heartwood", env=carina_env)
    adapter = select_platform_adapter(carina_env)
    ProjectConfigStore(
        project,
        ProjectConfig(
            platform_id="carina",
            policy=adapter.default_policy_profile(),
        ),
    ).select_local_model(
        artifact_id="synthetic-model",
        path=model_root,
        runtime="vllm",
        model_id="test-model",
        minimum_gpu_count=1,
        minimum_gpu_memory_bytes=1,
        tool_call_parser="hermes",
    )

    _write_executable(
        scheduler_bin / "sinfo",
        """
        #!/usr/bin/env bash
        set -euo pipefail
        printf 'dev*|gpu:nvidia_l40s:8|up|512000|128\n'
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
        workdir=""
        command=()
        while (($#)); do
          case "$1" in
            --chdir=*) workdir="${1#--chdir=}"; shift ;;
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
        : "${workdir:?missing Slurm working directory}"
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
        cd "${workdir}"
        exec env -i "${clean_environment[@]}" "${command[@]}"
        """,
    )
    _write_executable(
        scheduler_bin / "nvidia-smi",
        """
        #!/usr/bin/env bash
        set -euo pipefail
        printf '0, NVIDIA L40S, 46068, 45000, 570.148.08, 8.9\n'
        """,
    )
    heartwood_python = runtime_root / "heartwood" / "bin" / "python"
    heartwood_python.parent.mkdir(parents=True)
    heartwood_python.symlink_to(sys.executable)
    _write_executable(
        runtime_root / "vllm" / "bin" / "python",
        """
        #!/usr/bin/env bash
        set -euo pipefail
        echo '0.25.1+cu129 2.11.0+cu129 12.9'
        """,
    )
    _write_python_executable(
        runtime_root / "vllm" / "bin" / "heartwood-vllm",
        r"""
        import json
        import os
        import signal
        import sys
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from pathlib import Path

        if sys.argv[1:] == ["__heartwood_verify_runtime__"]:
            print("Transformers 5.5.0 integration verified")
            raise SystemExit(0)

        model_id = sys.argv[sys.argv.index("--served-model-name") + 1]
        runtime = Path.cwd() / ".heartwood" / "runtime"
        runtime.mkdir(parents=True, exist_ok=True)
        (runtime / "synthetic-vllm-environment.json").write_text(
            json.dumps(
                {
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
        "PYTHONPATH": os.pathsep.join(sys.path),
        "HEARTWOOD_PLATFORM": "carina",
        "HEARTWOOD_TEST_SRUN_LOG": str(srun_log),
        "HEARTWOOD_TEST_SCRATCH": str(scratch),
        "HEARTWOOD_TEST_SECRET": "must-not-cross-allocation",
    }
    completed = subprocess.run(
        (
            str(heartwood_python),
            "-m",
            "heartwood.cli",
            "--session-id",
            "carina-integration",
            "--plain",
            "runtime",
            "start",
            "--yes-request-allocation",
            "--startup-timeout",
            "10",
        ),
        check=False,
        capture_output=True,
        text=True,
        input="/status\n/exit\n",
        timeout=30,
        env=env,
        cwd=project_root,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "GPU partition: dev" in completed.stdout
    assert "[1/6] Verify the selected Heartwood-managed model" in completed.stdout
    assert "[6/6] Open session carina-integration" in completed.stdout
    assert "Readiness: ready" in completed.stdout
    assert "recovery-required" not in completed.stdout
    export_value = srun_log.read_text(encoding="utf-8")
    assert "HEARTWOOD_HOME" not in export_value
    assert "HEARTWOOD_MODEL_CACHE" not in export_value
    assert "HEARTWOOD_TEST_SECRET" not in export_value
    runtime_environment = json.loads(
        (project.runtime_dir / "synthetic-vllm-environment.json").read_text(encoding="utf-8")
    )
    assert runtime_environment["hf_home"] == str(project.cache_dir / "huggingface")
    assert runtime_environment["ld_library_path"].startswith(
        str(runtime_root / "bootstrap" / "lib")
    )
    assert runtime_environment["sampler"] == "0"
    assert not runtime_environment["secret_present"]
    assert runtime_environment["path"].startswith(str(runtime_root / "bootstrap" / "bin"))
    config = tomllib.loads(project.config_path.read_text(encoding="utf-8"))
    assert config["platform_id"] == "carina"
    assert config["model_source"] == "heartwood"
    assert config["models"]["active_profile"] == "heartwood"
    assert (project.runtime_dir / "synthetic-vllm-stopped").is_file()
    assert not any(scratch.iterdir())
