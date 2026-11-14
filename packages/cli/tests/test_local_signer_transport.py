# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""A local signer must authenticate its socket before receiving bearer credentials."""

from __future__ import annotations

import os
import socket
import stat
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
import uvicorn

from heartwood.audit import (
    CheckpointSignerError,
    LocalCheckpointSignerApp,
    LocalCheckpointSignerSetup,
    LocalEd25519CheckpointSigner,
    RemoteCheckpointSigner,
    initialize_local_checkpoint_signer,
    load_checkpoint_signer_registry,
    local_checkpoint_signer_socket,
    local_checkpoint_socket_path,
)
from heartwood.schemas import AuditCheckpointStatement, AuditRetention

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Unix signer ownership controls")


@pytest.fixture
def setup() -> Iterator[LocalCheckpointSignerSetup]:
    # Unix socket paths have a small platform limit; pytest's per-test path may exceed it.
    with tempfile.TemporaryDirectory(prefix="hw-signer-", dir=Path("/tmp").resolve()) as root:
        yield initialize_local_checkpoint_signer(directory=Path(root) / "operator")


def statement() -> AuditCheckpointStatement:
    return AuditCheckpointStatement(
        deployment_id="synthetic",
        session_id="synthetic",
        created_at="2026-11-14T00:00:00Z",
        audit_event_count=0,
        terminal_event_hash=None,
        audit_content_sha256="sha256:" + "a" * 64,
        audit_size_bytes=0,
        retention=AuditRetention(policy_id="synthetic", retain_until="2026-11-14"),
    )


def test_private_socket_signs_and_verifies_with_the_real_asgi_server(
    setup: LocalCheckpointSignerSetup,
) -> None:
    profile = load_checkpoint_signer_registry(setup.registry).profile()
    signer = LocalEd25519CheckpointSigner(
        private_key=setup.private_key,
        signer_id=profile.signer_id,
        key_id=profile.key_id,
        key_version=profile.key_version,
    )
    token = profile.authorization_token()
    assert token is not None
    path = local_checkpoint_socket_path(setup.endpoint)
    with local_checkpoint_signer_socket(setup.endpoint) as listener:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        server = uvicorn.Server(
            uvicorn.Config(
                LocalCheckpointSignerApp(signer, authorization_token=token),
                fd=listener.fileno(),
                lifespan="off",
                log_level="critical",
                access_log=False,
                proxy_headers=False,
            )
        )
        thread = threading.Thread(target=server.run)
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server.started
            signature = profile.signer().sign(statement())
            assert signature.public_key_sha256 == profile.public_key_sha256
            assert signature.signer_id == profile.signer_id
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            assert not thread.is_alive()
    assert not path.exists()


def test_squatted_tcp_listener_receives_no_connection_or_token() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(0.1)
        with pytest.raises(CheckpointSignerError, match="HTTPS"):
            RemoteCheckpointSigner(
                endpoint=f"http://127.0.0.1:{listener.getsockname()[1]}/v1/checkpoints/sign",
                allow_local_socket=True,
                authorization_token="synthetic-token",
            ).sign(statement())
        with pytest.raises(TimeoutError):
            listener.accept()


@pytest.mark.parametrize("change", ["socket-owner", "socket-mode", "directory-mode", "symlink"])
def test_untrusted_socket_receives_no_connection_or_token(
    setup: LocalCheckpointSignerSetup,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    path = local_checkpoint_socket_path(setup.endpoint)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(path))
        path.chmod(0o600)
        listener.listen()
        listener.settimeout(0.1)
        if change == "socket-owner":
            original = Path.lstat

            def metadata(value: Path, *_args: object, **_kwargs: object) -> object:
                result = original(value)
                if value == path:
                    return SimpleNamespace(st_mode=result.st_mode, st_uid=os.geteuid() + 1)
                return result

            monkeypatch.setattr(Path, "lstat", metadata)
        elif change == "socket-mode":
            path.chmod(0o666)
        elif change == "directory-mode":
            path.parent.chmod(0o755)
        else:
            target = path.with_name("other.sock")
            path.rename(target)
            path.symlink_to(target)
        with pytest.raises(CheckpointSignerError, match="socket is unavailable or insecure"):
            RemoteCheckpointSigner(
                endpoint=setup.endpoint,
                allow_local_socket=True,
                authorization_token="synthetic-token",
                timeout_seconds=1,
            ).sign(statement())
        with pytest.raises(TimeoutError):
            listener.accept()


def test_server_never_replaces_an_existing_listener(setup: LocalCheckpointSignerSetup) -> None:
    path = local_checkpoint_socket_path(setup.endpoint)
    with local_checkpoint_signer_socket(setup.endpoint):
        identity = path.stat().st_ino
        with (
            pytest.raises(OSError, match="Address already in use"),
            local_checkpoint_signer_socket(setup.endpoint),
        ):
            pytest.fail("replaced an existing signer")
        assert path.stat().st_ino == identity


def test_socket_path_cannot_redirect_through_an_untrusted_ancestor(
    setup: LocalCheckpointSignerSetup,
) -> None:
    path = local_checkpoint_socket_path(setup.endpoint)
    alias = path.parent.parent / "alias"
    alias.symlink_to(path.parent, target_is_directory=True)
    endpoint = (alias / path.name).as_uri().replace("file:", "unix:", 1)
    with (
        pytest.raises(ValueError, match="trusted real directories"),
        local_checkpoint_signer_socket(endpoint),
    ):
        pytest.fail("followed a directory symlink")
