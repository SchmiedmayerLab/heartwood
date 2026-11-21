# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT
"""Run the host Chromium matrix through the synthetic kind cluster's DNS."""

import asyncio
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

# This code runs in a loopback-only pod. TLS is end-to-end from Chrome to the service.
RELAY = r"""
import asyncio
import re

targets = re.compile(
    rb'(?:proxy-public\.heartwood-platform\.svc\.cluster\.local:443|'
    rb'keycloak\.heartwood-platform\.svc\.cluster\.local:8443|'
    rb'r[0-9a-f]{32}\.(?:demo|other|small)\.projects\.heartwood\.test:443)')

async def copy(reader, writer):
    while chunk := await reader.read(65536):
        writer.write(chunk)
        await writer.drain()

async def connect(reader, writer):
    upstream = None
    tasks = []
    try:
        headers = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 10)
        method, address, protocol = headers.split(b'\r\n', 1)[0].split()
        if method != b'CONNECT' or not targets.fullmatch(address):
            writer.write(b'HTTP/1.1 403 Forbidden\r\n\r\n')
            await writer.drain()
            return
        host, port = address.decode('ascii').split(':')
        remote, upstream = await asyncio.wait_for(asyncio.open_connection(host, int(port)), 10)
        writer.write(b'HTTP/1.1 200 Connection Established\r\n\r\n')
        await writer.drain()
        tasks = [asyncio.create_task(copy(reader, upstream)),
                 asyncio.create_task(copy(remote, writer))]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except (ValueError, OSError, EOFError, asyncio.LimitOverrunError):
        pass
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if upstream:
            upstream.close()
        writer.close()

async def main():
    server = await asyncio.start_server(connect, '127.0.0.1', 8888, limit=8192)
    async with server:
        await server.serve_forever()

asyncio.run(main())
"""

KUBE = ["kubectl", "--context=kind-heartwood-origins-revision"]
POD = [*KUBE, "-n", "heartwood-sessions"]


async def run() -> int:
    image = subprocess.check_output(
        [
            *KUBE,
            "-n",
            "heartwood-platform",
            "get",
            "sts/heartwood-control",
            "-o",
            "jsonpath={.spec.template.spec.containers[?(@.name=='control')].image}",
        ],
        text=True,
    ).strip()
    name = "browser-route-" + uuid.uuid4().hex[:12]
    spec = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name},
        "spec": {
            "automountServiceAccountToken": False,
            "restartPolicy": "Never",
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": 65532,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "containers": [
                {
                    "name": "route",
                    "image": image,
                    "command": ["/opt/heartwood-platform/.venv/bin/python", "-I", "-c", RELAY],
                    "readinessProbe": {
                        "exec": {
                            "command": [
                                "/opt/heartwood-platform/.venv/bin/python",
                                "-I",
                                "-c",
                                "import socket; "
                                "socket.create_connection(('127.0.0.1', 8888), 1).close()",
                            ]
                        },
                        "periodSeconds": 5,
                    },
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "readOnlyRootFilesystem": True,
                        "capabilities": {"drop": ["ALL"]},
                    },
                }
            ],
        },
    }
    subprocess.run([*POD, "create", "-f", "-"], input=json.dumps(spec), text=True, check=True)
    forward = None
    try:
        subprocess.run(
            [*POD, "wait", "--for=condition=Ready", "pod/" + name, "--timeout=120s"], check=True
        )
        forward = await asyncio.create_subprocess_exec(
            *POD,
            "port-forward",
            "--address=127.0.0.1",
            "pod/" + name,
            ":8888",
            stdout=asyncio.subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        stdout = forward.stdout
        if stdout is None:
            raise RuntimeError("port-forward stdout pipe is unavailable")
        line = await asyncio.wait_for(stdout.readline(), 20)
        port = re.fullmatch(rb"Forwarding from 127\.0\.0\.1:(\d+) -> 8888\s*", line)
        if not port:
            raise RuntimeError("loopback port-forward failed")
        env = dict(os.environ, HEARTWOOD_BROWSER_PROXY="http://127.0.0.1:" + port[1].decode())
        launcher = os.environ.get("HEARTWOOD_BROWSER_LAUNCHER")
        command = (
            ["bash", launcher]
            if launcher
            else ["node", str(Path(__file__).with_name("hosted-project-origins.cjs")), "--run"]
        )
        browser = await asyncio.create_subprocess_exec(*command, env=env)
        # kubectl writes one line for each connection. Drain its pipe for long runs.
        try:

            async def drain() -> None:
                while await stdout.read(4096):
                    pass

            output = asyncio.create_task(drain())
            return await browser.wait()
        finally:
            output.cancel()
            if browser.returncode is None:
                browser.terminate()
                await browser.wait()
    finally:
        if forward and forward.returncode is None:
            forward.terminate()
            await forward.wait()
        subprocess.run([*POD, "delete", "pod/" + name, "--timeout=45s"], check=True)


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
