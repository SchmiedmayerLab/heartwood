# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Researcher identity verified with OpenID Connect for a gateway behind a reverse proxy.

The gateway is a confidential OpenID Connect client bound to one subject. A browser
signs in through the authorization code flow, and other clients present an access
token issued for this client. Tokens, the client secret, codes and capabilities stay
in this process's memory.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import ssl
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from threading import Lock, RLock
from typing import Any, Literal, Self
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import httpx
import jwt
from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from heartwood.core_adapter.action_review import AUTOMATIC_REVIEW_ACTOR
from heartwood.gateway._access import (
    CAPABILITY_ACTOR,
    CAPABILITY_HEADER,
    OPERATOR_ACTOR,
    AccessResponse,
    AccessUnavailableError,
    GatewayAccess,
    LaunchCapability,
)
from heartwood.gateway._ingress import (
    IDENTITY_CALLBACK_PATH,
    IDENTITY_LOGIN_PATH,
    IngressConfigurationError,
    IngressPolicy,
)
from heartwood.gateway._transport import require_protected_transport
from heartwood.persistence import read_private_text

IDENTITY_INGRESS_MODE = "identity-proxy"
LOGIN_PATH = IDENTITY_LOGIN_PATH
CALLBACK_PATH = IDENTITY_CALLBACK_PATH
# Holds the sealed state of a sign-in in progress, for the browser that started it.
SIGN_IN_COOKIE = "heartwood-sign-in"
# Lets the index document load once after a sign-in; it authorizes nothing.
LOGIN_COOKIE = "heartwood-login"

# Asymmetric signatures only: a token never selects an unsigned or shared-secret algorithm.
_ALGORITHMS = ("RS256", "ES256")
_IDENTITY_TOKEN_CLAIMS = frozenset({"nonce", "at_hash", "c_hash"})
_CLOCK_SKEW_SECONDS = 30
_PROVIDER_TIMEOUT_SECONDS = 10.0
_MAXIMUM_PROVIDER_RESPONSE_BYTES = 1_048_576
# Lower bound between two key fetches, so unknown key identifiers cannot flood the provider.
_KEY_REFRESH_SECONDS = 30.0
_SIGN_IN_LIFETIME_SECONDS = 600
_MAXIMUM_USED_STATES = 1024
# A failed discovery or key fetch is answered from memory for this long.
_PROVIDER_RETRY_SECONDS = 5.0
_MAXIMUM_LOGINS = 8
_LAUNCH_LIFETIME_SECONDS = 60
_REFRESH_MARGIN_SECONDS = 30.0

type _TokenUse = Literal["id", "issued", "bearer"]


