# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Managed inference must never fall back to a squatted TCP listener."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import uvicorn

from heartwood.cli._launch import _run_interaction, _wait_for_runtime
from heartwood.gateway import ModelSettings, ProjectContext, SessionGateway
from heartwood.gateway._managed_inference import (
    MANAGED_MODEL_BASE_URL,
    MANAGED_RUNTIME_SOCKET_ENV,
    ManagedInferenceError,
    configure_managed_inference,
    managed_inference_client,
    managed_inference_socket,
)
from heartwood.gateway._model_catalog import (
    BUILT_IN_MODEL_CONNECTIONS,
    ModelCatalogService,
    _list_openai_models,
)
from heartwood.gateway._model_settings import ModelProfile
from heartwood.gateway._openhands_models import prepare_openhands_import
from heartwood.gateway._project_config import LocalModelSelection
from heartwood.gateway._readiness import managed_local_runtime_active
from heartwood.session import CommandKind, SessionCommand


@pytest.fixture
def model_server(tmp_path: Path) -> Iterator[tuple[Path, list[str], uvicorn.Server]]:
    observed: list[str] = []

    async def app(
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        assert scope["client"] is None
        body = bytearray()
        while True:
            request = await receive()
            body.extend(request.get("body", b""))
            if not request.get("more_body", False):
                break
        observed.append(scope["path"])
        if scope["path"] == "/v1/models":
            payload: dict[str, object] = {"data": [{"id": "synthetic-model"}]}
        else:
            assert scope["path"] == "/v1/chat/completions"
            message = json.loads(body)
            assert "synthetic prompt" in json.dumps(message)
            if message.get("stream"):
                await send(
                    {
                        "type": "http.response.start",
                        "status": 200,
                        "headers": [(b"content-type", b"text/event-stream")],
                    }
                )
                for content, finish in (("synthetic ", None), ("reply", None), ("", "stop")):
                    chunk = {
                        "id": "synthetic-completion",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "synthetic-model",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"role": "assistant", "content": content},
                                "finish_reason": finish,
                            }
                        ],
                    }
                    await send(
                        {
                            "type": "http.response.body",
                            "body": ("data: " + json.dumps(chunk) + "\n\n").encode(),
                            "more_body": True,
                        }
                    )
                await send({"type": "http.response.body", "body": b"data: [DONE]\n\n"})
                return
            payload = {
                "id": "synthetic-completion",
                "object": "chat.completion",
                "created": 1,
                "model": "synthetic-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "synthetic reply"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4},
            }
        encoded = json.dumps(payload).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": encoded})

    with managed_inference_socket(tmp_path) as path, socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(path))
        path.chmod(0o600)
        listener.listen()
        server = uvicorn.Server(
            uvicorn.Config(
                app,
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
            yield path, observed, server
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            assert not thread.is_alive()


def test_catalog_readiness_and_real_sdk_use_socket_while_tcp_is_squatted(
    model_server: tuple[Path, list[str], uvicorn.Server],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepare_openhands_import()
    import litellm
    from openhands.sdk import LLM, LLMStreamChunk
    from openhands.sdk.llm import Message, TextContent

    path, observed, server = model_server
    env = {
        MANAGED_RUNTIME_SOCKET_ENV: str(path),
        "HEARTWOOD_LOCAL_RUNTIME_ARTIFACT_ID": "synthetic",
    }
    monkeypatch.setattr(litellm, "client_session", None)
    monkeypatch.setattr(litellm, "aclient_session", None)
    monkeypatch.setattr("heartwood.gateway._managed_inference._binding", None)
    with socket.socket() as squatter:
        squatter.bind(("127.0.0.1", 8765))
        squatter.listen()
        squatter.settimeout(0.1)
        selection = LocalModelSelection(
            artifact_id="synthetic", path=".heartwood/models/synthetic", model_id="synthetic-model"
        )
        assert not managed_local_runtime_active(selection, {})
        assert managed_local_runtime_active(selection, env)
        connection = next(c for c in BUILT_IN_MODEL_CONNECTIONS if c.connection_id == "heartwood")
        assert ModelCatalogService(env=env).discover(connection, api_key=None).models
        # Warm the real provider client's cache before installing the private transport.
        # Reusing that client would bypass the Unix socket entirely.
        cached_requests = []

        def previous_transport(request: httpx.Request) -> httpx.Response:
            cached_requests.append(request)
            return httpx.Response(
                200,
                json={
                    "id": "cached-completion",
                    "created": 1,
                    "model": "synthetic-model",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "previous route"},
                            "finish_reason": "stop",
                        }
                    ],
                },
            )

        previous_client = httpx.Client(transport=httpx.MockTransport(previous_transport))
        litellm.client_session = previous_client
        previous_llm = LLM(
            model="openai/synthetic-model",
            api_key="local-model",
            base_url=MANAGED_MODEL_BASE_URL,
            api_mode="chat",
            stream=False,
            num_retries=0,
            max_input_tokens=32768,
            max_output_tokens=128,
        )
        messages = [Message(role="user", content=[TextContent(text="synthetic prompt")])]
        assert "previous route" in str(previous_llm.generate(messages))
        assert len(cached_requests) == 1
        litellm.client_session = None
        placeholder = configure_managed_inference(env)
        assert configure_managed_inference(env) == placeholder
        # Installation mutates the SDK's public client hooks in another function.
        client = cast(httpx.Client, litellm.client_session)
        async_client = cast(httpx.AsyncClient, litellm.aclient_session)
        assert isinstance(client, httpx.Client)
        assert isinstance(async_client, httpx.AsyncClient)
        try:
            llm = LLM(
                model="openai/synthetic-model",
                api_key=placeholder,
                base_url=MANAGED_MODEL_BASE_URL,
                api_mode="chat",
                stream=False,
                num_retries=0,
                max_input_tokens=32768,
                max_output_tokens=128,
            )
            assert "synthetic reply" in str(llm.generate(messages))
            assert "synthetic reply" in str(asyncio.run(llm.agenerate(messages)))
            streaming = llm.model_copy(update={"stream": True})
            chunks: list[LLMStreamChunk] = []
            assert "synthetic reply" in str(streaming.generate(messages, on_token=chunks.append))
            assert chunks
            chunks.clear()
            assert "synthetic reply" in str(
                asyncio.run(streaming.agenerate(messages, on_token=chunks.append))
            )
            assert chunks
            assert observed.count("/v1/chat/completions") == 4
            assert len(cached_requests) == 1
            with pytest.raises(TimeoutError):
                squatter.accept()
            server.should_exit = True
            deadline = time.monotonic() + 5
            while server.servers[0].is_serving() and time.monotonic() < deadline:
                time.sleep(0.01)
            # A vanished listener must not trigger the model client's ordinary TCP path.
            path.unlink()
            with pytest.raises(FileNotFoundError):
                client.post(
                    MANAGED_MODEL_BASE_URL + "/chat/completions",
                    json={"messages": "synthetic prompt"},
                )
            assert not managed_local_runtime_active(selection, env)
            with pytest.raises(TimeoutError):
                squatter.accept()
        finally:
            previous_client.close()
            client.close()
            asyncio.run(async_client.aclose())


