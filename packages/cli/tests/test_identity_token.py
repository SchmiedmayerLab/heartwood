# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Device sign-in from the terminal, against a synthetic in-process provider."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from heartwood.cli import main

_ISSUER = "https://id.example.test/realms/research"
_PROVIDER_TEXT = "synthetic provider detail"
_TOKEN = "synthetic.access.token"


class _Provider:
    """A synthetic provider that answers the device grant after a number of polls."""

    def __init__(self) -> None:
        self.pending: list[str] = ["authorization_pending"]
        self.discovery: dict[str, Any] = {}
        self.grant: dict[str, Any] = {}
        self.answer: dict[str, Any] = {}
        self.unreachable = False
        self.forms: list[dict[str, str]] = []
        self.sleeps: list[float] = []
        self.now = 0.0

    def clock(self) -> SimpleNamespace:
        def sleep(seconds: float) -> None:
            self.sleeps.append(seconds)
            self.now += seconds

        return SimpleNamespace(monotonic=lambda: self.now, sleep=sleep)

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.unreachable:
            raise httpx.ConnectError(_PROVIDER_TEXT, request=request)
        name = request.url.path.rsplit("/", 1)[-1]
        if name == "openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": _ISSUER,
                    "device_authorization_endpoint": f"{_ISSUER}/device",
                    "token_endpoint": f"{_ISSUER}/token",
                    **self.discovery,
                },
            )
        form = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
        self.forms.append(form)
        if name == "device":
            return httpx.Response(
                200,
                json={
                    "device_code": "synthetic-device-code",
                    "user_code": "WDJB-MJHT",
                    "verification_uri": f"{_ISSUER}/activate",
                    "expires_in": 600,
                    "interval": 2,
                    **self.grant,
                },
            )
        assert form == {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": "synthetic-device-code",
            "client_id": "heartwood-cli",
        }
        if self.pending:
            return httpx.Response(
                400, json={"error": self.pending.pop(0), "error_description": _PROVIDER_TEXT}
            )
        answer = {"access_token": _TOKEN, "token_type": "Bearer", "expires_in": 300, **self.answer}
        return httpx.Response(200, json=answer)


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Provider:
    synthetic = _Provider()
    monkeypatch.setattr(
        "heartwood.cli._identity._client",
        lambda _ca_file: httpx.Client(transport=httpx.MockTransport(synthetic.handle)),
    )
    monkeypatch.setattr("heartwood.cli._identity.time", synthetic.clock())
    monkeypatch.chdir(tmp_path)
    return synthetic


def _arguments(*extra: str) -> list[str]:
    return ["identity", "token", "--issuer", _ISSUER, "--client-id", "heartwood-cli", *extra]


