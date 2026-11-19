# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""OpenID Connect identity at gateway ingress, against a synthetic in-process provider."""

from __future__ import annotations

import asyncio
import base64
import datetime
import hashlib
import json
import secrets
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import jwt
import pytest
import tomli_w
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import NameOID
from pydantic import ValidationError

from heartwood.gateway import (
    GatewayAsgiApp,
    IdentityAccess,
    IdentityDeployment,
    IngressConfigurationError,
    IngressPolicy,
    LaunchCapability,
    ProjectContext,
    ProjectStateError,
    SessionGateway,
    load_project_deployment,
    select_gateway_access,
)
from heartwood.gateway._access import AccessResponse, AccessUnavailableError
from heartwood.session import CommandKind, JsonValue, SessionCommand

_ISSUER = "https://id.example.test/realms/research"
_CLIENT_ID = "hw-proj-demo"
_CLIENT_SECRET = "synthetic-client-secret-0123456789"
_SUBJECT = "researcher-7f3c"
_ORIGIN = "https://research.example.test"
_BASE = "/user/researcher/demo"
_PROXY = ("10.0.0.2", 43123)
_PROVIDER_TEXT = "synthetic provider detail"
_RSA_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER_RSA_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_EC_KEY = ec.generate_private_key(ec.SECP256R1())


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _Provider:
    """A synthetic OpenID Connect provider reached through an in-process transport."""

    def __init__(self, *, issuer: str = _ISSUER, provider_url: str | None = None) -> None:
        self.issuer = issuer
        self.base = issuer if provider_url is None else provider_url
        self.advertised = self.base
        self.key: Any = _RSA_KEY
        self.algorithm = "RS256"
        self.key_id = "key-1"
        self.subject = _SUBJECT
        self.id_claims: dict[str, Any] = {}
        self.access_claims: dict[str, Any] = {}
        self.token_fields: dict[str, Any] = {}
        self.refresh_lifetime: int | None = 1800
        self.opaque_access_tokens = False
        self.unreachable = False
        self.failing: set[str] = set()
        self.discovery: dict[str, Any] = {}
        self.requests: list[str] = []
        self.issued: list[str] = []
        self._codes: dict[str, dict[str, str]] = {}
        self._refresh_tokens: set[str] = set()

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self._handle))

    def token(self, *, header_type: str | None = None, **claims: object) -> str:
        """Sign an access token as the device flow of a public client would obtain it."""
        now = int(time.time())
        payload = {
            "iss": self.issuer,
            "sub": self.subject,
            "aud": [_CLIENT_ID, "account"],
            "azp": "heartwood-cli",
            "iat": now,
            "exp": now + 300,
            **claims,
        }
        return self._sign(
            {name: value for name, value in payload.items() if value is not None},
            header_type=header_type,
        )

    def authorize(self, location: str | None) -> str:
        """Act as the browser at the authorization endpoint and return the callback query."""
        assert location is not None
        parts = urlsplit(location)
        query = {name: values[0] for name, values in parse_qs(parts.query).items()}
        assert f"{parts.scheme}://{parts.netloc}{parts.path}" == f"{self.issuer}/authorize"
        assert query["response_type"] == "code"
        assert query["scope"] == "openid"
        assert query["client_id"] == _CLIENT_ID
        assert query["code_challenge_method"] == "S256"
        code = secrets.token_urlsafe(16)
        self._codes[code] = query
        self.issued.append(code)
        return f"code={code}&state={query['state']}&iss={self.issuer.replace(':', '%3A')}"

    def _sign(self, claims: dict[str, Any], *, header_type: str | None = None) -> str:
        headers = {"kid": self.key_id, **({} if header_type is None else {"typ": header_type})}
        token = jwt.encode(claims, self.key, algorithm=self.algorithm, headers=headers)
        self.issued.append(token)
        return token

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if self.unreachable:
            raise httpx.ConnectError(_PROVIDER_TEXT, request=request)
        assert str(request.url).startswith(self.base)
        name = request.url.path.rsplit("/", 1)[-1]
        self.requests.append(name)
        if name in self.failing:
            return httpx.Response(500, text=_PROVIDER_TEXT)
        if name == "openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": self.issuer,
                    "authorization_endpoint": f"{self.advertised}/authorize",
                    "token_endpoint": f"{self.advertised}/token",
                    "jwks_uri": f"{self.advertised}/keys",
                    **self.discovery,
                },
            )
        if name == "keys":
            algorithm = jwt.get_algorithm_by_name(self.algorithm)
            key = algorithm.to_jwk(self.key.public_key(), as_dict=True)
            return httpx.Response(200, json={"keys": [{**key, "kid": self.key_id, "use": "sig"}]})
        assert name == "token"
        return self._token_endpoint(request)

    def _token_endpoint(self, request: httpx.Request) -> httpx.Response:
        expected = base64.b64encode(f"{_CLIENT_ID}:{_CLIENT_SECRET}".encode()).decode()
        if request.headers.get("authorization") != f"Basic {expected}":
            return httpx.Response(401, json={"error": "invalid_client", "detail": _PROVIDER_TEXT})
        form = {name: values[0] for name, values in parse_qs(request.content.decode()).items()}
        refused = httpx.Response(400, json={"error": "invalid_grant", "detail": _PROVIDER_TEXT})
        nonce = None
        if form["grant_type"] == "authorization_code":
            grant = self._codes.pop(form.get("code", ""), None)
            challenge = base64.urlsafe_b64encode(
                hashlib.sha256(form.get("code_verifier", "").encode()).digest()
            )
            if (
                grant is None
                or grant["redirect_uri"] != form.get("redirect_uri")
                or grant["code_challenge"] != challenge.rstrip(b"=").decode()
            ):
                return refused
            nonce = grant["nonce"]
        elif form.get("refresh_token") in self._refresh_tokens:
            self._refresh_tokens.discard(form["refresh_token"])
        else:
            return refused
        now = int(time.time())
        claims = {"iss": self.issuer, "sub": self.subject, "iat": now, "exp": now + 300}
        refresh_token = secrets.token_urlsafe(16)
        self._refresh_tokens.add(refresh_token)
        self.issued.append(refresh_token)
        access_token = (
            secrets.token_urlsafe(24)
            if self.opaque_access_tokens
            else self._sign(
                {**claims, "aud": "account", "azp": _CLIENT_ID, "jti": secrets.token_hex(8)}
                | self.access_claims
            )
        )
        self.issued.append(access_token)
        payload: dict[str, Any] = {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": 300,
            "refresh_token": refresh_token,
            "id_token": self._sign({**claims, "aud": _CLIENT_ID, "nonce": nonce} | self.id_claims),
        }
        if self.refresh_lifetime is not None:
            payload["refresh_expires_in"] = self.refresh_lifetime
        payload |= self.token_fields
        return httpx.Response(
            200, json={name: value for name, value in payload.items() if value is not None}
        )


