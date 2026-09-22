# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Launch capability that binds gateway API access to the launching researcher."""

from __future__ import annotations

import secrets
from collections.abc import Mapping, MutableMapping
from http.cookies import CookieError, SimpleCookie
from threading import Lock
from urllib.parse import urlencode

from heartwood.gateway._ingress import IngressConfigurationError, IngressPolicy

CAPABILITY_COOKIE = "heartwood-capability"
CAPABILITY_HEADER = "x-heartwood-capability"
CAPABILITY_ENVIRONMENT = "HEARTWOOD_GATEWAY_CAPABILITY"
CAPABILITY_ACTOR = "human"
_MINIMUM_SECRET_LENGTH = 32


class LaunchCapability:
    """Process-lifetime secret that every gateway API, SSE, and WebSocket request must carry.

    The secret reaches the browser through a one-time launch link that sets an HttpOnly cookie.
    Automation and proxies supply the same secret through the capability header.
    """

    def __init__(self, secret: str, *, launch_token: str | None) -> None:
        if len(secret) < _MINIMUM_SECRET_LENGTH:
            raise IngressConfigurationError(
                f"gateway capability must be at least {_MINIMUM_SECRET_LENGTH} characters"
            )
        if any(character.isspace() or character in ";," for character in secret):
            raise IngressConfigurationError("gateway capability must be one opaque token")
        self._secret = secret
        self._launch_token = launch_token
        self._lock = Lock()

    @classmethod
    def generate(cls) -> LaunchCapability:
        """Create a fresh secret together with a one-time launch token."""
        return cls(secrets.token_urlsafe(32), launch_token=secrets.token_urlsafe(32))

    @classmethod
    def from_environment(cls, env: MutableMapping[str, str]) -> LaunchCapability | None:
        """Consume an operator-supplied secret so no child process inherits it."""
        secret = env.pop(CAPABILITY_ENVIRONMENT, None)
        if secret is None:
            return None
        return cls(secret, launch_token=None)

    @property
    def actor_id(self) -> str:
        """Return the principal recorded for commands presented with this capability."""
        return CAPABILITY_ACTOR

    def launch_url(self, ingress: IngressPolicy) -> str | None:
        """Return the one-time browser link, or None when the secret was supplied externally."""
        if self._launch_token is None:
            return None
        query = urlencode({"token": self._launch_token})
        return f"{ingress.external_origin}{ingress.browser_base_path}/launch?{query}"

    def consume_launch_token(self, token: str) -> bool:
        """Exchange the launch token exactly once."""
        with self._lock:
            expected = self._launch_token
            if expected is None or not secrets.compare_digest(expected, token):
                return False
            self._launch_token = None
            return True

    def authorizes(self, headers: Mapping[str, tuple[str, ...]]) -> bool:
        """Return whether the request carries the secret as a header or cookie."""
        presented = [value.strip() for value in headers.get(CAPABILITY_HEADER, ())]
        for raw in headers.get("cookie", ()):
            jar: SimpleCookie = SimpleCookie()
            try:
                jar.load(raw)
            except CookieError:
                continue
            morsel = jar.get(CAPABILITY_COOKIE)
            if morsel is not None:
                presented.append(morsel.value)
        return any(secrets.compare_digest(self._secret, value) for value in presented)

    def cookie_header(self, ingress: IngressPolicy) -> str:
        """Return the Set-Cookie value scoping the secret to the browser-visible gateway path."""
        attributes = [
            f"{CAPABILITY_COOKIE}={self._secret}",
            f"Path={ingress.browser_base_path or '/'}",
            "HttpOnly",
            "SameSite=Strict",
        ]
        if ingress.external_origin.startswith("https://"):
            attributes.append("Secure")
        return "; ".join(attributes)