class IdentityDeployment(BaseModel):
    """The manifest's ``[identity]`` table: one OpenID Connect client and one researcher."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    issuer: str = Field(min_length=1, max_length=2048)
    client_id: str = Field(min_length=1, max_length=255, pattern=r"^[\x21-\x7E]+$")
    client_secret_file: Path = Field(repr=False)
    # The subject becomes the actor of every command, so it follows the actor reference form.
    subject: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
    provider_url: str | None = Field(default=None, min_length=1, max_length=2048)
    ca_file: Path | None = None
    allow_plain_http: StrictBool = False

    @property
    def provider(self) -> str:
        """Return the base URL where the gateway reaches the provider."""
        return self.issuer if self.provider_url is None else self.provider_url

    @model_validator(mode="after")
    def _validate(self) -> Self:
        for url in (self.issuer, self.provider):
            parts = urlsplit(url)
            try:
                parts.port  # noqa: B018
            except ValueError:
                raise ValueError("identity provider URL has an invalid port") from None
            if (
                parts.scheme not in {"http", "https"}
                or not parts.hostname
                or parts.username is not None
                or parts.password is not None
                or parts.query
                or parts.fragment
                or any(character.isspace() for character in url)
            ):
                raise ValueError(
                    "identity provider URLs are http(s) URLs without credentials, query or fragment"
                )
            if not self.allow_plain_http:
                require_protected_transport(url)
        if self.subject in {CAPABILITY_ACTOR, AUTOMATIC_REVIEW_ACTOR, OPERATOR_ACTOR}:
            raise ValueError("identity subject names an actor the gateway reserves")
        for path in (self.client_secret_file, self.ca_file):
            if path is not None and not path.is_absolute():
                raise ValueError("identity files are absolute paths")
        return self


class _SignInRefusedError(Exception):
    """The provider or a token did not establish the bound researcher."""


@dataclass(slots=True, repr=False)
class _Login:
    access_token: str
    refresh_token: str | None
    access_expires_at: float
    expires_at: float
    launch_token: str | None
    launch_expires_at: float


@dataclass(frozen=True, slots=True)
class _Endpoints:
    authorization: str
    token: str
    keys: str


class IdentityAccess:
    """Authorize requests of the one researcher an OpenID Connect provider verifies."""

    refusal_check = "gateway-identity"

    def __init__(
        self,
        deployment: IdentityDeployment,
        ingress: IngressPolicy,
        *,
        client_secret: str,
        http: httpx.Client,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Bind one provider client and subject to one gateway route."""
        if ingress.mode != IDENTITY_INGRESS_MODE:
            raise IngressConfigurationError(
                "the manifest's [identity] table requires identity-proxy ingress"
            )
        self._issuer = deployment.issuer
        self._provider = deployment.provider
        self._client_id = deployment.client_id
        self._client_secret = client_secret
        self._subject = deployment.subject.encode()
        self._base_url = f"{ingress.external_origin}{ingress.browser_base_path}"
        if not self._base_url.isascii():
            raise IngressConfigurationError(
                "identity-proxy ingress requires an ASCII external origin and base path"
            )
        self._cookie_attributes = f"Path={ingress.external_base_path}; HttpOnly; SameSite=Lax" + (
            "; Secure" if ingress.external_origin.startswith("https://") else ""
        )
        # The key lives as long as the process, so a restart ends every sign-in in progress.
        self._seal = Fernet(Fernet.generate_key())
        self._http = http
        self._clock = clock
        self._lock = Lock()
        self._used_states: OrderedDict[str, float] = OrderedDict()
        self._logins: OrderedDict[str, _Login] = OrderedDict()
        self._refresh_lock = Lock()
        self._provider_lock = RLock()
        self._endpoints: _Endpoints | None = None
        self._keys: dict[str, jwt.PyJWK] = {}
        self._keys_fetched_at: float | None = None
        self._provider_failure: tuple[float, int] | None = None

    @classmethod
    def create(cls, deployment: IdentityDeployment, ingress: IngressPolicy) -> IdentityAccess:
        """Read the client secret and prepare the connection to the provider."""
        try:
            secret = read_private_text(deployment.client_secret_file, max_bytes=4096)
            verify: ssl.SSLContext | bool = (
                True
                if deployment.ca_file is None
                else ssl.create_default_context(cafile=str(deployment.ca_file))
            )
        except (OSError, ValueError):
            raise IngressConfigurationError(
                "identity client secret or certificate authority file is unavailable"
            ) from None
        secret = secret.removesuffix("\n")
        if not secret or any(not "\x21" <= character <= "\x7e" for character in secret):
            raise IngressConfigurationError(
                "identity client secret must be printable ASCII without spaces"
            )
        http = httpx.Client(
            timeout=_PROVIDER_TIMEOUT_SECONDS, verify=verify, follow_redirects=False
        )
        return cls(deployment, ingress, client_secret=secret, http=http)

    def launch_url(self, ingress: IngressPolicy) -> str | None:  # noqa: ARG002
        """Issue no launch link: the researcher signs in at the gateway's URL."""
        return None

    def principal(self, headers: Mapping[str, tuple[str, ...]]) -> str | None:
        """Return the bound subject for a signed-in browser or a valid access token."""
        for value in headers.get(CAPABILITY_HEADER, ()):
            with self._lock:
                if self._login(value.strip()) is not None:
                    return self._subject.decode()
        authorization = headers.get("authorization", ())
        if len(authorization) != 1:
            return None
        scheme, _, token = authorization[0].strip().partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            return None
        try:
            self._verified(token.strip(), "bearer")
        except _SignInRefusedError:
            return None
        return self._subject.decode()

    def exchange_launch_token(self, token: str) -> str | None:
        """Exchange the launch token of one sign-in for its capability exactly once."""
        presented = token.encode()
        with self._lock:
            now = self._purge()
            for capability, login in self._logins.items():
                if (
                    login.launch_token is not None
                    and login.launch_expires_at > now
                    and secrets.compare_digest(login.launch_token.encode(), presented)
                ):
                    login.launch_token = None
                    return capability
        return None

    def browser_response(
        self,
        *,
        method: str,
        path: str,
        query_string: str,
        headers: Mapping[str, tuple[str, ...]],
    ) -> AccessResponse | None:
        """Start a sign-in, complete one, or send the index document's reader to sign in."""
        if method != "GET":
            return None
        try:
            if path == LOGIN_PATH:
                return self._begin_sign_in()
            if path == CALLBACK_PATH:
                return self._complete_sign_in(query_string, headers)
        except AccessUnavailableError as error:
            return AccessResponse(error.status_code, check_id="gateway-identity-provider")
        except _SignInRefusedError:
            return AccessResponse(403, check_id=self.refusal_check)
        if path == "/" and _cookie_value(headers, LOGIN_COOKIE) != "1":
            return AccessResponse(302, location=f"{self._base_url}{LOGIN_PATH}")
        return None

    def researcher_access_token(self) -> str | None:
        """Return the access token of the latest sign-in, refreshed when it is about to expire.

        Returns None when no sign-in is live or the provider refuses the refresh. Raises
        ``AccessUnavailableError`` when the provider cannot be reached.
        """
        with self._refresh_lock:
            with self._lock:
                now = self._purge()
                if not self._logins:
                    return None
                capability, login = next(reversed(self._logins.items()))
                if (
                    login.access_expires_at - now > _REFRESH_MARGIN_SECONDS
                    or login.refresh_token is None
                ):
                    return login.access_token
                refresh_token = login.refresh_token
            try:
                refreshed = self._tokens(
                    self._token_request(
                        {"grant_type": "refresh_token", "refresh_token": refresh_token}
                    ),
                    refresh_token=refresh_token,
                )
            except _SignInRefusedError:
                with self._lock:
                    self._logins.pop(capability, None)
                return None
            with self._lock:
                if capability not in self._logins:
                    return None
                login.access_token = refreshed.access_token
                login.refresh_token = refreshed.refresh_token
                login.access_expires_at = refreshed.access_expires_at
                login.expires_at = refreshed.expires_at
                return login.access_token

    def _begin_sign_in(self) -> AccessResponse:
        endpoint = self._discovered().authorization
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        # The sign-in is bound to the browser that started it: its state, nonce and code
        # verifier travel only in a cookie sealed with this process's key, so the gateway
        # keeps no table that requests without identity could fill.
        sealed = self._seal.encrypt_at_time(
            json.dumps([state, nonce, verifier]).encode(), int(self._clock())
        )
        query = urlencode(
            {
                "response_type": "code",
                "scope": "openid",
                "client_id": self._client_id,
                "redirect_uri": f"{self._base_url}{CALLBACK_PATH}",
                "state": state,
                "nonce": nonce,
                "code_challenge": challenge.rstrip(b"=").decode(),
                "code_challenge_method": "S256",
            }
        )
        return AccessResponse(
            302,
            location=f"{endpoint}{'&' if '?' in endpoint else '?'}{query}",
            cookies=(self._cookie(SIGN_IN_COOKIE, sealed.decode(), _SIGN_IN_LIFETIME_SECONDS),),
        )

    def _complete_sign_in(
        self, query_string: str, headers: Mapping[str, tuple[str, ...]]
    ) -> AccessResponse:
        try:
            query = parse_qs(query_string, keep_blank_values=True, max_num_fields=16)
        except ValueError:
            raise _SignInRefusedError from None
        nonce, verifier = self._started_sign_in(_one(query, "state"), headers)
        if "error" in query or query.get("iss", [self._issuer]) != [self._issuer]:
            raise _SignInRefusedError
        payload = self._token_request(
            {
                "grant_type": "authorization_code",
                "code": _one(query, "code"),
                "redirect_uri": f"{self._base_url}{CALLBACK_PATH}",
                "code_verifier": verifier,
            }
        )
        id_token = payload.get("id_token")
        if not isinstance(id_token, str):
            raise AccessUnavailableError(502)
        returned = self._verified(id_token, "id").get("nonce")
        if not isinstance(returned, str) or not secrets.compare_digest(
            returned.encode(), nonce.encode()
        ):
            raise _SignInRefusedError
        login = self._tokens(payload, refresh_token=None)
        capability = secrets.token_urlsafe(32)
        launch_token = secrets.token_urlsafe(32)
        login.launch_token = launch_token
        with self._lock:
            login.launch_expires_at = self._purge() + _LAUNCH_LIFETIME_SECONDS
            while len(self._logins) >= _MAXIMUM_LOGINS:
                self._logins.popitem(last=False)
            self._logins[capability] = login
        # The marker cookie authorizes nothing. It only lets the index document through
        # once, so the page can exchange the launch token; every API request needs the
        # capability. The sign-in cookie has served its purpose and is removed.
        return AccessResponse(
            302,
            location=f"{self._base_url}/#{urlencode({'launch': launch_token})}",
            cookies=(
                self._cookie(SIGN_IN_COOKIE, "", 0),
                self._cookie(LOGIN_COOKIE, "1", _LAUNCH_LIFETIME_SECONDS),
            ),
        )

    def _started_sign_in(
        self, state: str, headers: Mapping[str, tuple[str, ...]]
    ) -> tuple[str, str]:
        """Return the nonce and code verifier of the sign-in this browser started.

        The callback's state must equal the one sealed in the browser's cookie, and each
        state is accepted once. The record of used states is bounded; beyond it, a second
        use still fails at the provider, which redeems an authorization code once.
        """
        try:
            now = self._clock()
            sealed = _cookie_value(headers, SIGN_IN_COOKIE).encode()
            started, nonce, verifier = json.loads(
                self._seal.decrypt_at_time(sealed, _SIGN_IN_LIFETIME_SECONDS, int(now))
            )
            matches = secrets.compare_digest(str(started).encode(), state.encode())
        except (InvalidToken, ValueError, TypeError):
            raise _SignInRefusedError from None
        with self._lock:
            for used in [key for key, expiry in self._used_states.items() if expiry <= now]:
                del self._used_states[used]
            if not matches or started in self._used_states:
                raise _SignInRefusedError
            while len(self._used_states) >= _MAXIMUM_USED_STATES:
                self._used_states.popitem(last=False)
            self._used_states[started] = now + _SIGN_IN_LIFETIME_SECONDS
        return str(nonce), str(verifier)

    def _cookie(self, name: str, value: str, lifetime: int) -> str:
        return f"{name}={value}; Max-Age={lifetime}; {self._cookie_attributes}"

    def _login(self, capability: str) -> _Login | None:
        try:
            presented = capability.encode()
        except UnicodeError:
            return None
        self._purge()
        for known, login in self._logins.items():
            if secrets.compare_digest(known.encode(), presented):
                return login
        return None

    def _purge(self) -> float:
        now = self._clock()
        for capability in [key for key, item in self._logins.items() if item.expires_at <= now]:
            del self._logins[capability]
        return now

    def _tokens(self, payload: Mapping[str, Any], *, refresh_token: str | None) -> _Login:
        """Read one token response; a sign-in ends when its refresh token or access token does."""
        access_token = payload.get("access_token")
        lifetime = _seconds(payload.get("expires_in"))
        if (
            not isinstance(access_token, str)
            or not access_token
            or lifetime is None
            or str(payload.get("token_type")).lower() != "bearer"
        ):
            raise AccessUnavailableError(502)
        if _is_signed_token(access_token):
            self._verified(access_token, "issued")
        issued_refresh = payload.get("refresh_token")
        if isinstance(issued_refresh, str) and issued_refresh:
            refresh_token = issued_refresh
        now = self._clock()
        refresh_lifetime = _seconds(payload.get("refresh_expires_in"))
        return _Login(
            access_token=access_token,
            refresh_token=refresh_token,
            access_expires_at=now + lifetime,
            expires_at=now
            + (lifetime if refresh_token is None or refresh_lifetime is None else refresh_lifetime),
            launch_token=None,
            launch_expires_at=now,
        )

    def _verified(self, token: str, use: _TokenUse) -> dict[str, Any]:
        """Verify signature, issuer, lifetime, audience and the bound subject of one token."""
        try:
            header = jwt.get_unverified_header(token)
            key_id = header.get("kid")
            if header.get("alg") not in _ALGORITHMS or not isinstance(key_id, str):
                raise _SignInRefusedError
            # A token of another issuer is refused before any key is looked up or fetched.
            if jwt.decode(token, options={"verify_signature": False}).get("iss") != self._issuer:
                raise _SignInRefusedError
            claims: dict[str, Any] = jwt.decode(
                token,
                self._key(key_id),
                algorithms=list(_ALGORITHMS),
                issuer=self._issuer,
                leeway=_CLOCK_SKEW_SECONDS,
                options={
                    "require": ["exp", "iss", "sub", *(("iat", "aud") if use == "id" else ())],
                    "verify_aud": False,
                },
            )
        except (jwt.PyJWTError, ValueError):
            raise _SignInRefusedError from None
        audience = claims.get("aud")
        audiences = audience if isinstance(audience, list) else [audience]
        authorized_party = claims.get("azp")
        subject = claims.get("sub")
        if use == "id":
            # The token is for this client alone, or names it as the authorized party.
            accepted = self._client_id in audiences and (
                authorized_party == self._client_id
                or (authorized_party is None and len(audiences) == 1)
            )
        elif use == "issued":
            # A provider may leave the requesting client out of its own access token's audience.
            accepted = self._client_id in audiences or authorized_party == self._client_id
        else:
            # An identity token names this client as its audience too, and it is no
            # access token: its marks are refused.
            accepted = (
                self._client_id in audiences
                and not _IDENTITY_TOKEN_CLAIMS.intersection(claims)
                and str(claims.get("typ")).lower() != "id"
            )
        if (
            not accepted
            or not isinstance(subject, str)
            or not secrets.compare_digest(subject.encode(), self._subject)
        ):
            raise _SignInRefusedError
        return claims

    def _key(self, key_id: str) -> jwt.PyJWK:
        # A known key is used without waiting for a fetch that another request started.
        if (known := self._keys.get(key_id)) is not None:
            return known
        with self._provider_lock:
            now = self._clock()
            stale = (
                self._keys_fetched_at is None or now - self._keys_fetched_at >= _KEY_REFRESH_SECONDS
            )
            if key_id not in self._keys and stale:
                document = self._provider_document(self._discovered().keys)
                try:
                    keys = jwt.PyJWKSet.from_dict(document).keys
                except jwt.PyJWTError:
                    raise AccessUnavailableError(502) from None
                self._keys = {
                    key.key_id: key
                    for key in keys
                    if key.key_id is not None
                    and key.algorithm_name in _ALGORITHMS
                    and key.public_key_use in {"sig", None}
                }
                self._keys_fetched_at = now
            try:
                return self._keys[key_id]
            except KeyError:
                raise _SignInRefusedError from None

    def _discovered(self) -> _Endpoints:
        """Read the provider's discovery document once and pin each endpoint to its origin.

        The browser is sent to the issuer's origin, and the gateway requests tokens and
        keys at the provider URL's origin, whichever of the two the document names.
        """
        with self._provider_lock:
            if self._endpoints is None:
                document = self._provider_document(
                    f"{self._provider.rstrip('/')}/.well-known/openid-configuration"
                )
                if document.get("issuer") != self._issuer:
                    raise AccessUnavailableError(502)
                self._endpoints = _Endpoints(
                    authorization=self._endpoint(document, "authorization_endpoint", self._issuer),
                    token=self._endpoint(document, "token_endpoint", self._provider),
                    keys=self._endpoint(document, "jwks_uri", self._provider),
                )
            return self._endpoints

    def _provider_document(self, url: str) -> dict[str, Any]:
        """Fetch discovery or keys; a failure is remembered briefly and not retried meanwhile.

        Requests without identity can ask for these documents, so an unavailable provider
        is asked once in a while and the other requests are answered from memory.
        """
        now = self._clock()
        if self._provider_failure is not None:
            failed_at, status_code = self._provider_failure
            if now - failed_at < _PROVIDER_RETRY_SECONDS:
                raise AccessUnavailableError(status_code)
        try:
            document = self._provider_json(self._http_request("GET", url))
        except AccessUnavailableError as error:
            self._provider_failure = (self._clock(), error.status_code)
            raise
        self._provider_failure = None
        return document

    def _endpoint(self, document: Mapping[str, Any], name: str, base: str) -> str:
        value = document.get(name)
        parts = urlsplit(value) if isinstance(value, str) else None
        if (
            parts is None
            or not str(value).isascii()
            or any(character.isspace() for character in str(value))
            or parts.fragment
            or _origin(parts.geturl()) not in {_origin(self._issuer), _origin(self._provider)}
        ):
            raise AccessUnavailableError(502)
        return f"{_origin(base)}{parts.path}{'?' + parts.query if parts.query else ''}"

    def _token_request(self, form: Mapping[str, str]) -> dict[str, Any]:
        """Request tokens as the confidential client; a refused grant is a refused sign-in."""
        response = self._http_request(
            "POST",
            self._discovered().token,
            data=dict(form),
            auth=(quote(self._client_id, safe=""), quote(self._client_secret, safe="")),
        )
        if 400 <= response.status_code < 500:
            raise _SignInRefusedError
        return self._provider_json(response)

    def _http_request(
        self,
        method: str,
        url: str,
        *,
        data: dict[str, str] | None = None,
        auth: tuple[str, str] | None = None,
    ) -> httpx.Response:
        try:
            return self._http.request(
                method, url, data=data, auth=auth, headers={"accept": "application/json"}
            )
        except httpx.HTTPError:
            # The error can carry provider text and the request; report the outcome only.
            raise AccessUnavailableError(503) from None

    @staticmethod
    def _provider_json(response: httpx.Response) -> dict[str, Any]:
        if response.status_code != 200 or len(response.content) > _MAXIMUM_PROVIDER_RESPONSE_BYTES:
            raise AccessUnavailableError(502)
        try:
            document = response.json()
        except ValueError:
            raise AccessUnavailableError(502) from None
        if not isinstance(document, dict):
            raise AccessUnavailableError(502)
        return document