def _ingress(origin: str = _ORIGIN, base: str = _BASE) -> IngressPolicy:
    return IngressPolicy.create(
        mode="identity-proxy",
        bind_host="0.0.0.0",
        external_origin=origin,
        external_base_path=base,
        trusted_proxy_sources=("10.0.0.0/24",),
    )


def _deployment(tmp_path: Path, **fields: object) -> IdentityDeployment:
    return IdentityDeployment.model_validate(
        {
            "issuer": _ISSUER,
            "client_id": _CLIENT_ID,
            "client_secret_file": str(tmp_path / "client-secret"),
            "subject": _SUBJECT,
            **fields,
        }
    )


def _access(
    tmp_path: Path,
    provider: _Provider,
    *,
    clock: Callable[[], float] | None = None,
    ingress: IngressPolicy | None = None,
    **fields: object,
) -> IdentityAccess:
    return IdentityAccess(
        _deployment(tmp_path, **fields),
        _ingress() if ingress is None else ingress,
        client_secret=_CLIENT_SECRET,
        http=provider.client(),
        clock=_Clock() if clock is None else clock,
    )


def _route(
    access: IdentityAccess, path: str, query: str = "", cookie: str | None = None
) -> AccessResponse | None:
    return access.browser_response(
        method="GET",
        path=path,
        query_string=query,
        headers={} if cookie is None else {"cookie": (cookie,)},
    )


def _get(access: IdentityAccess, path: str, query: str = "") -> AccessResponse:
    response = _route(access, path, query)
    assert response is not None
    return response


def _sealed(started: AccessResponse) -> str:
    """Return the sign-in cookie a browser sends back, as a Cookie header value."""
    return started.cookies[0].split(";")[0]


def _finish(access: IdentityAccess, started: AccessResponse, query: str) -> AccessResponse:
    """Return to the callback as the browser that received ``started``."""
    response = _route(access, "/oauth_callback", query, cookie=_sealed(started))
    assert response is not None
    return response


def _sign_in(access: IdentityAccess, provider: _Provider) -> tuple[str, str]:
    """Complete one browser sign-in and return its capability and launch token."""
    started = _get(access, "/identity/login")
    assert started.status_code == 302
    completed = _finish(access, started, provider.authorize(started.location))
    assert completed.status_code == 302, completed
    launch_token = parse_qs(urlsplit(str(completed.location)).fragment)["launch"][0]
    capability = access.exchange_launch_token(launch_token)
    assert capability is not None
    return capability, launch_token


def _capability(value: str) -> dict[str, tuple[str, ...]]:
    return {"x-heartwood-capability": (value,)}


def _bearer(token: str) -> dict[str, tuple[str, ...]]:
    return {"authorization": (f"Bearer {token}",)}


def _gateway(workspace: Path) -> SessionGateway:
    workspace.mkdir(parents=True, exist_ok=True)
    return SessionGateway(project=ProjectContext(workspace), env={}, backend_id="deterministic")


def _command(kind: CommandKind, **payload: JsonValue) -> bytes:
    return (
        SessionCommand(
            command_id=f"session-1-{kind.value}",
            session_id="session-1",
            kind=kind,
            actor_id="claimed-actor",
            created_at="2026-01-01T00:00:00Z",
            payload=payload,
        )
        .model_dump_json()
        .encode()
    )


