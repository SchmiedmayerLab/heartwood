# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Launch capability that binds gateway API access to the launching researcher."""

from __future__ import annotations

import os
import re
import secrets
import selectors
import stat
import time
from collections.abc import Mapping
from threading import Lock
from urllib.parse import urlencode

from heartwood.gateway._ingress import IngressConfigurationError, IngressPolicy

CAPABILITY_COOKIE = "heartwood-capability"
CAPABILITY_HEADER = "x-heartwood-capability"
CAPABILITY_ENVIRONMENT = "HEARTWOOD_GATEWAY_CAPABILITY"
CAPABILITY_ACTOR = "human"
_MINIMUM_SECRET_LENGTH = 32
_MAXIMUM_SECRET_BYTES = 4096
_DESCRIPTOR_TIMEOUT_SECONDS = 5.0
_COOKIE_OCTETS = re.compile(r"[\x21\x23-\x2B\x2D-\x3A\x3C-\x5B\x5D-\x7E]+")  # RFC 6265


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
        if _COOKIE_OCTETS.fullmatch(secret) is None:
            raise IngressConfigurationError(
                "gateway capability must be printable ASCII without spaces, quotes, commas, "
                "semicolons, or backslashes"
            )
        self._secret = secret.encode("ascii")
        self._launch_token = launch_token
        self._lock = Lock()

    @classmethod
    def generate(cls) -> LaunchCapability:
        """Create a fresh secret together with a one-time launch token."""
        return cls(secrets.token_urlsafe(32), launch_token=secrets.token_urlsafe(32))

    @classmethod
    def from_descriptor(cls, descriptor: int) -> LaunchCapability:
        """Consume and close an exclusively handed-off file or pipe descriptor.

        Standard streams are never consumed. Pipes must reach EOF within five
        seconds; the bounded payload may end with one newline. The launcher owns
        source-file permissions and must keep the source outside worker access.
        """
        if descriptor < 3:
            raise IngressConfigurationError("capability descriptor must be at least 3")
        try:
            with os.fdopen(descriptor, "rb", buffering=0) as source:
                os.set_inheritable(descriptor, False)
                mode = os.fstat(descriptor).st_mode
                if not (stat.S_ISREG(mode) or stat.S_ISFIFO(mode)):
                    raise IngressConfigurationError("capability descriptor must be a file or pipe")
                payload = bytearray()
                with selectors.DefaultSelector() as selector:
                    if stat.S_ISFIFO(mode):
                        os.set_blocking(descriptor, False)
                        selector.register(descriptor, selectors.EVENT_READ)
                    deadline = time.monotonic() + _DESCRIPTOR_TIMEOUT_SECONDS
                    while True:
                        if stat.S_ISFIFO(mode):
                            remaining = deadline - time.monotonic()
                            if remaining <= 0 or not selector.select(remaining):
                                raise IngressConfigurationError("capability descriptor timed out")
                        chunk = source.read(_MAXIMUM_SECRET_BYTES + 1 - len(payload))
                        if chunk is None:
                            continue
                        if not chunk:
                            break
                        payload.extend(chunk)
                        if len(payload) > _MAXIMUM_SECRET_BYTES:
                            raise IngressConfigurationError(
                                "capability descriptor exceeds 4096 bytes"
                            )
                secret = payload.removesuffix(b"\n").decode("ascii")
        except IngressConfigurationError:
            raise
        except (OSError, ValueError) as error:
            raise IngressConfigurationError("cannot read capability descriptor") from error
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
            if expected is None or not secrets.compare_digest(expected.encode(), token.encode()):
                return False
            self._launch_token = None
            return True

    def authorizes(self, headers: Mapping[str, tuple[str, ...]]) -> bool:
        """Return whether the request carries the secret as a header or cookie."""
        presented = [value.strip() for value in headers.get(CAPABILITY_HEADER, ())]
        for raw in headers.get("cookie", ()):
            for pair in raw.split(";"):
                name, separator, value = pair.strip().partition("=")
                if separator and name == CAPABILITY_COOKIE:
                    presented.append(value)
        return any(secrets.compare_digest(self._secret, value.encode()) for value in presented)

    def cookie_header(self, ingress: IngressPolicy) -> str:
        """Return the Set-Cookie value scoping the secret to the browser-visible gateway path."""
        attributes = [
            f"{CAPABILITY_COOKIE}={self._secret.decode('ascii')}",
            f"Path={ingress.browser_base_path or '/'}",
            "HttpOnly",
            "SameSite=Strict",
        ]
        if ingress.external_origin.startswith("https://"):
            attributes.append("Secure")
        return "; ".join(attributes)
