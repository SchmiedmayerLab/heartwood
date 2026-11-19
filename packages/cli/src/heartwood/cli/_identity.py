# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Sign-in from a terminal with the OAuth device authorization grant.

The command obtains an access token for a hosted gateway as a public client. It prints
the token on standard output and stores nothing.
"""

from __future__ import annotations

import argparse
import ssl
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from heartwood.gateway._transport import require_protected_transport

_DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
_REQUEST_TIMEOUT_SECONDS = 10.0
_DEFAULT_INTERVAL_SECONDS = 5.0
_MAXIMUM_RESPONSE_BYTES = 65_536
# The refusals a provider reports for a device code; other error text is not shown.
_REFUSALS = {
    "access_denied": "the sign-in was denied",
    "expired_token": "the sign-in code expired",
}


class IdentityTokenError(RuntimeError):
    """A sign-in that produced no token, described without provider text."""


def configure_identity(parser: argparse.ArgumentParser) -> None:
    """Attach the command that obtains an access token for a hosted gateway."""
    commands = parser.add_subparsers(dest="identity_command", required=True)
    token = commands.add_parser(
        "token",
        help="Sign in with a device code and print an access token on standard output.",
    )
    token.add_argument("--issuer", required=True, help="OpenID Connect issuer URL.")
    token.add_argument("--client-id", required=True, help="Public client of this command.")
    token.add_argument(
        "--scope",
        action="append",
        default=[],
        help="Scope to request, such as the project's scope; repeat for several.",
    )
    token.add_argument("--ca-file", type=Path, help="PEM certificate authorities for the provider.")
    token.add_argument(
        "--timeout",
        type=float,
        default=600.0,
        help="Seconds to wait for the sign-in; the provider's code lifetime also applies.",
    )


def handle_identity(args: argparse.Namespace) -> int:
    """Run the device flow; the token is the only text on standard output."""
    try:
        with _client(args.ca_file) as http:
            token = device_access_token(
                http,
                issuer=args.issuer,
                client_id=args.client_id,
                scopes=args.scope,
                timeout_seconds=args.timeout,
            )
    except IdentityTokenError as error:
        print(f"Sign-in failed: {error}", file=sys.stderr)
        return 1
    print(token)
    return 0


def _client(ca_file: Path | None) -> httpx.Client:
    try:
        verify: ssl.SSLContext | bool = (
            True if ca_file is None else ssl.create_default_context(cafile=str(ca_file))
        )
    except (OSError, ValueError):
        raise IdentityTokenError("the certificate authority file is unavailable") from None
    return httpx.Client(timeout=_REQUEST_TIMEOUT_SECONDS, verify=verify, follow_redirects=False)


def device_access_token(
    http: httpx.Client,
    *,
    issuer: str,
    client_id: str,
    scopes: list[str],
    timeout_seconds: float,
) -> str:
    """Return an access token after the person approves the device code in a browser."""
    parts = urlsplit(issuer)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise IdentityTokenError("the issuer is an http(s) URL without credentials or query")
    try:
        require_protected_transport(issuer)
    except ValueError:
        raise IdentityTokenError("the issuer must use HTTPS or loopback") from None
    origin = f"{parts.scheme}://{parts.netloc}".lower()
    document = _json(http, "GET", f"{issuer.rstrip('/')}/.well-known/openid-configuration")
    if document.get("issuer") != issuer:
        raise IdentityTokenError("the provider's discovery document names another issuer")
    endpoints = [document.get("device_authorization_endpoint"), document.get("token_endpoint")]
    for endpoint in endpoints:
        # Requests stay on the issuer's origin, whatever the document names.
        if not isinstance(endpoint, str) or not endpoint.lower().startswith(f"{origin}/"):
            raise IdentityTokenError("the provider offers no device sign-in at its own origin")
    device_endpoint, token_endpoint = (str(endpoint) for endpoint in endpoints)
    grant = _json(
        http,
        "POST",
        device_endpoint,
        {"client_id": client_id, **({"scope": " ".join(scopes)} if scopes else {})},
    )
    device_code = grant.get("device_code")
    user_code = grant.get("user_code")
    verification_uri = grant.get("verification_uri_complete") or grant.get("verification_uri")
    lifetime = _number(grant.get("expires_in"))
    if (
        not isinstance(device_code, str)
        or not _printable(user_code)
        or not _printable(verification_uri)
        or lifetime is None
    ):
        raise IdentityTokenError("the provider refused the device sign-in request")
    print(f"Open {verification_uri}\nand confirm the code {user_code}", file=sys.stderr)
    interval = _number(grant.get("interval")) or _DEFAULT_INTERVAL_SECONDS
    deadline = time.monotonic() + min(lifetime, timeout_seconds)
    while time.monotonic() + interval < deadline:
        time.sleep(interval)
        answer = _json(
            http,
            "POST",
            token_endpoint,
            {"grant_type": _DEVICE_GRANT, "device_code": device_code, "client_id": client_id},
            refusals=True,
        )
        error = answer.get("error")
        if error == "slow_down":
            interval += 5.0
        elif error in _REFUSALS:
            raise IdentityTokenError(_REFUSALS[error])
        elif error is not None and error != "authorization_pending":
            raise IdentityTokenError("the provider refused the sign-in")
        elif error is None:
            token = answer.get("access_token")
            if not _printable(token) or str(answer.get("token_type")).lower() != "bearer":
                raise IdentityTokenError("the provider returned no usable access token")
            return str(token)
    raise IdentityTokenError("the sign-in was not completed in time")


def _json(
    http: httpx.Client,
    method: str,
    url: str,
    form: dict[str, str] | None = None,
    *,
    refusals: bool = False,
) -> dict[str, Any]:
    """Request one JSON object; a refusal body is read only where the flow defines it."""
    try:
        response = http.request(method, url, data=form, headers={"accept": "application/json"})
    except httpx.HTTPError:
        raise IdentityTokenError("the identity provider could not be reached") from None
    accepted = response.status_code == 200 or (refusals and 400 <= response.status_code < 500)
    if not accepted or len(response.content) > _MAXIMUM_RESPONSE_BYTES:
        raise IdentityTokenError("the identity provider returned an unusable answer")
    try:
        document = response.json()
    except ValueError:
        raise IdentityTokenError("the identity provider returned an unusable answer") from None
    if not isinstance(document, dict) or (response.status_code != 200 and "error" not in document):
        raise IdentityTokenError("the identity provider returned an unusable answer")
    return document


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return float(value)


def _printable(value: object) -> bool:
    """Accept only text that is safe to print to a terminal on one line."""
    return (
        isinstance(value, str)
        and 0 < len(value) <= 8192
        and all("\x21" <= character <= "\x7e" for character in value)
    )