async def _call(
    app: GatewayAsgiApp,
    method: str,
    path: str,
    *,
    query: str = "",
    body: bytes = b"",
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    sent: list[dict[str, Any]] = []
    messages = iter(({"type": "http.request", "body": body, "more_body": False},))

    async def receive() -> dict[str, object]:
        return next(messages)

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    request_headers = {"host": "research.example.test", "x-forwarded-proto": "https,http"}
    request_headers |= headers or {}
    await app(
        {
            "type": "http",
            "method": method,
            "path": f"{_BASE}{path}",
            "raw_path": f"{_BASE}{path}".encode(),
            "query_string": query.encode(),
            "headers": [(name.encode(), value.encode()) for name, value in request_headers.items()],
            "client": _PROXY,
        },
        receive,
        send,
    )
    response_headers: dict[str, str] = {}
    for name, value in sent[0]["headers"]:
        # A repeated header, such as two cookies, is joined with a line break.
        repeated = response_headers.get(name.decode())
        response_headers[name.decode()] = (
            value.decode() if repeated is None else f"{repeated}\n{value.decode()}"
        )
    return sent[0]["status"], response_headers, cast(bytes, sent[1]["body"])


def _refusal(response: tuple[int, dict[str, str], bytes]) -> tuple[int, str]:
    assert _PROVIDER_TEXT.encode() not in response[2]
    return response[0], json.loads(response[2])["code"]


def test_browser_sign_in_records_the_verified_subject_and_keeps_tokens_in_memory(
    tmp_path: Path,
) -> None:
    provider = _Provider()
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<html><head></head><body>Synthetic</body></html>")
    (web / "app.js").write_text("// synthetic asset\n")
    workspace = tmp_path / "research"
    access = _access(tmp_path, provider)
    app = GatewayAsgiApp(_gateway(workspace), static_dir=web, ingress=_ingress(), access=access)
    navigation = {"sec-fetch-site": "cross-site", "sec-fetch-mode": "navigate"}

    async def scenario() -> dict[str, Any]:
        index = await _call(app, "GET", "/")
        assert index[0] == 302
        assert index[1]["location"] == f"{_ORIGIN}{_BASE}/identity/login"
        assert (await _call(app, "GET", "/app.js"))[0] == 200
        started = await _call(app, "GET", "/identity/login")
        assert started[0] == 302
        assert started[1]["cache-control"] == "no-store"
        sealed = started[1]["set-cookie"]
        assert sealed.startswith("heartwood-sign-in=")
        assert sealed.endswith(f"; Max-Age=600; Path={_BASE}; HttpOnly; SameSite=Lax; Secure")
        query = provider.authorize(started[1]["location"])
        # The callback URL alone, as another browser would open it, signs nobody in.
        assert _refusal(await _call(app, "GET", "/oauth_callback", query=query)) == (
            403,
            "HW-INGRESS-004",
        )
        callback = await _call(
            app,
            "GET",
            "/oauth_callback",
            query=query,
            headers={**navigation, "cookie": sealed.split(";")[0]},
        )
        assert callback[0] == 302
        location = urlsplit(callback[1]["location"])
        assert callback[1]["location"].startswith(f"{_ORIGIN}{_BASE}/#launch=")
        cleared, cookie = callback[1]["set-cookie"].split("\n")
        attributes = f"Path={_BASE}; HttpOnly; SameSite=Lax; Secure"
        assert cleared == f"heartwood-sign-in=; Max-Age=0; {attributes}"
        assert cookie == f"heartwood-login=1; Max-Age=60; {attributes}"
        page = await _call(app, "GET", "/", headers={**navigation, "cookie": cookie.split(";")[0]})
        assert page[0] == 200
        assert b"Synthetic" in page[2]
        # Behind a shared proxy origin, no page of that origin may frame the gateway.
        assert "frame-ancestors 'none'" in page[1]["content-security-policy"]
        assert page[1]["x-frame-options"] == "DENY"
        launch = json.dumps({"token": parse_qs(location.fragment)["launch"][0]}).encode()
        exchanged = await _call(app, "POST", "/launch", body=launch)
        assert exchanged[0] == 200
        capability = json.loads(exchanged[2])["capability"]
        assert _refusal(await _call(app, "POST", "/launch", body=launch)) == (
            403,
            "HW-INGRESS-004",
        )
        # The marker cookie alone authorizes nothing.
        assert _refusal(
            await _call(app, "GET", "/sessions", headers={"cookie": cookie.split(";")[0]})
        ) == (401, "HW-INGRESS-004")
        command = await _call(
            app,
            "POST",
            "/sessions/session-1/commands",
            body=_command(CommandKind.CHAT, prompt="Run the synthetic action"),
            headers={"x-heartwood-capability": capability},
        )
        assert command[0] == 200
        provider.issued.append(capability)
        return cast(dict[str, Any], json.loads(command[2]))

    body = asyncio.run(scenario())

    received = [event for event in body["events"] if event["kind"] == "command.received"]
    assert received[0]["payload"]["actor_id"] == _SUBJECT
    assert access.researcher_access_token() in provider.issued
    stored = b"".join(path.read_bytes() for path in workspace.rglob("*") if path.is_file())
    assert _SUBJECT.encode() in stored
    for secret in (*provider.issued, _CLIENT_SECRET):
        assert secret.encode() not in stored
        assert secret not in repr(access)
        assert secret not in repr(list(vars(access)["_logins"].values()))


@pytest.mark.parametrize(
    ("headers", "status"),
    [
        (lambda provider: _bearer(provider.token()), 200),
        (lambda provider: _bearer(provider.token(aud=_CLIENT_ID, azp=None)), 200),
        (lambda _provider: {}, 401),
        (lambda provider: {"authorization": f"Basic {provider.token()}"}, 401),
        (lambda provider: _bearer(provider.token(sub="another-researcher")), 401),
        (lambda provider: _bearer(provider.token(aud=["hw-proj-other", "account"])), 401),
        (lambda provider: _bearer(provider.token(aud=None, azp=_CLIENT_ID)), 401),
        (lambda provider: _bearer(provider.token(iss="https://id.example.test/realms/x")), 401),
        (lambda provider: _bearer(provider.token(exp=int(time.time()) - 120)), 401),
        (lambda provider: _bearer(provider.token(exp=None)), 401),
        (lambda provider: _bearer(jwt.encode(_claims(provider), _OTHER_RSA_KEY, "RS256")), 401),
        (
            lambda provider: _bearer(
                jwt.encode(_claims(provider), _OTHER_RSA_KEY, "RS256", headers={"kid": "key-1"})
            ),
            401,
        ),
        (
            lambda provider: _bearer(
                jwt.encode(_claims(provider), "", "none", headers={"kid": "key-1"})
            ),
            401,
        ),
        (
            lambda provider: _bearer(
                jwt.encode(_claims(provider), "s" * 48, "HS256", headers={"kid": "key-1"})
            ),
            401,
        ),
        (lambda _provider: _bearer("not-a-token"), 401),
        (lambda provider: _bearer(provider.token(header_type="at+jwt")), 200),
        (lambda provider: _bearer(provider.token(typ="Bearer")), 200),
        (lambda provider: _bearer(provider.token(nonce="synthetic-nonce")), 401),
        (lambda provider: _bearer(provider.token(at_hash="synthetic-hash")), 401),
        (lambda provider: _bearer(provider.token(c_hash="synthetic-hash")), 401),
        (lambda provider: _bearer(provider.token(typ="ID")), 401),
        (lambda provider: _bearer(provider.token(iat=int(time.time()) + 600)), 401),
        (lambda provider: _bearer(provider.token(nbf=int(time.time()) + 600)), 401),
        (
            lambda provider: _bearer(
                jwt.encode(_claims(provider), _EC_KEY, "ES256", headers={"kid": "key-1"})
            ),
            401,
        ),
        *(
            (
                lambda provider, algorithm=algorithm: _bearer(
                    jwt.encode(_claims(provider), _RSA_KEY, algorithm, headers={"kid": "key-1"})
                ),
                401,
            )
            for algorithm in ("PS256", "RS384", "RS512")
        ),
        (
            lambda provider: _bearer(
                jwt.encode(_claims(provider), _RSA_KEY, "RS256", headers={"kid": "key-9"})
            ),
            401,
        ),
    ],
    ids=[
        "valid",
        "valid-single-audience",
        "no-identity",
        "other-scheme",
        "other-subject",
        "other-audience",
        "authorized-party-without-audience",
        "other-issuer",
        "expired",
        "no-expiry",
        "unknown-key",
        "other-key",
        "unsigned",
        "shared-secret",
        "malformed",
        "access-token-header-type",
        "access-token-type-claim",
        "identity-token-nonce",
        "identity-token-access-hash",
        "identity-token-code-hash",
        "identity-token-type-claim",
        "issued-in-the-future",
        "not-yet-valid",
        "elliptic-curve-header-for-an-rsa-key",
        "ps256",
        "rs384",
        "rs512",
        "unknown-key-identifier",
    ],
)
def test_a_bearer_token_is_accepted_only_for_the_bound_subject_and_client(
    tmp_path: Path,
    headers: Callable[[_Provider], dict[str, Any]],
    status: int,
) -> None:
    provider = _Provider()
    app = GatewayAsgiApp(
        _gateway(tmp_path / "research"), ingress=_ingress(), access=_access(tmp_path, provider)
    )
    presented = {
        name: value[0] if isinstance(value, tuple) else value
        for name, value in headers(provider).items()
    }

    response = asyncio.run(
        _call(
            app,
            "POST",
            "/sessions/session-1/commands",
            body=_command(CommandKind.PAUSE),
            headers=presented,
        )
    )

    assert response[0] == status
    if status == 200:
        events = json.loads(response[2])["events"]
        assert events[0]["payload"]["actor_id"] == _SUBJECT
    else:
        assert _refusal(response) == (401, "HW-INGRESS-004")
        assert not (tmp_path / "research" / ".heartwood" / "sessions").exists()


def test_two_authorization_headers_and_cross_site_api_navigations_are_refused(
    tmp_path: Path,
) -> None:
    provider = _Provider()
    access = _access(tmp_path, provider)
    app = GatewayAsgiApp(_gateway(tmp_path / "research"), ingress=_ingress(), access=access)
    token = provider.token()

    assert access.principal({"authorization": (f"Bearer {token}", f"Bearer {token}")}) is None
    response = asyncio.run(
        _call(
            app,
            "GET",
            "/sessions",
            headers={"sec-fetch-site": "cross-site", "sec-fetch-mode": "navigate"},
        )
    )
    # Only the sign-in routes accept a navigation from another site.
    assert _refusal(response) == (403, "HW-INGRESS-002")
    assert _refusal(asyncio.run(_call(app, "GET", "/sessions"))) == (401, "HW-INGRESS-004")


def test_an_identity_token_of_the_browser_flow_is_no_bearer_token(tmp_path: Path) -> None:
    provider = _Provider()
    access = _access(tmp_path, provider)
    _sign_in(access, provider)
    identity_tokens = [
        token
        for token in provider.issued
        if token.count(".") == 2
        and "nonce" in jwt.decode(token, options={"verify_signature": False})
    ]

    assert identity_tokens
    assert all(access.principal(_bearer(token)) is None for token in identity_tokens)


def test_a_token_of_another_issuer_fetches_no_keys(tmp_path: Path) -> None:
    provider = _Provider()
    access = _access(tmp_path, provider)

    assert access.principal(_bearer(provider.token(iss="https://other.example.test"))) is None
    assert provider.requests == []


def test_an_unavailable_provider_is_asked_again_only_after_a_pause(tmp_path: Path) -> None:
    provider = _Provider()
    provider.failing = {"openid-configuration"}
    clock = _Clock()
    access = _access(tmp_path, provider, clock=clock)
    token = provider.token()

    for _ in range(50):
        assert _get(access, "/identity/login").status_code == 502
        with pytest.raises(AccessUnavailableError):
            access.principal(_bearer(token))
    assert provider.requests == ["openid-configuration"]

    clock.now += 5
    assert _get(access, "/identity/login").status_code == 502
    assert provider.requests == ["openid-configuration"] * 2
    # A failing key endpoint is remembered in the same way, and recovery is noticed.
    provider.failing = {"keys"}
    clock.now += 5
    for _ in range(50):
        with pytest.raises(AccessUnavailableError):
            access.principal(_bearer(token))
    assert provider.requests.count("keys") == 1
    assert _get(access, "/identity/login").status_code == 302
    provider.failing = set()
    clock.now += 5
    assert access.principal(_bearer(token)) == _SUBJECT


def test_the_callback_requires_the_cookie_of_the_browser_that_started_the_sign_in(
    tmp_path: Path,
) -> None:
    provider = _Provider()
    access = _access(tmp_path, provider)
    started = _get(access, "/identity/login")
    other = _get(access, "/identity/login")
    query = provider.authorize(started.location)
    name, _, value = _sealed(started).partition("=")
    tampered = f"{name}={value[:-8]}{'A' * 8}"
    foreign = _access(tmp_path, provider)

    for cookie in (None, _sealed(other), tampered, f"{name}=", f"{name}=not-sealed", "other=1"):
        refused = _route(access, "/oauth_callback", query, cookie=cookie)
        assert refused is not None
        assert (refused.status_code, refused.check_id) == (403, "gateway-identity")
    # Another gateway process, with another key, does not accept the cookie either.
    assert _finish(foreign, started, query).status_code == 403
    assert provider.requests.count("token") == 0

    assert _finish(access, started, query).status_code == 302
    assert _finish(access, started, query).status_code == 403
    assert provider.requests.count("token") == 1


def test_used_states_are_bounded_and_forgotten_when_they_expire(tmp_path: Path) -> None:
    provider = _Provider()
    clock = _Clock()
    access = _access(tmp_path, provider, clock=clock)
    for _ in range(1030):
        started = _get(access, "/identity/login")
        _finish(
            access, started, "state=" + parse_qs(urlsplit(str(started.location)).query)["state"][0]
        )

    assert len(vars(access)["_used_states"]) == 1024
    clock.now += 600
    _sign_in(access, provider)
    assert len(vars(access)["_used_states"]) == 1


def _claims(provider: _Provider) -> dict[str, Any]:
    now = int(time.time())
    return {
        "iss": provider.issuer,
        "sub": _SUBJECT,
        "aud": [_CLIENT_ID],
        "iat": now,
        "exp": now + 300,
    }


def test_an_elliptic_curve_provider_key_verifies(tmp_path: Path) -> None:
    provider = _Provider()
    provider.key, provider.algorithm = _EC_KEY, "ES256"
    access = _access(tmp_path, provider)

    assert access.principal(_bearer(provider.token())) == _SUBJECT
    assert access.principal(_capability(_sign_in(access, provider)[0])) == _SUBJECT


def test_sign_in_request_binds_state_nonce_and_a_code_challenge(tmp_path: Path) -> None:
    provider = _Provider()
    access = _access(tmp_path, provider)

    first = parse_qs(urlsplit(str(_get(access, "/identity/login").location)).query)
    second = parse_qs(urlsplit(str(_get(access, "/identity/login").location)).query)

    assert first["redirect_uri"] == [f"{_ORIGIN}{_BASE}/oauth_callback"]
    assert set(first) == {
        "response_type",
        "scope",
        "client_id",
        "redirect_uri",
        "state",
        "nonce",
        "code_challenge",
        "code_challenge_method",
    }
    for name in ("state", "nonce", "code_challenge"):
        assert len(first[name][0]) >= 43
        assert first[name] != second[name]
    assert (
        access.browser_response(method="POST", path="/identity/login", query_string="", headers={})
        is None
    )
    assert access.launch_url(_ingress()) is None


def _other_subject(provider: _Provider) -> None:
    provider.subject = "another-researcher"


def _other_access_subject(provider: _Provider) -> None:
    provider.access_claims = {"sub": "another-researcher"}


@pytest.mark.parametrize(
    "tamper",
    [
        _other_subject,
        _other_access_subject,
        lambda provider: provider.id_claims.update(nonce="another-nonce"),
        lambda provider: provider.id_claims.update(nonce=None),
        lambda provider: provider.id_claims.update(aud="hw-proj-other"),
        lambda provider: provider.id_claims.update(aud=[_CLIENT_ID, "other"]),
        lambda provider: provider.id_claims.update(aud=[_CLIENT_ID, "other"], azp="other"),
        lambda provider: provider.id_claims.update(azp="other"),
        lambda provider: provider.id_claims.update(iss="https://id.example.test/realms/x"),
        lambda provider: provider.id_claims.update(exp=int(time.time()) - 120),
        lambda provider: provider.id_claims.update(iat=None),
        lambda provider: provider.access_claims.update(aud="account", azp="other"),
        lambda provider: setattr(provider, "key", _OTHER_RSA_KEY),
    ],
    ids=[
        "other-subject",
        "access-token-of-another-subject",
        "other-nonce",
        "no-nonce",
        "other-audience",
        "several-audiences-without-authorized-party",
        "several-audiences-for-another-party",
        "other-authorized-party",
        "other-issuer",
        "expired",
        "no-issue-time",
        "access-token-of-another-client",
        "other-key",
    ],
)
def test_sign_in_is_refused_unless_every_token_check_passes(
    tmp_path: Path, tamper: Callable[[_Provider], object]
) -> None:
    provider = _Provider()
    access = _access(tmp_path, provider)
    # The keys are cached before the provider changes, as in a running gateway.
    assert access.principal(_bearer(provider.token())) == _SUBJECT
    started = _get(access, "/identity/login")
    tamper(provider)

    completed = _finish(access, started, provider.authorize(started.location))

    assert (completed.status_code, completed.check_id) == (403, "gateway-identity")
    assert completed.location is None
    assert completed.cookies == ()
    assert access.researcher_access_token() is None


def test_an_authorized_party_with_several_audiences_signs_in(tmp_path: Path) -> None:
    provider = _Provider()
    provider.id_claims = {"aud": [_CLIENT_ID, "account"], "azp": _CLIENT_ID}
    provider.access_claims = {"aud": [_CLIENT_ID, "account"], "azp": "other"}
    access = _access(tmp_path, provider)

    assert access.principal(_capability(_sign_in(access, provider)[0])) == _SUBJECT


def test_an_opaque_access_token_signs_in(tmp_path: Path) -> None:
    provider = _Provider()
    provider.opaque_access_tokens = True
    access = _access(tmp_path, provider)

    capability, _ = _sign_in(access, provider)

    assert access.principal(_capability(capability)) == _SUBJECT
    assert access.researcher_access_token() in provider.issued


@pytest.mark.parametrize(
    "query",
    [
        lambda query: query.replace("state=", "state=x"),
        lambda query: query.replace("code=", "code=x"),
        lambda query: query.split("&", 1)[1],
        lambda query: query.split("&")[0],
        lambda query: f"{query}&error=access_denied",
        lambda query: query.rsplit("&", 1)[0] + "&iss=https%3A%2F%2Fother.example.test",
        lambda query: f"{query}&state=second",
        lambda query: "&".join(f"extra{index}=1" for index in range(20)) + f"&{query}",
    ],
    ids=[
        "unknown-state",
        "unknown-code",
        "no-code",
        "no-state",
        "provider-error",
        "other-issuer",
        "repeated-state",
        "too-many-fields",
    ],
)
def test_callback_refuses_unexpected_queries(tmp_path: Path, query: Callable[[str], str]) -> None:
    provider = _Provider()
    access = _access(tmp_path, provider)
    started = _get(access, "/identity/login")

    completed = _finish(access, started, query(provider.authorize(started.location)))

    assert (completed.status_code, completed.check_id) == (403, "gateway-identity")


def test_state_is_single_use_and_expires(tmp_path: Path) -> None:
    provider = _Provider()
    clock = _Clock()
    access = _access(tmp_path, provider, clock=clock)
    started = _get(access, "/identity/login")
    query = provider.authorize(started.location)
    assert _finish(access, started, query).status_code == 302

    assert _finish(access, started, query).status_code == 403
    # A second code for the consumed state is refused as well.
    assert _finish(access, started, provider.authorize(started.location)).status_code == 403

    late = _get(access, "/identity/login")
    clock.now += 601
    assert _finish(access, late, provider.authorize(late.location)).status_code == 403


def test_a_code_obtained_for_another_verifier_is_refused(tmp_path: Path) -> None:
    provider = _Provider()
    access = _access(tmp_path, provider)
    started = _get(access, "/identity/login")
    first = provider.authorize(started.location)
    second = provider.authorize(_get(access, "/identity/login").location)
    swapped = "&".join((second.split("&")[0], *first.split("&")[1:]))

    assert _finish(access, started, swapped).status_code == 403


def test_sign_ins_in_progress_hold_no_state_and_live_sign_ins_are_bounded(
    tmp_path: Path,
) -> None:
    provider = _Provider()
    access = _access(tmp_path, provider)
    oldest = _get(access, "/identity/login")
    before = {name: repr(value) for name, value in vars(access).items()}
    for _ in range(2000):
        _get(access, "/identity/login")

    # Requests without identity leave nothing behind and cannot end another sign-in.
    assert {name: repr(value) for name, value in vars(access).items()} == before
    assert _finish(access, oldest, provider.authorize(oldest.location)).status_code == 302

    capabilities = [_sign_in(access, provider)[0] for _ in range(8)]
    assert access.principal(_capability(capabilities[0])) == _SUBJECT
    assert access.principal(_capability(capabilities[-1])) == _SUBJECT
    capabilities.append(_sign_in(access, provider)[0])
    assert access.principal(_capability(capabilities[0])) is None
    assert access.principal(_capability(capabilities[-1])) == _SUBJECT


def test_launch_token_is_single_use_and_short_lived(tmp_path: Path) -> None:
    provider = _Provider()
    clock = _Clock()
    access = _access(tmp_path, provider, clock=clock)
    _capability_value, launch_token = _sign_in(access, provider)
    assert access.exchange_launch_token(launch_token) is None
    assert access.exchange_launch_token("unknown") is None

    started = _get(access, "/identity/login")
    completed = _finish(access, started, provider.authorize(started.location))
    clock.now += 61
    assert (
        access.exchange_launch_token(
            parse_qs(urlsplit(str(completed.location)).fragment)["launch"][0]
        )
        is None
    )


@pytest.mark.parametrize(("refresh_lifetime", "ends_after"), [(1800, 1800), (None, 300), (0, 300)])
def test_a_sign_in_ends_with_its_refresh_token_or_its_access_token(
    tmp_path: Path, refresh_lifetime: int | None, ends_after: int
) -> None:
    provider = _Provider()
    provider.refresh_lifetime = refresh_lifetime
    clock = _Clock()
    access = _access(tmp_path, provider, clock=clock)
    capability, _ = _sign_in(access, provider)

    clock.now += ends_after - 1
    assert access.principal(_capability(capability)) == _SUBJECT
    clock.now += 1
    assert access.principal(_capability(capability)) is None
    assert access.principal(_capability("\ud800")) is None
    assert access.researcher_access_token() is None


def test_the_researcher_access_token_is_refreshed_before_it_expires(tmp_path: Path) -> None:
    provider = _Provider()
    clock = _Clock()
    access = _access(tmp_path, provider, clock=clock)
    assert access.researcher_access_token() is None
    capability, _ = _sign_in(access, provider)
    first = access.researcher_access_token()
    assert first is not None

    clock.now += 269
    assert access.researcher_access_token() == first
    clock.now += 2
    second = access.researcher_access_token()
    assert second not in {None, first}
    assert access.researcher_access_token() == second
    assert provider.requests.count("token") == 2
    # The refreshed sign-in lasts for the new refresh token's lifetime.
    clock.now += 1799
    assert access.principal(_capability(capability)) == _SUBJECT


def test_a_refresh_without_a_new_refresh_token_keeps_the_old_one(tmp_path: Path) -> None:
    provider = _Provider()
    clock = _Clock()
    access = _access(tmp_path, provider, clock=clock)
    _sign_in(access, provider)
    provider.token_fields = {"refresh_token": None}
    clock.now += 280

    assert access.researcher_access_token() is not None
    assert vars(access)["_logins"].popitem()[1].refresh_token is not None


def test_a_refused_or_unavailable_refresh_never_returns_a_stale_token(tmp_path: Path) -> None:
    provider = _Provider()
    clock = _Clock()
    access = _access(tmp_path, provider, clock=clock)
    capability, _ = _sign_in(access, provider)
    clock.now += 280

    provider.unreachable = True
    with pytest.raises(AccessUnavailableError):
        access.researcher_access_token()
    provider.unreachable = False
    assert access.principal(_capability(capability)) == _SUBJECT

    provider._refresh_tokens.clear()
    assert access.researcher_access_token() is None
    assert access.principal(_capability(capability)) is None


def test_a_sign_in_without_a_refresh_token_keeps_its_access_token(tmp_path: Path) -> None:
    provider = _Provider()
    provider.token_fields = {"refresh_token": None}
    clock = _Clock()
    access = _access(tmp_path, provider, clock=clock)
    _sign_in(access, provider)
    clock.now += 290

    assert access.researcher_access_token() in provider.issued
    assert provider.requests.count("token") == 1


def test_keys_are_fetched_again_for_an_unknown_key_at_a_bounded_rate(tmp_path: Path) -> None:
    provider = _Provider()
    clock = _Clock()
    access = _access(tmp_path, provider, clock=clock)
    assert access.principal(_bearer(provider.token())) == _SUBJECT
    provider.key, provider.key_id = _OTHER_RSA_KEY, "key-2"
    rotated = provider.token()

    assert access.principal(_bearer(rotated)) is None
    assert access.principal(_bearer(rotated)) is None
    assert provider.requests.count("keys") == 1
    clock.now += 30
    assert access.principal(_bearer(rotated)) == _SUBJECT
    assert provider.requests.count("keys") == 2
    assert provider.requests.count("openid-configuration") == 1


def test_the_gateway_reaches_the_provider_at_its_own_url(tmp_path: Path) -> None:
    internal = "http://provider.identity.svc:8080/realms/research"
    provider = _Provider(provider_url=internal)
    # The provider advertises its browser-facing endpoints.
    provider.advertised = _ISSUER
    access = _access(tmp_path, provider, provider_url=internal, allow_plain_http=True)

    started = _get(access, "/identity/login")

    assert str(started.location).startswith(f"{_ISSUER}/authorize?")
    assert access.principal(_capability(_sign_in(access, provider)[0])) == _SUBJECT
    assert provider.requests == ["openid-configuration", "token", "keys"]


@pytest.mark.parametrize(
    ("discovery", "status"),
    [
        ({"issuer": "https://id.example.test/realms/other"}, 502),
        ({"token_endpoint": "https://elsewhere.example.test/token"}, 502),
        ({"jwks_uri": "https://elsewhere.example.test/keys"}, 502),
        ({"authorization_endpoint": "https://elsewhere.example.test/authorize"}, 502),
        ({"authorization_endpoint": f"{_ISSUER}/authorize#fragment"}, 502),
        ({"authorization_endpoint": f"{_ISSUER}/authorize?tenant=r\u00e9search"}, 502),
        ({"authorization_endpoint": f"{_ISSUER}/authorize?tenant=a b"}, 502),
        ({"token_endpoint": None}, 502),
    ],
)
def test_discovery_that_leaves_the_configured_origins_is_refused(
    tmp_path: Path, discovery: dict[str, Any], status: int
) -> None:
    provider = _Provider()
    provider.discovery = discovery
    access = _access(tmp_path, provider)

    started = _get(access, "/identity/login")

    assert (started.status_code, started.check_id) == (status, "gateway-identity-provider")
    assert started.location is None


def test_an_authorization_endpoint_keeps_its_own_query(tmp_path: Path) -> None:
    provider = _Provider()
    provider.discovery = {"authorization_endpoint": f"{_ISSUER}/authorize?tenant=research"}
    access = _access(tmp_path, provider)

    location = str(_get(access, "/identity/login").location)

    assert location.startswith(f"{_ISSUER}/authorize?tenant=research&response_type=code&")


@pytest.mark.parametrize(
    ("failure", "status"),
    [
        (lambda provider: setattr(provider, "unreachable", True), 503),
        (lambda provider: provider.failing.add("token"), 502),
        (lambda provider: provider.token_fields.update(id_token=None), 502),
        (lambda provider: provider.token_fields.update(access_token=None), 502),
        (lambda provider: provider.token_fields.update(expires_in=True), 502),
        (lambda provider: provider.token_fields.update(token_type="mac"), 502),
        (lambda provider: provider.failing.add("keys"), 502),
    ],
    ids=[
        "unreachable",
        "token-endpoint-error",
        "no-id-token",
        "no-access-token",
        "no-lifetime",
        "other-token-type",
        "key-endpoint-error",
    ],
)
def test_provider_failures_during_sign_in_report_a_stable_diagnostic(
    tmp_path: Path, failure: Callable[[_Provider], object], status: int
) -> None:
    provider = _Provider()
    access = _access(tmp_path, provider)
    started = _get(access, "/identity/login")
    query = provider.authorize(started.location)
    failure(provider)

    completed = _finish(access, started, query)

    assert (completed.status_code, completed.check_id) == (status, "gateway-identity-provider")
    assert access.researcher_access_token() is None


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="not json"),
        httpx.Response(200, json=["not", "an", "object"]),
        httpx.Response(200, content=b" " * 1_048_577),
        httpx.Response(302, headers={"location": "https://elsewhere.example.test"}),
        httpx.Response(200, json={"keys": []}),
    ],
)
def test_unusable_provider_documents_are_reported_without_their_text(
    tmp_path: Path, response: httpx.Response
) -> None:
    provider = _Provider()
    access = IdentityAccess(
        _deployment(tmp_path),
        _ingress(),
        client_secret=_CLIENT_SECRET,
        http=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: (
                    provider._handle(request)
                    if request.url.path.endswith("openid-configuration")
                    and response.status_code == 200
                    and b"keys" in response.content
                    else response
                )
            )
        ),
    )

    with pytest.raises(AccessUnavailableError) as error:
        access.principal(_bearer(provider.token()))

    assert error.value.status_code == 502
    assert "elsewhere" not in str(error.value)


