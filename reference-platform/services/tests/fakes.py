# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""In-process stand-ins for Keycloak and the Kubernetes TokenReview API."""

from __future__ import annotations

import base64
import functools
import json
import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

ISSUER = "https://keycloak.example.test/realms/heartwood"
AUDIENCE = "heartwood-control"
HUB = "system:serviceaccount:heartwood-sessions:hub"


def _number(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


@functools.cache
def _private_key(key_id: str) -> rsa.RSAPrivateKey:
    """Generate one key for each identifier, because key generation is slow."""
    del key_id
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@dataclass
class SigningKey:
    """An RSA key that signs test tokens and publishes itself as a key set."""

    key_id: str = "test-key"
    fetches: int = 0

    @property
    def private(self) -> rsa.RSAPrivateKey:
        return _private_key(self.key_id)

    def key_set(self) -> dict[str, Any]:
        self.fetches += 1
        numbers = self.private.public_key().public_numbers()
        key = {"kty": "RSA", "use": "sig", "alg": "RS256", "kid": self.key_id}
        return {"keys": [{**key, "n": _number(numbers.n), "e": _number(numbers.e)}]}

    def token(self, **changes: Any) -> str:  # noqa: ANN401
        """Sign an exchanged access token, with claims replaced or removed by None."""
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": "researcher-1",
            "azp": "hw-proj-demo",
            "typ": "Bearer",
            "iat": now,
            "exp": now + 300,
        }
        headers = {"kid": changes.pop("kid", self.key_id)}
        algorithm = changes.pop("algorithm", "RS256")
        key = changes.pop("key", self.private)
        claims.update(changes)
        claims = {name: value for name, value in claims.items() if value is not None}
        return jwt.encode(claims, key, algorithm=algorithm, headers=headers)


class TokenReviews:
    """Answers TokenReviews for a fixed set of service account tokens."""

    def __init__(self) -> None:
        self.tokens: dict[str, dict[str, Any]] = {
            "hub-token": {
                "authenticated": True,
                "audiences": [AUDIENCE],
                "user": {"username": HUB},
            }
        }

    def review(self, token: str, audience: str) -> dict[str, Any]:
        status = self.tokens.get(token, {"authenticated": False})
        if audience not in status.get("audiences", [audience]):
            return {"authenticated": False}
        return status


class FakeKeycloak:
    """The part of the Keycloak token and admin API that the control service calls.

    Like Keycloak, an update replaces the whole client representation.
    """

    def __init__(self, secret: str = "control-secret") -> None:
        self.secret = secret
        self.clients: dict[str, dict[str, Any]] = {}
        self.scopes = {f"scope-{name}": name for name in ("svc-control", "svc-data", "svc-jobs")}
        self.token_requests: list[dict[str, str]] = []
        self.requests: list[tuple[str, str]] = []
        self.update_delay = 0.0
        self._lock = threading.Lock()

    def client(self) -> httpx.Client:
        transport = httpx.MockTransport(self._handle)
        return httpx.Client(transport=transport, base_url="https://keycloak.example.test")

    def by_client_id(self, client_id: str) -> dict[str, Any]:
        (found,) = [c for c in self.clients.values() if c["clientId"] == client_id]
        return found

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/realms/heartwood/protocol/openid-connect/token":
            form = dict(httpx.QueryParams(request.content.decode()))
            self.token_requests.append(form)
            if form.get("client_secret") != self.secret or form.get("scope") != "kc-admin-api":
                return httpx.Response(401, json={"error": "unauthorized_client"})
            token = f"admin-token-{len(self.token_requests)}"
            return httpx.Response(200, json={"access_token": token, "expires_in": 60})
        if not request.headers.get("Authorization", "").startswith("Bearer admin-token-"):
            return httpx.Response(401)
        self.requests.append((request.method, path))
        parts = path.removeprefix("/admin/realms/heartwood/").split("/")
        body = json.loads(request.content) if request.content else None
        return self._admin(request.method, parts, request.url.params, body)

    def _admin(
        self,
        method: str,
        parts: list[str],
        query: httpx.QueryParams,
        body: Any,  # noqa: ANN401
    ) -> httpx.Response:
        if parts == ["client-scopes"]:
            scopes = [{"id": key, "name": name} for key, name in self.scopes.items()]
            return httpx.Response(200, json=scopes)
        if parts == ["clients"] and method == "GET":
            wanted = query.get("clientId")
            with self._lock:
                found = [dict(c) for c in self.clients.values() if c["clientId"] == wanted]
            return httpx.Response(200, json=found)
        if parts == ["clients"] and method == "POST":
            with self._lock:
                if any(c["clientId"] == body["clientId"] for c in self.clients.values()):
                    return httpx.Response(409)
                identifier = f"uuid-{len(self.clients) + 1}"
                self.clients[identifier] = {**body, "id": identifier, "optionalClientScopes": []}
            return httpx.Response(201)
        identifier = parts[1]
        if identifier not in self.clients:
            return httpx.Response(404)
        if len(parts) == 2 and method == "PUT":
            time.sleep(self.update_delay)
            with self._lock:
                self.clients[identifier] = body
            return httpx.Response(204)
        if parts[2:] == ["client-secret"]:
            return httpx.Response(200, json={"type": "secret", "value": f"secret-of-{identifier}"})
        if parts[2] == "optional-client-scopes" and method == "PUT":
            with self._lock:
                self.clients[identifier]["optionalClientScopes"].append(self.scopes[parts[3]])
            return httpx.Response(204)
        return httpx.Response(405)


