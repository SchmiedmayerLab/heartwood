# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Exercise the packaged worker with synthetic credentials and no network or host mounts."""

from __future__ import annotations

import argparse
import os
import secrets
import subprocess

_PROBE = r"""
import json
import os
import shlex
import time
import httpx
from openhands.sdk.workspace import RemoteWorkspace

key = os.environ["SESSION_API_KEY"]
for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
    os.environ.pop(name, None)
with httpx.Client(base_url="http://127.0.0.1:8000", trust_env=False, timeout=5) as client:
    for attempt in range(60):
        try:
            result = client.get("/api/conversations/search", headers={"X-Session-API-Key": key})
            if result.status_code == 200:
                break
        except httpx.TransportError:
            pass
        time.sleep(1)
    else:
        raise AssertionError("worker did not become ready")
    assert client.get("/api/conversations/search").status_code == 401
    wrong = client.get("/api/conversations/search", headers={"X-Session-API-Key": "wrong"})
    assert wrong.status_code == 401

workspace = RemoteWorkspace(host="http://127.0.0.1:8000", api_key=key, working_dir="/workspace")
source = 'import json,os;print(json.dumps({"uid":os.geteuid(),"env":dict(os.environ)}))'
result = workspace.execute_command(shlex.join(["python", "-I", "-c", source]), timeout=20)
assert result.exit_code == 0, "native command failed"
data = json.loads(result.stdout)
assert data["uid"] == 10001
for name in ("AWS_SECRET_ACCESS_KEY", "GH_TOKEN", "HF_TOKEN", "JUPYTERHUB_API_TOKEN",
             "HEARTWOOD_GATEWAY_CAPABILITY", "UNRECOGNIZED_SECRET", "HTTPS_PROXY", "BASH_ENV"):
    assert name not in data["env"], f"unexpected inherited variable: {name}"
assert data["env"]["HOME"] == "/home/heartwood"
assert data["env"]["HEARTWOOD_INFERENCE_PLACEHOLDER"] == "synthetic-placeholder"
assert data["env"]["OH_TELEMETRY_CONSENT"] == "denied"
assert workspace.execute_command("printf retained > /workspace/probe", timeout=10).exit_code == 0
readback = workspace.execute_command("cat /workspace/probe", timeout=10)
assert readback.exit_code == 0 and readback.stdout.strip() == "retained"
assert workspace.execute_command("test ! -e /var/run/docker.sock", timeout=10).exit_code == 0
assert workspace.execute_command("test ! -w /etc/heartwood/worker.json", timeout=10).exit_code == 0
workspace.reset_client()
import errno
import runpy
bootstrap = runpy.run_path("/opt/heartwood/images/worker/launch.py")
read_fd, write_fd = os.pipe()
os.set_inheritable(write_fd, True)
def verify_exec(executable, arguments, environment):
    for descriptor in (read_fd, write_fd):
        try:
            os.fstat(descriptor)
        except OSError as error:
            assert error.errno == errno.EBADF
        else:
            raise AssertionError("bootstrap retained an inherited descriptor")
    assert arguments[1:6] == ["-I", "-B", "-u", "-m", "heartwood.gateway.worker_server"]
    assert environment["SESSION_API_KEY"] == key
os.execve = verify_exec
bootstrap["main"]()
print(json.dumps({"authenticated_native_commands": True, "missing_and_wrong_key_denied": True,
                  "worker_environment_filtered": True, "workspace_retained": True,
                  "configuration_read_only": True, "inherited_descriptors_closed": True}))
"""


def main() -> None:
    """Run the image check and remove only the temporary container it creates."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", help="Locally built worker image tag or digest")
    args = parser.parse_args()
    name = "heartwood-worker-smoke-" + secrets.token_hex(8)
    environment = dict(os.environ)
    environment["SESSION_API_KEY"] = secrets.token_urlsafe(32)
    command = [
        "docker",
        "run",
        "--detach",
        "--name",
        name,
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--cpus",
        "1",
        "--memory",
        "2g",
        "--pids-limit",
        "256",
        "--env",
        "SESSION_API_KEY",
        "--env",
        "HEARTWOOD_INFERENCE_PLACEHOLDER=synthetic-placeholder",
        "--env",
        "HTTPS_PROXY=http://unapproved.invalid",
        "--env",
        "OH_ENABLE_VSCODE=true",
        "--env",
        "OH_TELEMETRY_CONSENT=granted",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=128m",
        "--tmpfs",
        "/home/heartwood:rw,nosuid,nodev,uid=10001,gid=10001,mode=700,size=256m",
        "--tmpfs",
        "/workspace:rw,nosuid,nodev,uid=10001,gid=10001,mode=700,size=64m",
    ]
    for variable in (
        "AWS_SECRET_ACCESS_KEY",
        "GH_TOKEN",
        "HF_TOKEN",
        "JUPYTERHUB_API_TOKEN",
        "HEARTWOOD_GATEWAY_CAPABILITY",
        "UNRECOGNIZED_SECRET",
        "BASH_ENV",
    ):
        command.extend(["--env", variable + "=synthetic-must-not-reach-server"])
    command.append(args.image)
    subprocess.run(command, env=environment, check=True, capture_output=True, text=True)
    try:
        subprocess.run(
            ["docker", "exec", "--interactive", name, "/opt/heartwood/.venv/bin/python", "-I", "-"],
            input=_PROBE,
            text=True,
            check=True,
            timeout=100,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        subprocess.run(["docker", "inspect", "--format", "{{json .State}}", name], check=False)
        subprocess.run(["docker", "logs", "--tail", "40", name], check=False)
        raise
    finally:
        subprocess.run(["docker", "rm", "--force", name], check=True, capture_output=True)


if __name__ == "__main__":
    main()