def test_requests_report_an_unavailable_provider_without_its_text(tmp_path: Path) -> None:
    provider = _Provider()
    provider.unreachable = True
    gateway = _gateway(tmp_path / "research")
    app = GatewayAsgiApp(gateway, ingress=_ingress(), access=_access(tmp_path, provider))
    token = provider.token()

    async def scenario() -> list[dict[str, Any]]:
        for path in ("/sessions", "/identity/login", "/sessions/session-1/events/stream"):
            response = await _call(app, "GET", path, headers={"authorization": f"Bearer {token}"})
            assert _refusal(response) == (503, "HW-INGRESS-005")
        return await _socket(app, headers={"authorization": f"Bearer {token}"})

    assert asyncio.run(scenario()) == [
        {"type": "websocket.accept"},
        {"type": "websocket.close", "code": 1013},
    ]
    assert not (tmp_path / "research" / ".heartwood").exists()


async def _socket(
    app: GatewayAsgiApp, *, headers: dict[str, str], frames: tuple[str, ...] = ()
) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []
    messages: list[dict[str, object]] = [{"type": "websocket.connect"}]
    messages.extend({"type": "websocket.receive", "text": frame} for frame in frames)

    async def receive() -> dict[str, object]:
        return messages.pop(0) if messages else {"type": "websocket.disconnect"}

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    request_headers = {"host": "research.example.test", "origin": _ORIGIN, **headers}
    path = f"{_BASE}/sessions/session-1/events"
    await app(
        {
            "type": "websocket",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [(name.encode(), value.encode()) for name, value in request_headers.items()],
            "client": _PROXY,
        },
        receive,
        send,
    )
    return sent


