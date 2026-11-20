# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Test certificate authorities and HTTPS servers on the loopback interface."""

from __future__ import annotations

import http.server
import socket
import ssl
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import uvicorn
from starlette.types import ASGIApp


def certificate_authority(directory: Path, name: str) -> tuple[Path, Path]:
    """Create a test certificate authority and return its certificate and key."""
    certificate, key = directory / f"{name}.crt", directory / f"{name}.key"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
            "-nodes", "-days", "1", "-subj", f"/CN={name}",
            "-addext", "basicConstraints=critical,CA:TRUE",
            "-addext", "keyUsage=critical,keyCertSign",
            "-keyout", str(key), "-out", str(certificate),
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip
    return certificate, key


def server_certificate(directory: Path, authority: tuple[Path, Path]) -> tuple[Path, Path]:
    """Issue a certificate for localhost from ``authority``; return certificate and key."""
    directory.mkdir(parents=True, exist_ok=True)
    key, request, certificate = (directory / name for name in ("tls.key", "tls.csr", "tls.crt"))
    subprocess.run(
        [
            "openssl", "req", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
            "-nodes", "-subj", "/CN=localhost", "-keyout", str(key), "-out", str(request),
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip
    extensions = directory / "tls.ext"
    extensions.write_text("subjectAltName=DNS:localhost\n", encoding="utf-8")
    subprocess.run(
        [
            "openssl", "x509", "-req", "-in", str(request), "-CA", str(authority[0]),
            "-CAkey", str(authority[1]), "-days", "1", "-extfile", str(extensions),
            "-out", str(certificate),
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip
    return certificate, key


@contextmanager
def https_server(directory: Path, authority: tuple[Path, Path]) -> Iterator[int]:
    """Answer 200 over HTTPS on 127.0.0.1 with a localhost certificate; yield the port."""
    certificate, key = server_certificate(directory, authority)

    class Answer(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *_args: object) -> None:
            pass

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, key)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Answer)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


@contextmanager
def serve_tls(app: ASGIApp, directory: Path, authority: tuple[Path, Path]) -> Iterator[str]:
    """Serve ``app`` over HTTPS with a localhost certificate; yield its origin."""
    certificate, key = server_certificate(directory, authority)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        config = uvicorn.Config(
            app,
            log_level="error",
            access_log=False,
            ssl_certfile=certificate,
            ssl_keyfile=key,
        )
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while not server.started and time.monotonic() < deadline:
                time.sleep(0.01)
            if not server.started:
                raise RuntimeError("the test service did not start")
            yield f"https://localhost:{listener.getsockname()[1]}"
        finally:
            server.should_exit = True
            thread.join(5)
