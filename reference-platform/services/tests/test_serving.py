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
from pathlib import Path

import pytest
from heartwood_platform.common.serving import CertificateReloader, TlsSettings


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
    first = _served(context)
    settings.key_file.unlink()

    assert reloader.reload() is False
    assert _served(context) == first


def test_the_check_runs_on_the_event_loop(
    mounted: tuple[TlsSettings, ssl.SSLContext], tmp_path: Path
) -> None:
    settings, context = mounted
    reloader = CertificateReloader(context, settings)
    certificate, key = _certificate(tmp_path, "second")
    shutil.copy(certificate, settings.certificate_file)
    shutil.copy(key, settings.key_file)

    async def watch_briefly() -> None:
        watching = asyncio.create_task(reloader.watch(0.01))
        await asyncio.sleep(0.2)
        watching.cancel()

    asyncio.run(watch_briefly())
    assert _served(context) == _der(certificate)