def test_event_streams_accept_the_sign_in_capability_and_a_bearer_token(tmp_path: Path) -> None:
    provider = _Provider()
    access = _access(tmp_path, provider)
    gateway = _gateway(tmp_path / "research")
    app = GatewayAsgiApp(gateway, ingress=_ingress(), access=access)
    capability, _ = _sign_in(access, provider)
    frame = json.dumps({"type": "authenticate", "capability": capability})

    async def scenario() -> list[list[dict[str, Any]]]:
        try:
            return [
                await _socket(app, headers={}, frames=(frame,)),
                await _socket(app, headers={"authorization": f"Bearer {provider.token()}"}),
                await _socket(app, headers={}, frames=(frame.replace(capability, "x" * 43),)),
                await _socket(app, headers={"authorization": "Bearer invalid"}),
            ]
        finally:
            gateway.stop()

    accepted_frame, accepted_bearer, refused_frame, refused_bearer = asyncio.run(scenario())

    for accepted in (accepted_frame, accepted_bearer):
        assert any(message["type"] == "websocket.send" for message in accepted)
    for refused in (refused_frame, refused_bearer):
        assert refused == [{"type": "websocket.accept"}, {"type": "websocket.close", "code": 1008}]


def test_the_index_document_is_served_only_after_the_sign_in_marker(tmp_path: Path) -> None:
    provider = _Provider()
    ingress = _ingress(origin="http://research.example.test", base="/")
    access = _access(tmp_path, provider, ingress=ingress)

    assert _get(access, "/").location == "http://research.example.test/identity/login"
    assert _route(access, "/", cookie="other=1; heartwood-login=1") is None
    assert _route(access, "/", cookie="heartwood-login=0") == _get(access, "/")
    assert _route(access, "/app.js") is None
    assert _route(access, "/sessions") is None
    started = _get(access, "/identity/login")
    assert "redirect_uri=http%3A%2F%2Fresearch.example.test%2Foauth_callback" in str(
        started.location
    )
    completed = _finish(access, started, provider.authorize(started.location))
    assert str(completed.location).startswith("http://research.example.test/#launch=")
    assert completed.cookies == (
        "heartwood-sign-in=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax",
        "heartwood-login=1; Max-Age=60; Path=/; HttpOnly; SameSite=Lax",
    )
    assert started.cookies[0].endswith("; Max-Age=600; Path=/; HttpOnly; SameSite=Lax")