_BINDINGS = "/apis/rbac.authorization.k8s.io/v1/clusterrolebindings"


class FakeKubernetes:
    """The part of the Kubernetes API that the control service calls.

    Objects are kept by their API path. A certificate Secret appears once the
    certificate Job exists, and the gateway StatefulSet reports a ready replica after
    a number of reads, as the certificate Job and the gateway would cause.
    """

    def __init__(self) -> None:
        self.objects: dict[str, dict[str, Any]] = {
            "/api/v1/namespaces/kube-system": {"metadata": {"name": "kube-system", "uid": "uid-0"}},
            f"{_BINDINGS}/heartwood-openshell-node-reader": {
                "metadata": {"name": "heartwood-openshell-node-reader"},
                "roleRef": {"kind": "ClusterRole", "name": "heartwood-openshell-node-reader"},
            },
        }
        self.calls: list[tuple[str, str]] = []
        # Requests, as method and path, that fail with a server error.
        self.failing: set[tuple[str, str]] = set()
        self.reads_until_ready = 1
        self._reads = 0
        self._created = 0

    def client(self) -> httpx.Client:
        transport = httpx.MockTransport(self._handle)
        return httpx.Client(transport=transport, base_url="https://kubernetes.example.test")

    def kind(self, kind: str, namespace: str | None = None) -> dict[str, dict[str, Any]]:
        """Return the objects of a kind by name, in one namespace or cluster-wide."""
        return {
            item["metadata"]["name"]: item
            for item in self.objects.values()
            if item.get("kind") == kind and item["metadata"].get("namespace") == namespace
        }

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        if (request.method, path) in self.failing:
            return httpx.Response(500)
        if request.method == "POST":
            body = json.loads(request.content)
            item = f"{path}/{body['metadata']['name']}"
            if item in self.objects:
                return httpx.Response(409)
            self._created += 1
            body["metadata"]["uid"] = f"uid-{self._created}"
            self.objects[item] = body
            return httpx.Response(201, json=body)
        if request.method == "PATCH":
            if (
                request.headers["Content-Type"] != "application/apply-patch+yaml"
                or request.url.params.get("fieldManager") != "heartwood-control"
                or request.url.params.get("force") != "true"
            ):
                return httpx.Response(415)
            self.objects[path] = {**self.objects.get(path, {}), **json.loads(request.content)}
            return httpx.Response(200, json=self.objects[path])
        if request.method == "DELETE":
            if path not in self.objects:
                return httpx.Response(404)
            # Deleting a namespace deletes the objects in it.
            name = path.removeprefix("/api/v1/namespaces/")
            inside = None if name == path or "/" in name else f"/namespaces/{name}/"
            for key in list(self.objects):
                if key == path or (inside is not None and inside in key):
                    del self.objects[key]
            return httpx.Response(200)
        return self._get(path)

    def _get(self, path: str) -> httpx.Response:
        namespace = path.split("/namespaces/")[-1].split("/")[0]
        if path.endswith("/secrets/openshell-client-tls"):
            job = f"/apis/batch/v1/namespaces/{namespace}/jobs/openshell-certgen"
            if job not in self.objects:
                return httpx.Response(404)
            files = {name: f"{name} of {namespace}" for name in ("ca.crt", "tls.crt", "tls.key")}
            data = {
                name: base64.b64encode(value.encode()).decode() for name, value in files.items()
            }
            return httpx.Response(200, json={"data": data})
        if path not in self.objects:
            return httpx.Response(404)
        found = self.objects[path]
        if path.endswith("/statefulsets/openshell"):
            self._reads += 1
            ready = 1 if self._reads >= self.reads_until_ready else 0
            found = {**found, "status": {"readyReplicas": ready}}
        return httpx.Response(200, json=found)
