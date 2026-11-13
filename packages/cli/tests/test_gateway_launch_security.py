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
            assert app.access.authorizes({"x-heartwood-capability": (_SECRET,)})
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