def _write_secret(tmp_path: Path, content: str = f"{_CLIENT_SECRET}\n") -> Path:
    path = tmp_path / "client-secret"
    path.write_text(content)
    path.chmod(0o600)
    return path


def test_identity_and_its_ingress_mode_require_each_other(tmp_path: Path) -> None:
    _write_secret(tmp_path)
    identity = _deployment(tmp_path)
    loopback = IngressPolicy.create()

    assert isinstance(select_gateway_access(loopback, identity=None), LaunchCapability)
    assert isinstance(select_gateway_access(_ingress(), identity=identity), IdentityAccess)
    with pytest.raises(IngressConfigurationError, match=r"requires identity-proxy ingress"):
        select_gateway_access(loopback, identity=identity)
    with pytest.raises(IngressConfigurationError, match=r"requires the deployment manifest"):
        select_gateway_access(_ingress(), identity=None)
    with pytest.raises(IngressConfigurationError, match=r"accepts no capability descriptor"):
        select_gateway_access(_ingress(), identity=identity, capability_descriptor=3)
    with pytest.raises(IngressConfigurationError, match=r"requires identity-proxy ingress"):
        IdentityAccess(identity, loopback, client_secret=_CLIENT_SECRET, http=_Provider().client())
    with pytest.raises(IngressConfigurationError, match=r"ASCII"):
        _access(tmp_path, _Provider(), ingress=_ingress(base="/user/r\u00e9searcher"))
    gateway = _gateway(tmp_path / "research")
    for ingress, access in (
        (_ingress(), LaunchCapability.generate()),
        (loopback, _access(tmp_path, _Provider())),
    ):
        with pytest.raises(IngressConfigurationError, match=r"require each other"):
            GatewayAsgiApp(gateway, ingress=ingress, access=access)