def test_missing_binding_never_selects_tcp() -> None:
    with pytest.raises(ManagedInferenceError, match="launcher-owned"):
        managed_inference_client({})


@pytest.mark.parametrize("asynchronous", [False, True])
def test_replaced_socket_cannot_receive_inference_or_rebind_the_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, asynchronous: bool
) -> None:
    prepare_openhands_import()
    import litellm

    monkeypatch.setattr(litellm, "client_session", None)
    monkeypatch.setattr(litellm, "aclient_session", None)
    monkeypatch.setattr("heartwood.gateway._managed_inference._binding", None)
    with (
        managed_inference_socket(tmp_path) as path,
        socket.socket(socket.AF_UNIX) as original,
        socket.socket(socket.AF_UNIX) as replacement,
    ):
        original.bind(str(path))
        path.chmod(0o600)
        original.listen()
        env = {MANAGED_RUNTIME_SOCKET_ENV: str(path)}
        configure_managed_inference(env)
        client = cast(httpx.Client, litellm.client_session)
        async_client = cast(httpx.AsyncClient, litellm.aclient_session)
        try:
            path.rename(path.with_suffix(".old"))
            replacement.bind(str(path))
            path.chmod(0o600)
            replacement.listen()
            replacement.settimeout(0.1)
            if asynchronous:
                with pytest.raises(ManagedInferenceError, match="identity changed"):
                    asyncio.run(async_client.post(MANAGED_MODEL_BASE_URL + "/chat/completions"))
            else:
                with pytest.raises(ManagedInferenceError, match="identity changed"):
                    client.post(MANAGED_MODEL_BASE_URL + "/chat/completions")
            with pytest.raises(ManagedInferenceError, match="binding cannot change"):
                configure_managed_inference(env)
            assert litellm.client_session is client
            assert litellm.aclient_session is async_client
            with pytest.raises(TimeoutError):
                replacement.accept()
        finally:
            client.close()
            asyncio.run(async_client.aclose())


