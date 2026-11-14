# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Private Unix-socket transport for one launcher-owned inference runtime."""

from __future__ import annotations

import tempfile
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import httpx

from heartwood.persistence import private_socket_identity, require_private_socket_parent

MANAGED_MODEL_BASE_URL = "http://127.0.0.1:8765/v1"
MANAGED_RUNTIME_SOCKET_ENV = "HEARTWOOD_LOCAL_RUNTIME_SOCKET"
_TRANSPORT_ORIGIN = "http://127.0.0.1:8765"
_binding_lock = threading.Lock()
_binding: tuple[Path, tuple[int, int], str] | None = None


class ManagedInferenceError(ValueError):
    """The launched runtime cannot be reached through its private socket."""


def require_managed_base_url(base_url: str | None) -> None:
    """Reject altered managed routes before a provider client can select TCP."""
    if base_url != MANAGED_MODEL_BASE_URL:
        raise ManagedInferenceError("managed inference requires its reserved local route")


@contextmanager
def managed_inference_socket(project_root: Path) -> Iterator[Path]:
    """Allocate a short, owner-only socket path outside the research directory."""
    with tempfile.TemporaryDirectory(prefix="hw-model-", dir=Path("/tmp").resolve()) as root:
        path = Path(root) / "model.sock"
        if path.is_relative_to(project_root.resolve()):
            raise ManagedInferenceError("managed inference socket must be outside the project")
        require_private_socket_parent(path)
        yield path


def runtime_socket_path(env: Mapping[str, str]) -> Path:
    """Read an explicit launcher binding; never discover a network listener."""
    value = env.get(MANAGED_RUNTIME_SOCKET_ENV, "")
    path = Path(value)
    if not value or not path.is_absolute() or ".." in path.parts or str(path) != value:
        raise ManagedInferenceError("managed inference requires its launcher-owned Unix socket")
    return path


class _SocketBinding:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.identity = private_socket_identity(path)

    def check(self) -> None:
        if private_socket_identity(self.path) != self.identity:
            raise ManagedInferenceError("managed inference socket identity changed")


class _SocketTransport(httpx.BaseTransport):
    def __init__(self, binding: _SocketBinding) -> None:
        self.binding = binding
        self.transport = httpx.HTTPTransport(
            uds=str(binding.path), retries=0, limits=httpx.Limits(max_keepalive_connections=0)
        )

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.binding.check()
        return self.transport.handle_request(request)

    def close(self) -> None:
        self.transport.close()


class _AsyncSocketTransport(httpx.AsyncBaseTransport):
    def __init__(self, binding: _SocketBinding) -> None:
        self.binding = binding
        self.transport = httpx.AsyncHTTPTransport(
            uds=str(binding.path), retries=0, limits=httpx.Limits(max_keepalive_connections=0)
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.binding.check()
        return await self.transport.handle_async_request(request)

    async def aclose(self) -> None:
        await self.transport.aclose()


def managed_inference_client(env: Mapping[str, str], *, timeout: float = 30) -> httpx.Client:
    """Create a bounded catalog client with no proxy, redirect or TCP fallback."""
    binding = _SocketBinding(runtime_socket_path(env))
    return httpx.Client(
        base_url=MANAGED_MODEL_BASE_URL,
        transport=_SocketTransport(binding),
        timeout=timeout,
        follow_redirects=False,
        trust_env=False,
    )


def configure_managed_inference(env: Mapping[str, str]) -> str:
    """Bind the private transport and return its distinct provider-client cache identity."""
    import litellm

    global _binding
    socket_binding = _SocketBinding(runtime_socket_path(env))
    selected = (socket_binding.path, socket_binding.identity)
    with _binding_lock:
        if _binding is not None:
            if _binding[:2] != selected:
                raise ManagedInferenceError("managed inference binding cannot change in a process")
            return _binding[2]
        if litellm.client_session is not None or litellm.aclient_session is not None:
            raise ManagedInferenceError(
                "managed inference cannot replace an existing SDK transport"
            )
        # The dependency owns provider protocols and routing; only the reserved local
        # origin is mounted on a private socket. Other origins retain the standard HTTPX transport.
        litellm.client_session = httpx.Client(
            mounts={_TRANSPORT_ORIGIN: _SocketTransport(socket_binding)},
            follow_redirects=False,
        )
        litellm.aclient_session = httpx.AsyncClient(
            mounts={_TRANSPORT_ORIGIN: _AsyncSocketTransport(socket_binding)},
            follow_redirects=False,
        )
        # Provider clients can be cached before their HTTP transport is configured.
        # A unique placeholder prevents reuse of a client holding the old TCP transport.
        # The socket's ownership authenticates this route; this value grants no access.
        placeholder = "managed-" + uuid4().hex
        _binding = (*selected, placeholder)
        return placeholder
