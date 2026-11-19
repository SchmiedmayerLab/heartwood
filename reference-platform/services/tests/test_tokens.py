# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from fakes import AUDIENCE, HUB, ISSUER, SigningKey, TokenReviews
from heartwood_platform.common.tokens import (
    AuthenticationError,
    Researcher,
    ResearcherTokens,
    ServiceTokens,
)


def _encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


@pytest.fixture
def key() -> SigningKey:
    return SigningKey()


@pytest.fixture
def tokens(key: SigningKey) -> ResearcherTokens:
    return ResearcherTokens(ISSUER, AUDIENCE, key.key_set)


def test_exchanged_token_yields_subject_and_project(
    key: SigningKey, tokens: ResearcherTokens
) -> None:
    assert tokens.verify(key.token()) == Researcher(subject="researcher-1", project="demo")
    assert tokens.verify(key.token(aud=[AUDIENCE])).project == "demo"


@pytest.mark.parametrize(
    "changes",
    [
        {"iss": "https://other.example.test/realms/heartwood"},
        {"exp": 1},
        {"aud": "heartwood-data"},
        {"aud": [AUDIENCE, "heartwood-data"]},
        {"aud": None},
        {"azp": "heartwood-cli"},
        {"azp": "hw-proj-"},
        {"azp": "hw-proj-demo/../other"},
        {"azp": None},
        {"sub": None},
        {"sub": ""},
        {"typ": "ID"},
        {"typ": None},
        {"iat": None},
        {"kid": "unknown"},
    ],
    ids=lambda changes: ",".join(f"{name}={value}" for name, value in changes.items()),
)
def test_token_with_a_wrong_claim_is_refused(
    key: SigningKey, tokens: ResearcherTokens, changes: dict[str, Any]
) -> None:
    with pytest.raises(AuthenticationError):
        tokens.verify(key.token(**changes))


def test_token_signed_by_another_key_is_refused(tokens: ResearcherTokens) -> None:
    with pytest.raises(AuthenticationError):
        tokens.verify(SigningKey(key_id="other").token(kid="test-key"))


def test_symmetric_and_unsigned_tokens_are_refused(
    key: SigningKey, tokens: ResearcherTokens
) -> None:
    # A token signed with HMAC under the public key, which a verifier that trusted the
    # token's own algorithm would accept.
    public = key.private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    header, payload, _ = key.token().split(".")
    header = _encode(json.dumps({"alg": "HS256", "typ": "JWT", "kid": key.key_id}).encode())
    signature = hmac.new(public, f"{header}.{payload}".encode(), hashlib.sha256).digest()

    for forged in (
        f"{header}.{payload}.{_encode(signature)}",
        key.token(algorithm="none", key=None),
        "not-a-token",
    ):
        with pytest.raises(AuthenticationError):
            tokens.verify(forged)


def test_keys_are_fetched_once_and_unknown_keys_do_not_flood_the_issuer(key: SigningKey) -> None:
    now = [0.0]
    tokens = ResearcherTokens(ISSUER, AUDIENCE, key.key_set, clock=lambda: now[0])

    tokens.verify(key.token())
    tokens.verify(key.token())
    for _ in range(5):
        with pytest.raises(AuthenticationError):
            tokens.verify(key.token(kid="unknown"))
    assert key.fetches == 1

    now[0] = 31.0
    with pytest.raises(AuthenticationError):
        tokens.verify(key.token(kid="unknown"))
    assert key.fetches == 2


def test_rotated_key_is_fetched(key: SigningKey) -> None:
    now = [0.0]
    tokens = ResearcherTokens(ISSUER, AUDIENCE, lambda: current.key_set(), clock=lambda: now[0])
    current = key
    tokens.verify(key.token())

    current = SigningKey(key_id="rotated")
    now[0] = 31.0

    assert tokens.verify(current.token()).subject == "researcher-1"


def test_listed_service_account_is_accepted() -> None:
    reviews = TokenReviews()
    services = ServiceTokens(reviews.review, AUDIENCE, frozenset({HUB}))

    assert services.verify("hub-token") == HUB


@pytest.mark.parametrize(
    "status",
    [
        {"authenticated": False},
        {"authenticated": True, "audiences": ["heartwood-data"], "user": {"username": HUB}},
        {"authenticated": True, "audiences": [AUDIENCE], "user": {"username": HUB + "-other"}},
        {"authenticated": True, "audiences": [AUDIENCE]},
        {"audiences": [AUDIENCE], "user": {"username": HUB}},
        {},
    ],
)
def test_other_service_account_tokens_are_refused(status: dict[str, Any]) -> None:
    services = ServiceTokens(lambda _token, _audience: status, AUDIENCE, frozenset({HUB}))

    with pytest.raises(AuthenticationError):
        services.verify("token")
