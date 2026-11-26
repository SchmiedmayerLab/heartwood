# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Authentication of the callers of a platform service.

Researchers present a Keycloak access token that a project client obtained by token
exchange for this service. Platform components present a projected service account
token whose audience is this service.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import jwt

_PROJECT_CLIENT = re.compile(r"hw-proj-([a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?)")
_REQUIRED_CLAIMS = ["exp", "iat", "iss", "aud", "sub", "azp"]
# Lower bound between two key fetches, so unknown key identifiers cannot flood Keycloak.
_KEY_REFRESH_SECONDS = 30.0


class AuthenticationError(Exception):
    """The caller did not prove an identity that this service accepts."""


@dataclass(frozen=True)
class Researcher:
    """A researcher acting in one project."""

    subject: str
    project: str


class ResearcherTokens:
    """Verifies exchanged Keycloak access tokens.

    The project comes from the authorized party: every exchanged token names the
    project client that requested it, and no other client can obtain such a token.
    """

    def __init__(
        self,
        issuer: str,
        audience: str,
        fetch_keys: Callable[[], dict[str, Any]],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Verify tokens of one issuer for one audience with the issuer's key set."""
        self._issuer = issuer
        self._audience = audience
        self._fetch_keys = fetch_keys
        self._clock = clock
        self._keys: dict[str, jwt.PyJWK] = {}
        self._fetched_at: float | None = None
        self._lock = threading.Lock()

    def _key(self, key_id: str) -> jwt.PyJWK:
        with self._lock:
            now = self._clock()
            stale = self._fetched_at is None or now - self._fetched_at >= _KEY_REFRESH_SECONDS
            if key_id not in self._keys and stale:
                # A failed fetch raises and leaves the time unset, so the next token
                # fetches again.
                keys = jwt.PyJWKSet.from_dict(self._fetch_keys()).keys
                self._keys = {key.key_id: key for key in keys if key.key_id is not None}
                self._fetched_at = now
            try:
                return self._keys[key_id]
            except KeyError:
                raise AuthenticationError("unknown signing key") from None

    def verify(self, token: str) -> Researcher:
        """Return the researcher and project that a token is bound to."""
        try:
            header = jwt.get_unverified_header(token)
            key_id = header.get("kid")
            if not isinstance(key_id, str):
                raise AuthenticationError("token names no signing key")
            claims = jwt.decode(
                token,
                self._key(key_id),
                algorithms=["RS256"],
                audience=self._audience,
                issuer=self._issuer,
                options={"require": _REQUIRED_CLAIMS},
            )
        except (jwt.InvalidTokenError, jwt.PyJWKError, ValueError) as error:
            raise AuthenticationError("invalid token") from error
        # One token carries one service audience.
        if claims["aud"] not in (self._audience, [self._audience]):
            raise AuthenticationError("token has another audience")
        if claims.get("typ") != "Bearer":
            raise AuthenticationError("token is not an access token")
        client = claims["azp"]
        match = _PROJECT_CLIENT.fullmatch(client) if isinstance(client, str) else None
        subject = claims["sub"]
        if match is None or not isinstance(subject, str) or not subject:
            raise AuthenticationError("token is not bound to a project")
        return Researcher(subject=subject, project=match.group(1))


class ServiceTokens:
    """Verifies projected service account tokens with a TokenReview."""

    def __init__(
        self,
        review: Callable[[str, str], dict[str, Any]],
        audience: str,
        callers: frozenset[str],
    ) -> None:
        """Accept the listed service account user names for one audience."""
        self._review = review
        self._audience = audience
        self._callers = callers

    def verify(self, token: str) -> str:
        """Return the user name of an accepted calling service account."""
        status = self._review(token, self._audience)
        username = status.get("user", {}).get("username")
        if (
            status.get("authenticated") is not True
            or self._audience not in status.get("audiences", [])
            or username not in self._callers
        ):
            raise AuthenticationError("service account token is not accepted")
        assert isinstance(username, str)
        return username