@pytest.mark.parametrize("existing_transport", ["client_session", "aclient_session"])
def test_managed_inference_preserves_an_existing_sdk_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing_transport: str
) -> None:
    prepare_openhands_import()
    import litellm

    monkeypatch.setattr(litellm, "client_session", None)
    monkeypatch.setattr(litellm, "aclient_session", None)
    monkeypatch.setattr("heartwood.gateway._managed_inference._binding", None)
    with (
        managed_inference_socket(tmp_path) as path,
        socket.socket(socket.AF_UNIX) as listener,
        httpx.Client() as existing,
    ):
        listener.bind(str(path))
        path.chmod(0o600)
        listener.listen()
        listener.settimeout(0.1)
        monkeypatch.setattr(litellm, existing_transport, existing)
        with pytest.raises(ManagedInferenceError, match="cannot replace an existing SDK transport"):
            configure_managed_inference({MANAGED_RUNTIME_SOCKET_ENV: str(path)})
        assert getattr(litellm, existing_transport) is existing
        with pytest.raises(TimeoutError):
            listener.accept()


def test_managed_socket_cannot_be_created_inside_the_project() -> None:
    with (
        pytest.raises(ManagedInferenceError, match="must be outside the project"),
        managed_inference_socket(Path("/tmp").resolve()),
    ):
        pytest.fail("exposed the managed socket to the project")


@pytest.mark.parametrize(
    "base_url",
    [MANAGED_MODEL_BASE_URL + "/", "http://localhost:8765/v1", "http://127.0.0.1:9000/v1"],
)
def test_altered_managed_route_is_rejected_before_client_creation(
    tmp_path: Path, base_url: str
) -> None:
    from heartwood.gateway._openhands_sdk import OpenHandsSdkBackend

    profile = ModelProfile(
        profile_id="heartwood",
        model="openai/synthetic-model",
        base_url=base_url,
        policy_endpoint=base_url.rstrip("/") + "/chat/completions",
        credential_kind="none",
    )
    backend = OpenHandsSdkBackend(
        profile=profile,
        workspace=tmp_path,
        skills_dir=tmp_path / "skills",
        persistence_dir=tmp_path / "state",
        conversation_key="synthetic",
        managed_inference=True,
        env={},
    )
    assert backend.configuration_error == "managed inference requires its reserved local route"
    with pytest.raises(ManagedInferenceError, match="reserved local route"):
        backend._default_conversation_factory(lambda _event: None, lambda _chunk: None)
    assert not (tmp_path / "state").exists()
    connection = next(c for c in BUILT_IN_MODEL_CONNECTIONS if c.connection_id == "heartwood")
    with pytest.raises(ManagedInferenceError, match="reserved local route"):
        _list_openai_models(replace(connection, base_url=base_url), None, env={})


def test_gateway_binds_managed_transport_from_the_selected_source(tmp_path: Path) -> None:
    gateway = SessionGateway(project=ProjectContext(tmp_path), env={})
    profile = ModelProfile(
        profile_id="heartwood",
        model="openai/synthetic-model",
        base_url=MANAGED_MODEL_BASE_URL + "/",
        policy_endpoint=MANAGED_MODEL_BASE_URL + "/chat/completions",
        credential_kind="none",
    )
    gateway.config_store.select_model_source(
        "heartwood", ModelSettings(active_profile="heartwood", profiles=(profile,))
    )
    try:
        result = gateway.handle(
            SessionCommand(
                command_id="synthetic-chat",
                session_id="synthetic",
                kind=CommandKind.CHAT,
                created_at="2026-11-13T12:00:00Z",
                payload={"prompt": "synthetic prompt"},
            )
        )
        assert any(
            event.payload.get("reason") == "managed inference requires its reserved local route"
            for event in result.events
        )
    finally:
        gateway.stop()


