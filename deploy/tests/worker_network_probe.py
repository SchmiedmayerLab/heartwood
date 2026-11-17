# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Probe denied worker egress against independently reachable synthetic canaries.

The outside test harness must first reach each supplied canary over the tested
address family and transport, then verify that this probe sends it no traffic.
Host services are the deployment's real endpoints, which the harness reaches from
outside; cloud metadata addresses are reached from outside only where a listener
exists. This script alone cannot establish any of those outside observations.
Every probed route passes only on an explicit denial.
"""

from __future__ import annotations

import argparse
import errno
import ipaddress
import json
import re
import socket

# The sandbox proxy refuses a request with one of these JSON error codes; any other
# response, including a 403 from the destination itself, means the request got through.
_PROXY_DENIALS = frozenset({"policy_denied", "ssrf_denied"})
_SERVICE = re.compile(r"([a-z0-9]+(?:-[a-z0-9]+)*)=([A-Za-z0-9.-]+):([0-9]{1,5})")


def service_targets(values: list[str]) -> dict[str, tuple[str, int]]:
    """Parse each host-service endpoint, named for the service and the route to it."""
    targets: dict[str, tuple[str, int]] = {}
    for value in values:
        match = _SERVICE.fullmatch(value)
        if match is None or match[1] in targets or not 1 <= int(match[3]) <= 65535:
            raise ValueError("Each service must be a unique NAME=ADDRESS:PORT")
        targets[match[1]] = (match[2], int(match[3]))
    return targets


def proxy_denial(response: bytes) -> bool:
    """Return whether a response is the sandbox proxy's own refusal."""
    head, separator, body = response.partition(b"\r\n\r\n")
    if not separator or not head.startswith(b"HTTP/1.1 403 "):
        return False
    try:
        error = json.loads(body).get("error")
    except (ValueError, AttributeError):
        return False
    return error in _PROXY_DENIALS


def denied_connection(
    address: str,
    port: int,
    family: socket.AddressFamily,
    transport: socket.SocketKind,
    hostname: str,
    *,
    method: str = "GET /canary",
) -> str:
    """Require an explicit denial, rather than a timeout or an unavailable route."""
    request = (f"{method} HTTP/1.1\r\nHost: {hostname}\r\nConnection: close\r\n\r\n").encode(
        "ascii"
    )
    try:
        with socket.socket(family, transport) as connection:
            connection.settimeout(5)
            connection.connect((address, port))
            connection.sendall(request)
            response = b""
            while len(response) <= 16384 and (chunk := connection.recv(4096)):
                response += chunk
                if transport != socket.SOCK_STREAM:
                    break
            if transport == socket.SOCK_STREAM and proxy_denial(response):
                return "policy-403"
            raise AssertionError("The attempted connection did not receive a policy denial")
    except OSError as error:
        if error.errno not in (errno.EACCES, errno.EPERM):
            raise AssertionError(
                "The attempted connection failed without a policy denial"
            ) from error
        return errno.errorcode[error.errno]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ipv4", type=ipaddress.IPv4Address, required=True)
    parser.add_argument("--ipv6", type=ipaddress.IPv6Address, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--ipv6-port", type=int, required=True)
    parser.add_argument("--allowed-host", default="host.docker.internal")
    parser.add_argument(
        "--service",
        action="append",
        default=[],
        metavar="NAME=ADDRESS:PORT",
        help="A real endpoint of a service on the worker's host, by one route to it",
    )
    parser.add_argument("--proxy-port", type=int, help="An outside forward proxy on that address")
    args = parser.parse_args()
    if args.ipv6.ipv4_mapped is not None:
        parser.error("The native IPv6 canary must not be an IPv4-mapped address")
    try:
        services = service_targets(args.service)
    except ValueError as error:
        parser.error(str(error))
    ports: tuple[int, ...] = (args.port, args.ipv6_port)
    if args.proxy_port is not None:
        ports += (args.proxy_port,)
    if any(not 1 <= port <= 65535 for port in ports):
        parser.error("Canary ports must be between 1 and 65535")
    if not args.allowed_host or any(char in args.allowed_host for char in "\r\n"):
        parser.error("The allowed hostname must be a nonempty single line")

    allowed = socket.getaddrinfo(args.allowed_host, args.port, socket.AF_INET, socket.SOCK_STREAM)
    if not allowed:
        raise AssertionError("The permitted hostname did not resolve")
    # OpenShell can return an observation-only synthetic address for an unknown
    # name. Resolution does not grant access; the subsequent connection must fail.
    blocked_host = "unlisted.qualification.invalid"
    answers = socket.getaddrinfo(blocked_host, 80, socket.AF_INET, socket.SOCK_STREAM)
    synthetic_pool = ipaddress.ip_network("198.18.0.0/15")
    if not answers or not all(ipaddress.ip_address(row[4][0]) in synthetic_pool for row in answers):
        raise AssertionError("The unlisted hostname did not receive only synthetic addresses")

    checks: dict[str, str | bool] = {"allowed-dns": True, "unlisted-dns-synthetic": True}
    cases = (
        (
            "unknown-host-tcp",
            str(answers[0][4][0]),
            80,
            socket.AF_INET,
            socket.SOCK_STREAM,
            blocked_host,
        ),
        (
            "ipv4-tcp",
            str(args.ipv4),
            args.port,
            socket.AF_INET,
            socket.SOCK_STREAM,
            args.allowed_host,
        ),
        (
            "mapped-ipv6-tcp",
            f"::ffff:{args.ipv4}",
            args.port,
            socket.AF_INET6,
            socket.SOCK_STREAM,
            args.allowed_host,
        ),
        (
            "direct-udp",
            str(args.ipv4),
            args.port,
            socket.AF_INET,
            socket.SOCK_DGRAM,
            args.allowed_host,
        ),
        (
            "native-ipv6-tcp",
            str(args.ipv6),
            args.ipv6_port,
            socket.AF_INET6,
            socket.SOCK_STREAM,
            args.allowed_host,
        ),
        (
            "native-ipv6-udp",
            str(args.ipv6),
            args.ipv6_port,
            socket.AF_INET6,
            socket.SOCK_DGRAM,
            args.allowed_host,
        ),
    )
    for name, address, port, family, transport, hostname in cases:
        checks[name] = denied_connection(address, port, family, transport, hostname)
    for name, (address, port) in services.items():
        checks[f"{name}-tcp"] = denied_connection(
            address, port, socket.AF_INET, socket.SOCK_STREAM, address
        )
    for name, address, family in (
        ("metadata-ipv4-tcp", "169.254.169.254", socket.AF_INET),
        ("metadata-ipv6-tcp", "fd00:ec2::254", socket.AF_INET6),
    ):
        checks[name] = denied_connection(
            address, 80, family, socket.SOCK_STREAM, "metadata.qualification.invalid"
        )
    if args.proxy_port is not None:
        # A tunnel request through an outside proxy names the approved destination.
        target = f"{args.allowed_host}:443"
        checks["alternate-proxy-tcp"] = denied_connection(
            str(args.ipv4),
            args.proxy_port,
            socket.AF_INET,
            socket.SOCK_STREAM,
            target,
            method=f"CONNECT {target}",
        )
    print(json.dumps(checks))


if __name__ == "__main__":
    main()
