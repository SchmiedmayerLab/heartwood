# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Run a packaged inference runtime and its qualification through a private socket."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
from contextlib import suppress
from pathlib import Path

from heartwood.cli._launch import _run_interaction, _wait_for_runtime
from heartwood.gateway._managed_inference import (
    MANAGED_RUNTIME_SOCKET_ENV,
    managed_inference_socket,
)


def main() -> int:
    """Use the application readiness and supervision path for packaged runtimes."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a qualification command is required")
    with managed_inference_socket(args.project) as socket_path:
        env = {**os.environ, MANAGED_RUNTIME_SOCKET_ENV: str(socket_path)}
        with args.log.open("w", encoding="utf-8") as log:
            runtime = subprocess.Popen(
                ["bash", str(args.runtime)],
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
                umask=0o077,
            )
        try:
            if not _wait_for_runtime(
                runtime,
                model_id=env.get("HEARTWOOD_MANAGED_MODEL_ALIAS", "heartwood-managed-runtime"),
                socket_path=socket_path,
                timeout=args.timeout,
            ):
                print(f"Managed runtime did not become ready; see {args.log}.")
                return 70
            return _run_interaction(command, env=env, cwd=args.project, runtime=runtime)
        finally:
            # vLLM can leave engine children after its serving process exits.
            # The session belongs to this fixture; never target an inherited group.
            with suppress(ProcessLookupError):
                os.killpg(runtime.pid, signal.SIGTERM)
            with suppress(subprocess.TimeoutExpired):
                runtime.wait(timeout=10)
            with suppress(ProcessLookupError):
                os.killpg(runtime.pid, signal.SIGKILL)
            runtime.wait()


if __name__ == "__main__":
    raise SystemExit(main())
