# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Probe denied worker egress against independently reachable synthetic canaries.

The outside test harness must first reach each supplied canary over the tested
address family and transport, then verify that this probe sends it no traffic.
This script alone cannot establish either of those outside observations.
"""

from __future__ import annotations

import argparse
import errno
import ipaddress
import json
import socket


def denied_connection(
    address: str,
    port: int,
    family: socket.AddressFamily,
    transport: socket.SocketKind,
    hostname: str,
) -> str:
    """Require an explicit denial, rather than a timeout or an unavailable route."""
    request = (f"GET /canary HTTP/1.1\r\nHost: {hostname}\r\nConnection: close\r\n\r\n").encode(
        "ascii"
    )
    try:
        with socket.socket(family, transport) as connection:
            connection.settimeout(5)
            connection.connect((address, port))
            connection.sendall(request)
            response = connection.recv(1024)
            if transport == socket.SOCK_STREAM and response.startswith(b"HTTP/1.1 403 "):
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
    args = parser.parse_args()
    if args.ipv6.ipv4_mapped is not None:
        parser.error("The native IPv6 canary must not be an IPv4-mapped address")
    if any(not 1 <= port <= 65535 for port in (args.port, args.ipv6_port)):
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
    print(json.dumps(checks))


if __name__ == "__main__":
    main()