def _certificate(path: Path) -> None:
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "synthetic authority")])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(_EC_KEY.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(_EC_KEY, hashes.SHA256())
    )
    path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))


def test_access_reads_the_client_secret_and_certificate_authority(tmp_path: Path) -> None:
    secret = _write_secret(tmp_path)
    authority = tmp_path / "ca.pem"
    _certificate(authority)
    loopback_provider = {"issuer": "http://127.0.0.1:9/realms/research"}

    access = IdentityAccess.create(
        _deployment(tmp_path, ca_file=str(authority), **loopback_provider), _ingress()
    )
    # Nothing listens on the discard port, so the provider is reported as unavailable.
    assert _get(access, "/identity/login").status_code == 503
    assert vars(access)["_client_secret"] == _CLIENT_SECRET

    authority.write_text("not a certificate")
    with pytest.raises(IngressConfigurationError, match=r"unavailable") as error:
        IdentityAccess.create(_deployment(tmp_path, ca_file=str(authority)), _ingress())
    assert _CLIENT_SECRET not in str(error.value)
    for content in ("", "two words", "café-secret"):
        secret.write_text(content)
        with pytest.raises(IngressConfigurationError, match=r"printable ASCII") as error:
            IdentityAccess.create(_deployment(tmp_path), _ingress())
        assert content == "" or content not in str(error.value)
    secret.unlink()
    with pytest.raises(IngressConfigurationError, match=r"unavailable"):
        IdentityAccess.create(_deployment(tmp_path), _ingress())


