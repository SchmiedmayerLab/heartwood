# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Credential delivery and port ownership at the gateway launch boundary."""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest
import uvicorn

from heartwood.cli import main
from heartwood.gateway import GatewayAsgiApp

_SECRET = "synthetic-launch-capability-" * 2


def _arguments(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, port: int) -> list[str]:
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<main>Synthetic</main>")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HEARTWOOD_PLATFORM", "generic")
    monkeypatch.delenv("HEARTWOOD_GATEWAY_CAPABILITY", raising=False)
    return ["gateway", "serve", "--web-root", str(web), "--port", str(port)]


def test_occupied_port_never_discloses_launch_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with socket.create_server(("127.0.0.1", 0)) as occupied:
        args = _arguments(tmp_path, monkeypatch, occupied.getsockname()[1])
        with pytest.raises(SystemExit) as error:
            main(args)
        assert error.value.code != 0
    assert "#launch=" not in capsys.readouterr().out


@pytest.mark.parametrize("supplied", [False, True])
def test_gateway_owns_listener_and_consumes_descriptor_before_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    supplied: bool,
) -> None:
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    args = _arguments(tmp_path, monkeypatch, port)
    descriptor: int | None = None
    if supplied:
        source = tmp_path / "synthetic-secret"
        source.write_text(_SECRET)
        descriptor = os.open(source, os.O_RDONLY)
        args.extend(["--capability-fd", str(descriptor)])
    observed = []

    def print_link(message: str, *, flush: bool = False) -> None:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            pass
        print(message, flush=flush)

    monkeypatch.setattr("heartwood.cli.print", print_link, raising=False)

    def run(server: uvicorn.Server, sockets: list[socket.socket] | None = None) -> None:
        assert sockets == [listener]
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            pass
        assert not listener.get_inheritable()
        assert not server.config.proxy_headers
        assert not server.config.access_log
        with socket.socket() as competitor:
            competitor.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            with pytest.raises(OSError, match="Address already in use"):
                competitor.bind(("127.0.0.1", port))
        app = server.config.app
        assert isinstance(app, GatewayAsgiApp)
        printed = capsys.readouterr().out
        assert ("#launch=" in printed) is not supplied
        assert _SECRET not in printed
        if descriptor is not None:
            with pytest.raises(OSError, match="Bad file descriptor"):
                os.fstat(descriptor)
            assert app.access.principal({"x-heartwood-capability": (_SECRET,)}) == "human"
        observed.append(True)

    monkeypatch.setattr(uvicorn.Config, "bind_socket", lambda _config: listener)
    monkeypatch.setattr(uvicorn.Server, "run", run)
    assert main(args) == 0
    assert observed == [True]
    assert listener.fileno() == -1


def test_environment_delivery_fails_without_printing_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    args = _arguments(tmp_path, monkeypatch, 8767)
    monkeypatch.setenv("HEARTWOOD_GATEWAY_CAPABILITY", _SECRET)
    with pytest.raises(SystemExit, match="use --capability-fd") as error:
        main(args)
    assert _SECRET not in str(error.value)
    assert "#launch=" not in capsys.readouterr().out


def _certificate_authority(directory: Path) -> tuple[Path, Path, Path]:
    """Write a synthetic CA and a serving certificate for 127.0.0.1 and localhost."""
    import ipaddress
    from datetime import UTC, datetime, timedelta

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    now = datetime.now(UTC)
    authority_key = ec.generate_private_key(ec.SECP256R1())
    authority_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Synthetic CA")])
    authority = (
        x509.CertificateBuilder()
        .subject_name(authority_name)
        .issuer_name(authority_name)
        .public_key(authority_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(authority_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    serving = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
        .issuer_name(authority_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .sign(authority_key, hashes.SHA256())
    )
    paths = directory / "ca.crt", directory / "tls.crt", directory / "tls.key"
    paths[0].write_bytes(authority.public_bytes(serialization.Encoding.PEM))
    paths[1].write_bytes(serving.public_bytes(serialization.Encoding.PEM))
    paths[2].write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return paths


def test_gateway_serves_only_tls_with_the_given_certificate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ssl
    import threading

    authority, certificate, key = _certificate_authority(tmp_path)
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    args = _arguments(tmp_path, monkeypatch, port)
    args += ["--tls-certificate", str(certificate), "--tls-key", str(key)]
    args += ["--ingress-mode", "trusted-proxy", "--public-origin", "https://localhost"]
    args += ["--trusted-proxy-source", "127.0.0.1/32"]
    serve = uvicorn.Server.run
    answers: dict[str, bytes] = {}

    def run(server: uvicorn.Server, sockets: list[socket.socket] | None = None) -> None:
        thread = threading.Thread(target=serve, args=(server,), kwargs={"sockets": sockets})
        thread.start()
        try:
            for _ in range(100):
                if server.started:
                    break
                threading.Event().wait(0.05)
            context = ssl.create_default_context(cafile=authority)
            with (
                socket.create_connection(("127.0.0.1", port), timeout=5) as raw,
                context.wrap_socket(raw, server_hostname="localhost") as secure,
            ):
                secure.sendall(
                    b"GET /healthz HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
                )
                answers["tls"] = secure.recv(64)
            # A plain HTTP request receives no HTTP answer.
            with socket.create_connection(("127.0.0.1", port), timeout=5) as plain:
                plain.sendall(b"GET /healthz HTTP/1.1\r\nHost: localhost\r\n\r\n")
                try:
                    answers["plain"] = plain.recv(64)
                except ConnectionResetError:
                    answers["plain"] = b""
        finally:
            server.should_exit = True
            thread.join(timeout=10)

    monkeypatch.setattr(uvicorn.Config, "bind_socket", lambda _config: listener)
    monkeypatch.setattr(uvicorn.Server, "run", run)
    assert main(args) == 0
    assert answers["tls"].startswith(b"HTTP/1.1 ")
    assert not answers["plain"].startswith(b"HTTP/")


@pytest.mark.parametrize(
    "extra",
    [
        ["--tls-certificate", "tls.crt"],
        ["--tls-key", "tls.key"],
        # A launch link of direct loopback ingress names an http origin.
        ["--tls-certificate", "tls.crt", "--tls-key", "tls.key"],
        [
            "--tls-certificate",
            "tls.crt",
            "--tls-key",
            "tls.key",
            "--public-origin",
            "http://research.example.test",
        ],
    ],
)
def test_tls_requires_its_key_and_an_https_origin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    extra: list[str],
) -> None:
    args = _arguments(tmp_path, monkeypatch, 8767)
    with pytest.raises(SystemExit) as error:
        main([*args, *extra])
    assert error.value.code == 2
    assert "#launch=" not in capsys.readouterr().out
