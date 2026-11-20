# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""HTTPS serving of a platform service.

A service serves only HTTPS, with a certificate that the platform CA signed for the
names of its Service. The certificate and key files come from a mounted Secret, which
Kubernetes replaces as a whole; the service reloads them when their content changes,
so a replaced certificate takes effect for new connections without a restart.
"""

from __future__ import annotations

import asyncio
import logging
import ssl
import tempfile
from pathlib import Path

import uvicorn
from pydantic import BaseModel, ConfigDict
from starlette.types import ASGIApp

PORT = 8443
RELOAD_SECONDS = 60.0

_LOGGER = logging.getLogger(__name__)


class TlsSettings(BaseModel):
    """The serving certificate and its key, as PEM files."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    certificate_file: Path
    key_file: Path


class CertificateReloader:
    """Load changed certificate files into the serving context.

    New connections use the new certificate; established connections keep theirs. A pair
    that does not load, such as a certificate with another key, leaves the previous one
    in use and is tried again at the next check. The context keeps one certificate for
    each key type, so a replacement keeps the key type of the certificate it replaces.
    The private copy of each pair is written to the temporary directory, which the chart
    keeps in memory.
    """

    def __init__(self, context: ssl.SSLContext, settings: TlsSettings) -> None:
        """Serve with ``context`` and load it from the files of ``settings``.

        Nothing counts as loaded until the first :meth:`reload`, so the context then serves
        exactly the bytes that the reloader compares later.
        """
        self._context = context
        self._settings = settings
        self._loaded = (b"", b"")

    def _content(self) -> tuple[bytes, bytes]:
        return self._settings.certificate_file.read_bytes(), self._settings.key_file.read_bytes()

    def reload(self) -> bool:
        """Load the files if they changed; return whether a new certificate is in use."""
        try:
            content = self._content()
            if content == self._loaded:
                return False
            # Both loads read the same bytes from a private copy. A pair that fails to load
            # can leave a context without a usable certificate, so it is checked in a
            # separate context first.
            with tempfile.TemporaryDirectory() as directory:
                certificate, key = Path(directory, "tls.crt"), Path(directory, "tls.key")
                certificate.write_bytes(content[0])
                key.write_bytes(content[1])
                ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(certificate, key)
                self._context.load_cert_chain(certificate, key)
        except (OSError, ssl.SSLError) as error:
            _LOGGER.warning("the serving certificate was not reloaded: %s", type(error).__name__)
            return False
        self._loaded = content
        _LOGGER.info("the serving certificate was reloaded")
        return True

    async def watch(self, interval: float = RELOAD_SECONDS) -> None:
        """Check the files every ``interval`` seconds.

        The check runs on the server's event loop, which also performs the TLS
        handshakes, so no handshake sees a certificate without its key.
        """
        while True:
            await asyncio.sleep(interval)
            self.reload()


def _configure(
    application: ASGIApp, tls: TlsSettings, *, host: str, port: int
) -> tuple[uvicorn.Config, CertificateReloader]:
    config = uvicorn.Config(
        application,
        host=host,
        port=port,
        log_config=None,
        ssl_certfile=tls.certificate_file,
        ssl_keyfile=tls.key_file,
    )
    config.load()
    if config.ssl is None:
        raise RuntimeError("the server has no TLS context")
    config.ssl.minimum_version = ssl.TLSVersion.TLSv1_2
    reloader = CertificateReloader(config.ssl, tls)
    if not reloader.reload():
        raise RuntimeError("the serving certificate does not load")
    return config, reloader


async def _serve(server: uvicorn.Server, reloader: CertificateReloader, interval: float) -> None:
    watching = asyncio.create_task(reloader.watch(interval))
    try:
        await server.serve()
    finally:
        watching.cancel()


def serve(application: ASGIApp, tls: TlsSettings, *, port: int = PORT) -> None:
    """Serve ``application`` over HTTPS only, reloading the certificate when it changes."""
    config, reloader = _configure(application, tls, host="0.0.0.0", port=port)
    asyncio.run(_serve(uvicorn.Server(config), reloader, RELOAD_SECONDS))
