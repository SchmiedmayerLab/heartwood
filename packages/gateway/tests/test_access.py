# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Tests for the gateway launch capability."""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest

from heartwood.gateway import IngressConfigurationError, IngressPolicy, LaunchCapability

_SECRET = "operator" * 5


def test_generated_capability_issues_one_launch_link() -> None:
    capability = LaunchCapability.generate()
    link = capability.launch_url(IngressPolicy.create())

    assert link is not None
    assert link.startswith("http://127.0.0.1:8767/#launch=")
    token = link.rsplit("=", 1)[1]
    assert not capability.exchange_launch_token("wrong")
    assert not capability.exchange_launch_token("\u00e9")
    assert capability.exchange_launch_token(token)
    assert not capability.exchange_launch_token(token)
    assert capability.launch_url(IngressPolicy.create()) is None


@pytest.mark.parametrize("pipe", [False, True])
def test_descriptor_capability_is_consumed_and_prints_no_link(tmp_path: Path, pipe: bool) -> None:
    payload = (_SECRET + "\n").encode()
    if pipe:
        descriptor, writer = os.pipe()
        os.write(writer, payload)
        os.close(writer)
    else:
        path = tmp_path / "secret"
        path.write_bytes(payload)
        descriptor = os.open(path, os.O_RDONLY)
    os.set_inheritable(descriptor, True)

    capability = LaunchCapability.from_descriptor(descriptor)

    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(descriptor)
    assert capability.launch_url(IngressPolicy.create()) is None
    assert capability.authorizes({"x-heartwood-capability": (f" {_SECRET} ",)})


@pytest.mark.parametrize(
    "payload", [b"", b"short", b"x" * 4097, b"x" * 32 + b"\xff", b"x" * 32 + b"\nextra"]
)
def test_descriptor_rejects_invalid_payload_and_always_closes(
    tmp_path: Path, payload: bytes
) -> None:
    source = tmp_path / "invalid"
    source.write_bytes(payload)
    descriptor = os.open(source, os.O_RDONLY)
    with pytest.raises(IngressConfigurationError):
        LaunchCapability.from_descriptor(descriptor)
    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(descriptor)


@pytest.mark.parametrize("descriptor", [-1, 0, 1, 2])
def test_standard_streams_are_not_consumed(descriptor: int) -> None:
    with pytest.raises(IngressConfigurationError, match="at least 3"):
        LaunchCapability.from_descriptor(descriptor)


def test_descriptor_rejects_socket() -> None:
    transport = socket.socket()
    descriptor = transport.detach()
    with pytest.raises(IngressConfigurationError, match="file or pipe"):
        LaunchCapability.from_descriptor(descriptor)
    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(descriptor)


def test_descriptor_requires_pipe_eof(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("heartwood.gateway._access._DESCRIPTOR_TIMEOUT_SECONDS", 0.01)
    descriptor, writer = os.pipe()
    try:
        os.write(writer, _SECRET.encode())
        with pytest.raises(IngressConfigurationError, match="timed out"):
            LaunchCapability.from_descriptor(descriptor)
        with pytest.raises(OSError, match="Bad file descriptor"):
            os.fstat(descriptor)
    finally:
        os.close(writer)


@pytest.mark.parametrize(
    "secret",
    ["short", "x" * 4097, "has space " + "x" * 30, "a;b" + "x" * 30, '"' + "x" * 32, "\u00e9" * 32],
)
def test_capability_rejects_unusable_secrets(secret: str) -> None:
    with pytest.raises(IngressConfigurationError):
        LaunchCapability(secret, launch_token=None)


def test_capability_accepts_explicit_headers_and_rejects_cookies() -> None:
    capability = LaunchCapability(_SECRET, launch_token=None)
    assert capability.authorizes({"x-heartwood-capability": (_SECRET,)})
    assert not capability.authorizes({"cookie": ("heartwood-capability=" + _SECRET,)})
    assert not capability.authorizes({"x-heartwood-capability": ("\u00e9" * 40,)})
    assert not capability.authorizes({})
