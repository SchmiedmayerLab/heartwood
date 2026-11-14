# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Run the network probe with reachable outside canaries and native sandbox identity.

Run on a trusted Linux controller reachable from the worker's host. The supplied
addresses must route to this controller, including any published port mappings.
Use a disposable synthetic sandbox; this command does not change its policy.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import queue
import select
import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openshell import SandboxClient


def serve_canary(
    listener: socket.socket,
    stopping: threading.Event,
    observations: queue.SimpleQueue[bytes],
    errors: queue.SimpleQueue[Exception],
) -> None:
    try:
        while not stopping.is_set():
            if not select.select([listener], [], [], 0.1)[0]:
                continue
            if listener.type == socket.SOCK_STREAM:
                connection, _ = listener.accept()
                with connection:
                    connection.settimeout(5)
                    payload = connection.recv(1024)
                    observations.put(payload)
                    connection.sendall(b"ACK")
            else:
                payload, address = listener.recvfrom(1024)
                observations.put(payload)
                listener.sendto(b"ACK", address)
    except Exception as error:
        errors.put(error)


@contextmanager
def outside_canaries(port: int, ipv6_port: int) -> Iterator[queue.SimpleQueue[bytes]]:
    observations: queue.SimpleQueue[bytes] = queue.SimpleQueue()
    errors: queue.SimpleQueue[Exception] = queue.SimpleQueue()
    stopping = threading.Event()
    listeners: list[socket.socket] = []
    threads: list[threading.Thread] = []
    try:
        for family, host, local_port in (
            (socket.AF_INET, "0.0.0.0", port),
            (socket.AF_INET6, "::", ipv6_port),
        ):
            for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM):
                listener = socket.socket(family, kind)
                listeners.append(listener)
                if family == socket.AF_INET6:
                    listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                listener.bind((host, local_port))
                if kind == socket.SOCK_STREAM:
                    listener.listen()
                thread = threading.Thread(
                    target=serve_canary, args=(listener, stopping, observations, errors)
                )
                thread.start()
                threads.append(thread)
        yield observations
    finally:
        stopping.set()
        for thread in threads:
            thread.join(timeout=6)
        for listener in listeners:
            listener.close()
        if any(thread.is_alive() for thread in threads):
            raise RuntimeError("Canary listener did not settle")
        if not errors.empty():
            raise RuntimeError("Canary listener failed") from errors.get_nowait()


def positive_controls(
    ipv4: ipaddress.IPv4Address,
    ipv6: ipaddress.IPv6Address,
    port: int,
    ipv6_port: int,
    observations: queue.SimpleQueue[bytes],
) -> None:
    token = b"synthetic-outside-positive-control"
    for family, address, target_port, kind in (
        (socket.AF_INET, str(ipv4), port, socket.SOCK_STREAM),
        (socket.AF_INET6, f"::ffff:{ipv4}", port, socket.SOCK_STREAM),
        (socket.AF_INET, str(ipv4), port, socket.SOCK_DGRAM),
        (socket.AF_INET6, str(ipv6), ipv6_port, socket.SOCK_STREAM),
        (socket.AF_INET6, str(ipv6), ipv6_port, socket.SOCK_DGRAM),
    ):
        with socket.socket(family, kind) as connection:
            connection.settimeout(5)
            connection.connect((address, target_port))
            connection.sendall(token)
            if connection.recv(16) != b"ACK":
                raise AssertionError("Outside canary did not acknowledge the positive control")
        if observations.get(timeout=5) != token:
            raise AssertionError("Unexpected traffic during the outside positive control")
    if not observations.empty():
        raise AssertionError("Unexpected additional traffic during the outside positive controls")


def qualify_network(
    control: SandboxClient,
    sandbox: str,
    *,
    workspace: str,
    ipv4: ipaddress.IPv4Address,
    ipv6: ipaddress.IPv6Address,
    port: int,
    ipv6_port: int,
    allowed_host: str = "host.docker.internal",
    python: str = "/opt/heartwood/.venv/bin/python",
) -> dict[str, object]:
    if ipv6.ipv4_mapped is not None:
        raise ValueError("The native IPv6 canary must not be an IPv4-mapped address")
    before = control.get(sandbox, workspace=workspace)
    admission = before.status.configuration_admission
    if admission is None:
        raise AssertionError("The sandbox does not have an active admitted configuration")
    from openshell._proto import openshell_pb2

    if (
        admission.state != openshell_pb2.CONFIGURATION_ADMISSION_STATE_ACCEPTED
        or not before.status.configuration_activated
        or not before.status.main_process_instance_id
    ):
        raise AssertionError("The sandbox does not have an active admitted configuration")
    program = Path(__file__).with_name("worker_network_probe.py").read_text()
    with outside_canaries(port, ipv6_port) as observations:
        positive_controls(ipv4, ipv6, port, ipv6_port, observations)
        result = control.exec(
            sandbox,
            [
                python,
                "-I",
                "-B",
                "-c",
                program,
                "--ipv4",
                str(ipv4),
                "--ipv6",
                str(ipv6),
                "--port",
                str(port),
                "--ipv6-port",
                str(ipv6_port),
                "--allowed-host",
                allowed_host,
            ],
            workspace=workspace,
            timeout_seconds=45,
            no_login_shell=True,
        )
        if result.exit_code != 0:
            raise AssertionError("Worker network probe failed; a denial was not established")
        checks = json.loads(result.stdout)
        after = control.get(sandbox, workspace=workspace)
        if (
            before.id != after.id
            or before.status.main_process_instance_id != after.status.main_process_instance_id
            or admission != after.status.configuration_admission
            or not after.status.configuration_activated
        ):
            raise AssertionError(
                "Sandbox identity or admitted configuration changed during the probe"
            )
        # A second round proves the destinations stayed reachable throughout the
        # probe and detects any worker traffic queued before these controls.
        positive_controls(ipv4, ipv6, port, ipv6_port, observations)
    if not observations.empty():
        raise AssertionError("The worker reached an outside canary")
    return {
        "drill_id": "dns-and-direct-egress.v1",
        "sandbox_id": before.id,
        "generation": before.status.main_process_instance_id,
        "policy_sha256": admission.policy_hash,
        "network_probe_sha256": hashlib.sha256(program.encode()).hexdigest(),
        "network_checks": checks,
        "outside_positive_controls": 10,
        "forbidden_canary_requests": 0,
        "passed": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway", required=True)
    parser.add_argument("--ca", type=Path, required=True)
    parser.add_argument("--cert", type=Path, required=True)
    parser.add_argument("--key", type=Path, required=True)
    parser.add_argument("--sandbox", required=True)
    parser.add_argument("--workspace", default="default")
    parser.add_argument("--ipv4", type=ipaddress.IPv4Address, required=True)
    parser.add_argument("--ipv6", type=ipaddress.IPv6Address, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--ipv6-port", type=int, required=True)
    parser.add_argument("--allowed-host", default="host.docker.internal")
    args = parser.parse_args()
    from openshell import SandboxClient, TlsConfig

    control = SandboxClient(
        args.gateway,
        tls=TlsConfig(ca_path=args.ca, cert_path=args.cert, key_path=args.key),
        timeout=15,
    )
    try:
        record = qualify_network(
            control,
            args.sandbox,
            workspace=args.workspace,
            ipv4=args.ipv4,
            ipv6=args.ipv6,
            port=args.port,
            ipv6_port=args.ipv6_port,
            allowed_host=args.allowed_host,
        )
        print(json.dumps(record))
    finally:
        control.close()


if __name__ == "__main__":
    main()