@pytest.mark.parametrize(
    "fields",
    [
        {"issuer": "http://id.example.test/realms/research"},
        {"provider_url": "http://provider.identity.svc:8080/realms/research"},
        {"issuer": "ftp://id.example.test"},
        {"issuer": "https://user:password@id.example.test"},
        {"issuer": "https://id.example.test/realms/research?tenant=1"},
        {"issuer": "https://id.example.test/realms/research#fragment"},
        {"issuer": "https://id.example.test:port"},
        {"issuer": "https://id.example.test/realms/re search"},
        {"issuer": ""},
        {"provider_url": ""},
        {"client_id": ""},
        {"client_id": "two words"},
        {"subject": ""},
        {"subject": "line\nbreak"},
        {"subject": "auth0|synthetic"},
        {"subject": "researcher@example.test"},
        {"subject": "s" * 129},
        {"subject": "gateway"},
        {"subject": "human"},
        {"client_secret_file": "relative/secret"},
        {"ca_file": "relative/ca.pem"},
        {"allow_plain_http": "true"},
        {"client_secret": _CLIENT_SECRET},
    ],
)
def test_identity_table_rejects_unsafe_or_malformed_fields(
    tmp_path: Path, fields: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError):
        _deployment(tmp_path, **fields)


def test_identity_table_accepts_loopback_and_explicit_plain_http(tmp_path: Path) -> None:
    internal = "http://provider.identity.svc:8080/realms/research"

    assert _deployment(tmp_path).provider == _ISSUER
    assert _deployment(tmp_path, issuer="http://localhost:8080/realms/research").provider
    plain = _deployment(tmp_path, provider_url=internal, allow_plain_http=True)
    assert plain.provider == internal
    assert "client-secret" not in repr(plain)


def _manifest(tmp_path: Path, identity: dict[str, Any]) -> tuple[Path, Path]:
    workspace = tmp_path / "research"
    workspace.mkdir(exist_ok=True)
    state_parent = tmp_path / "protected"
    state_parent.mkdir(mode=0o700)
    project_id = str(uuid4())
    path = tmp_path / "deployment.toml"
    path.write_text(
        tomli_w.dumps(
            {
                "schema_version": "heartwood.project-deployment.v1",
                "project_id": project_id,
                "workspace": str(workspace),
                "state_root": str(state_parent / project_id),
                "policy": {"policy_id": "deployment", "platform_id": "generic"},
                "identity": identity,
            }
        )
    )
    path.chmod(0o600)
    return path, workspace


def _identity_table(tmp_path: Path, **fields: object) -> dict[str, Any]:
    return {
        "issuer": _ISSUER,
        "client_id": _CLIENT_ID,
        "client_secret_file": str(_write_secret(tmp_path)),
        "subject": _SUBJECT,
        **fields,
    }


def test_manifest_binds_the_identity_table(tmp_path: Path) -> None:
    authority = tmp_path / "ca.pem"
    _certificate(authority)
    manifest, workspace = _manifest(tmp_path, _identity_table(tmp_path, ca_file=str(authority)))

    deployment = load_project_deployment(manifest, project_root=workspace, env={})

    assert deployment.identity is not None
    assert deployment.identity.subject == _SUBJECT
    assert deployment.identity.ca_file == authority
    assert not deployment.project.state_root.exists()


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda table, _path: table.update(issuer="http://id.example.test"), "identity table"),
        (
            lambda table, _path: table.update(client_secret=_CLIENT_SECRET),
            "identity table: client_secret",
        ),
        (lambda table, _path: table.pop("subject"), "identity table: subject"),
        (lambda _table, path: (path / "client-secret").chmod(0o640), "owner-only"),
        (lambda _table, path: (path / "client-secret").chmod(0o604), "owner-only"),
        (lambda _table, path: (path / "client-secret").chmod(0o622), "write-protected"),
        (lambda _table, path: (path / "client-secret").unlink(), "unavailable or invalid"),
        (
            lambda table, path: table.update(client_secret_file=str(_link(path))),
            "outside project files",
        ),
        (
            lambda table, path: table.update(client_secret_file=str(_inside(path))),
            "outside project files",
        ),
        (lambda table, path: table.update(ca_file=str(path / "missing.pem")), "unavailable"),
    ],
)
def test_manifest_refuses_an_unsafe_identity_table(
    tmp_path: Path, change: Callable[[dict[str, Any], Path], object], message: str
) -> None:
    table = _identity_table(tmp_path)
    (tmp_path / "research").mkdir()
    change(table, tmp_path)
    manifest, workspace = _manifest(tmp_path, table)

    with pytest.raises(ProjectStateError, match=message) as error:
        load_project_deployment(manifest, project_root=workspace, env={})

    assert "HW-DEPLOY" in str(error.value)
    assert _CLIENT_SECRET not in str(error.value)


def _link(path: Path) -> Path:
    link = path / "linked-secret"
    link.symlink_to(path / "client-secret")
    return link


def _inside(path: Path) -> Path:
    inside = path / "research" / "client-secret"
    inside.write_text(_CLIENT_SECRET)
    inside.chmod(0o600)
    return inside