def test_device_sign_in_prints_only_the_token_on_standard_output(
    provider: _Provider, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    provider.pending = ["authorization_pending", "slow_down", "authorization_pending"]

    assert main(_arguments("--scope", "openid", "--scope", "project-demo")) == 0

    captured = capsys.readouterr()
    assert captured.out == f"{_TOKEN}\n"
    assert f"Open {_ISSUER}/activate" in captured.err
    assert "WDJB-MJHT" in captured.err
    assert _TOKEN not in captured.err
    assert provider.forms[0] == {"client_id": "heartwood-cli", "scope": "openid project-demo"}
    # The provider's interval is kept, and a slow-down request adds five seconds.
    assert provider.sleeps == [2, 2, 7, 7]
    assert list(tmp_path.iterdir()) == []


def test_device_sign_in_prefers_the_complete_verification_address(
    provider: _Provider, capsys: pytest.CaptureFixture[str]
) -> None:
    provider.grant = {
        "verification_uri_complete": f"{_ISSUER}/activate?user_code=WDJB-MJHT",
        "interval": None,
    }

    assert main(_arguments()) == 0

    assert f"Open {_ISSUER}/activate?user_code=WDJB-MJHT" in capsys.readouterr().err
    assert provider.forms[0] == {"client_id": "heartwood-cli"}
    assert provider.sleeps == [5, 5]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda provider: provider.pending.insert(0, "access_denied"), "was denied"),
        (lambda provider: provider.pending.insert(0, "expired_token"), "code expired"),
        (lambda provider: provider.pending.insert(0, "invalid_client"), "refused the sign-in"),
        (lambda provider: provider.pending.extend(["authorization_pending"] * 400), "in time"),
        (lambda provider: provider.grant.update(expires_in=4), "in time"),
        (lambda provider: provider.grant.update(user_code="\x1b[2Jcode"), "refused the device"),
        (
            lambda provider: provider.grant.update(verification_uri="two words"),
            "refused the device",
        ),
        (lambda provider: provider.grant.update(device_code=None), "refused the device"),
        (lambda provider: provider.grant.update(expires_in=True), "refused the device"),
        (lambda provider: provider.answer.update(token_type="mac"), "no usable access token"),
        (lambda provider: provider.answer.update(access_token="two words"), "no usable access"),
        (lambda provider: provider.discovery.update(issuer="https://other.example.test"), "issuer"),
        (
            lambda provider: provider.discovery.update(
                token_endpoint="https://other.example.test/token"
            ),
            "no device sign-in at its own origin",
        ),
        (
            lambda provider: provider.discovery.update(device_authorization_endpoint=None),
            "no device sign-in at its own origin",
        ),
        (lambda provider: setattr(provider, "unreachable", True), "could not be reached"),
    ],
)
def test_device_sign_in_fails_without_a_token_or_provider_text(
    provider: _Provider,
    capsys: pytest.CaptureFixture[str],
    change: Callable[[_Provider], object],
    message: str,
) -> None:
    change(provider)

    assert main(_arguments("--timeout", "300")) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.splitlines()[-1].startswith("Sign-in failed: ")
    assert message in captured.err
    assert _PROVIDER_TEXT not in captured.err
    assert "\x1b" not in captured.err


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, text=_PROVIDER_TEXT),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json=["not", "an", "object"]),
        httpx.Response(200, content=b" " * 65_537),
        httpx.Response(400, json={"detail": _PROVIDER_TEXT}),
    ],
)
def test_device_sign_in_reports_unusable_provider_answers(
    provider: _Provider,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    response: httpx.Response,
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return response if request.url.path.endswith("/token") else provider.handle(request)

    monkeypatch.setattr(
        "heartwood.cli._identity._client",
        lambda _ca_file: httpx.Client(transport=httpx.MockTransport(handle)),
    )

    assert main(_arguments()) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "unusable answer" in captured.err
    assert _PROVIDER_TEXT not in captured.err


@pytest.mark.parametrize(
    ("issuer", "extra", "message"),
    [
        ("http://id.example.test/realms/research", (), "HTTPS or loopback"),
        ("ftp://id.example.test", (), "http(s) URL"),
        ("https://user:secret@id.example.test", (), "http(s) URL"),
        # With the opt-in the request is made; the synthetic provider names its own issuer.
        ("http://id.example.test/realms/research", ("--allow-plain-http",), "another issuer"),
    ],
)
def test_device_sign_in_requires_a_protected_issuer(
    provider: _Provider,
    capsys: pytest.CaptureFixture[str],
    issuer: str,
    extra: tuple[str, ...],
    message: str,
) -> None:
    arguments = ["identity", "token", "--issuer", issuer, "--client-id", "heartwood-cli", *extra]

    assert main(arguments) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert provider.forms == []
    assert message in captured.err


def test_device_sign_in_reads_a_certificate_authority_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    broken = tmp_path / "ca.pem"
    broken.write_text("not a certificate")

    assert main(_arguments("--ca-file", str(broken))) == 1
    assert "certificate authority file is unavailable" in capsys.readouterr().err

    # Nothing listens on the discard port, so a default client reports the provider as unreachable.
    loopback = ["identity", "token", "--issuer", "http://127.0.0.1:9", "--client-id", "cli"]
    assert main(loopback) == 1
    assert "could not be reached" in capsys.readouterr().err