def select_gateway_access(
    ingress: IngressPolicy,
    *,
    identity: IdentityDeployment | None,
    capability_descriptor: int | None = None,
) -> GatewayAccess:
    """Return the access method of an ingress mode; identity and its mode need each other."""
    if ingress.mode != IDENTITY_INGRESS_MODE:
        if identity is not None:
            raise IngressConfigurationError(
                "the manifest's [identity] table requires identity-proxy ingress"
            )
        if capability_descriptor is not None:
            return LaunchCapability.from_descriptor(capability_descriptor)
        return LaunchCapability.generate()
    if identity is None:
        raise IngressConfigurationError(
            "identity-proxy ingress requires the deployment manifest's [identity] table"
        )
    if capability_descriptor is not None:
        raise IngressConfigurationError(
            "identity-proxy ingress verifies the researcher's identity and accepts no "
            "capability descriptor"
        )
    return IdentityAccess.create(identity, ingress)


def _one(query: Mapping[str, list[str]], name: str) -> str:
    values = query.get(name, [])
    if len(values) != 1 or not values[0]:
        raise _SignInRefusedError
    return values[0]


def _seconds(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return float(value)


def _is_signed_token(token: str) -> bool:
    """Return whether an access token is a JSON Web Token; other tokens are opaque."""
    try:
        jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        return False
    return True


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


def _cookie_value(headers: Mapping[str, tuple[str, ...]], name: str) -> str:
    """Return the value of one request cookie, or an empty string."""
    for header in headers.get("cookie", ()):
        for item in header.split(";"):
            key, _, value = item.strip().partition("=")
            if key == name:
                return value
    return ""
