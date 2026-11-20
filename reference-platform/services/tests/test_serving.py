# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Reloading the serving certificate of a platform service."""

from __future__ import annotations

import asyncio
import shutil
import socket
import ssl
import subprocess
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from heartwood_platform.common.serving import CertificateReloader, TlsSettings, _configure, _serve
from starlette.types import Receive, Scope, Send


def _certificate(directory: Path, name: str) -> tuple[Path, Path]:
    certificate, key = directory / f"{name}.crt", directory / f"{name}.key"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
            "-nodes", "-days", "1", "-subj", f"/CN={name}",
            "-keyout", str(key), "-out", str(certificate),
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip
    return certificate, key


def _served(context: ssl.SSLContext) -> bytes:
    """The certificate that a client receives from a server with ``context``."""
    server, client = socket.socketpair()
    accepted: list[ssl.SSLSocket] = []

    def accept() -> None:
        accepted.append(context.wrap_socket(server, server_side=True))

    thread = threading.Thread(target=accept)
    thread.start()
    verifying = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    verifying.check_hostname = False
    verifying.verify_mode = ssl.CERT_NONE
    with verifying.wrap_socket(client) as connection:
        served = connection.getpeercert(binary_form=True)
    thread.join()
    accepted[0].close()
    assert served is not None
    return served


def _der(certificate: Path) -> bytes:
    return ssl.PEM_cert_to_DER_cert(certificate.read_text(encoding="utf-8"))


@pytest.fixture
def mounted(tmp_path: Path) -> tuple[TlsSettings, ssl.SSLContext]:
    certificate, key = _certificate(tmp_path, "first")
    settings = TlsSettings(certificate_file=tmp_path / "tls.crt", key_file=tmp_path / "tls.key")
    shutil.copy(certificate, settings.certificate_file)
    shutil.copy(key, settings.key_file)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(settings.certificate_file, settings.key_file)
    return settings, context


def test_a_replaced_certificate_is_served_to_new_connections(
    mounted: tuple[TlsSettings, ssl.SSLContext], tmp_path: Path
) -> None:
    settings, context = mounted
    reloader = CertificateReloader(context, settings)
    assert reloader.reload() is True
    first = _served(context)
    assert reloader.reload() is False

    certificate, key = _certificate(tmp_path, "second")
    shutil.copy(certificate, settings.certificate_file)
    shutil.copy(key, settings.key_file)

    assert reloader.reload() is True
    assert _served(context) == _der(certificate) != first
    assert reloader.reload() is False


def test_a_certificate_with_another_key_keeps_the_previous_one(
    mounted: tuple[TlsSettings, ssl.SSLContext], tmp_path: Path
) -> None:
    settings, context = mounted
    reloader = CertificateReloader(context, settings)
    assert reloader.reload() is True
    first = _served(context)
    certificate, key = _certificate(tmp_path, "second")
    shutil.copy(certificate, settings.certificate_file)

    assert reloader.reload() is False
    assert _served(context) == first

    # The pair loads once the matching key arrives.
    shutil.copy(key, settings.key_file)
    assert reloader.reload() is True
    assert _served(context) == _der(certificate)


def test_missing_files_keep_the_previous_certificate(
    mounted: tuple[TlsSettings, ssl.SSLContext],
) -> None:
    settings, context = mounted
    reloader = CertificateReloader(context, settings)
    assert reloader.reload() is True
    first = _served(context)
    settings.key_file.unlink()

    assert reloader.reload() is False
    assert _served(context) == first


def test_the_watch_reloads_changed_files(
    mounted: tuple[TlsSettings, ssl.SSLContext], tmp_path: Path
) -> None:
    settings, context = mounted
    reloader = CertificateReloader(context, settings)
    assert reloader.reload() is True
    certificate, key = _certificate(tmp_path, "second")
    shutil.copy(certificate, settings.certificate_file)
    shutil.copy(key, settings.key_file)

    async def watch_briefly() -> None:
        watching = asyncio.create_task(reloader.watch(0.01))
        await asyncio.sleep(0.2)
        watching.cancel()

    asyncio.run(watch_briefly())
    assert _served(context) == _der(certificate)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


async def _answer(scope: Scope, _receive: Receive, send: Send) -> None:
    if scope["type"] == "http":
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"served"})


def _client() -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def _certificate_at(port: int) -> bytes:
    with (
        socket.create_connection(("127.0.0.1", port), timeout=5) as raw,
        _client().wrap_socket(raw) as connection,
    ):
        served = connection.getpeercert(binary_form=True)
    assert served is not None
    return served


def test_the_server_serves_tls_1_2_or_later_only_and_reloads_its_certificate(
    mounted: tuple[TlsSettings, ssl.SSLContext], tmp_path: Path
) -> None:
    settings, _ = mounted
    port = _free_port()
    config, reloader = _configure(_answer, settings, host="127.0.0.1", port=port)
    assert config.ssl is not None
    # The context that the server uses refuses protocol versions before TLS 1.2.
    assert config.ssl.minimum_version == ssl.TLSVersion.TLSv1_2
    server = uvicorn.Server(config)
    thread = threading.Thread(target=asyncio.run, args=(_serve(server, reloader, 0.05),))
    thread.start()
    try:
        for _ in range(200):
            if server.started:
                break
            time.sleep(0.05)
        assert server.started
        assert _certificate_at(port) == _der(settings.certificate_file)

        # Plain HTTP gets no HTTP answer.
        with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
            raw.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
            try:
                answer = raw.recv(64)
            except ConnectionResetError:
                answer = b""
        assert not answer.startswith(b"HTTP/")

        certificate, key = _certificate(tmp_path, "second")
        shutil.copy(certificate, settings.certificate_file)
        shutil.copy(key, settings.key_file)
        for _ in range(200):
            if _certificate_at(port) == _der(certificate):
                break
            time.sleep(0.05)
        assert _certificate_at(port) == _der(certificate)
    finally:
        server.should_exit = True
        thread.join(timeout=10)
    assert not thread.is_alive()