def test_runtime_exit_terminates_the_interaction(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime_command = [sys.executable, "-c", "import time; time.sleep(0.5)"]
    interaction_command = [
        sys.executable,
        "-c",
        "import os,time; from pathlib import Path; "
        "Path('pid').write_text(str(os.getpid())); time.sleep(30)",
    ]
    with subprocess.Popen(runtime_command, text=True) as runtime:
        assert (
            _run_interaction(interaction_command, env=os.environ, cwd=tmp_path, runtime=runtime)
            == 70
        )
    assert "inference exited" in capsys.readouterr().out
    with pytest.raises(ProcessLookupError):
        os.kill(int((tmp_path / "pid").read_text()), 0)


def test_exited_runtime_cannot_start_an_interaction(tmp_path: Path) -> None:
    with subprocess.Popen([sys.executable, "-c", "pass"], text=True) as runtime:
        runtime.wait()
        assert (
            _run_interaction(
                [sys.executable, "-c", "from pathlib import Path; Path('started').touch()"],
                env=os.environ,
                cwd=tmp_path,
                runtime=runtime,
            )
            == 70
        )
    assert not (tmp_path / "started").exists()


def test_launcher_readiness_uses_the_owned_socket(
    model_server: tuple[Path, list[str], uvicorn.Server],
) -> None:
    path, observed, _server = model_server
    with subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], text=True
    ) as runtime:
        try:
            assert _wait_for_runtime(
                runtime, socket_path=path, model_id="synthetic-model", timeout=1
            )
            runtime.terminate()
            runtime.wait()
            assert not _wait_for_runtime(
                runtime, socket_path=path, model_id="synthetic-model", timeout=1
            )
            assert observed == ["/v1/models"]
        finally:
            if runtime.poll() is None:
                runtime.terminate()


@pytest.mark.parametrize("fails_startup", [False, True])
def test_packaged_qualification_uses_private_socket_and_cleans_up(
    tmp_path: Path, fails_startup: bool
) -> None:
    root = Path(__file__).resolve().parents[3]
    scripts = root / "images/generic/scripts"
    project = tmp_path / "project"
    project.mkdir()
    runtime = tmp_path / "runtime.sh"
    runtime.write_text(
        "exit 1\n"
        if fails_startup
        else (
            'exec "$HEARTWOOD_PYTHON" "$STUB" --socket "$HEARTWOOD_LOCAL_RUNTIME_SOCKET" '
            '--request-log "$REQUESTS"\n'
        ),
        encoding="utf-8",
    )
    output = project / "observed.json"
    probe = (
        "import json,os,pathlib; "
        "from heartwood.gateway._managed_inference import managed_inference_client; "
        "p=pathlib.Path(os.environ['HEARTWOOD_LOCAL_RUNTIME_SOCKET']); "
        "client=managed_inference_client(os.environ); "
        "r=client.get('models'); r.raise_for_status(); client.close(); "
        "pathlib.Path('observed.json').write_text(json.dumps({'path':str(p),"
        "'mode':p.stat().st_mode & 0o777,'models':r.json()}))"
    )
    result = subprocess.run(
        [
            sys.executable,
            str(scripts / "qualify_runtime.py"),
            "--project",
            str(project),
            "--log",
            str(tmp_path / "runtime.log"),
            "--timeout",
            "10",
            "--runtime",
            str(runtime),
            "--",
            sys.executable,
            "-c",
            probe,
        ],
        env={
            **os.environ,
            "HEARTWOOD_PYTHON": sys.executable,
            "STUB": str(scripts / "local_model_stub.py"),
            "REQUESTS": str(tmp_path / "requests"),
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == (70 if fails_startup else 0), result.stdout + result.stderr
    if fails_startup:
        assert not output.exists()
    else:
        observed = json.loads(output.read_text())
        assert observed["mode"] == 0o600
        assert observed["models"]["data"][0]["id"] == "heartwood-managed-runtime"
        assert not Path(observed["path"]).parent.exists()
