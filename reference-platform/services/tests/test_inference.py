# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Session keys in LiteLLM, the model route provider and the synthetic model."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from fakes import SYNTHETIC_PRICES
from heartwood_platform import synthetic_model
from heartwood_platform.common.serving import TlsSettings
from heartwood_platform.control.inference import (
    DISCOVERY_ROUTES,
    WORKER_ROUTES,
    InferenceSetupError,
    ManagedInference,
    RouteModel,
    SessionRoute,
    request_headroom,
)
from heartwood_platform.control.litellm import InferenceGatewayError, LiteLLM, key_hash
from heartwood_platform.control.openshell import OpenShellProviders, ProviderError, ProviderRoute
from heartwood_platform.control.prices import PriceSheet
from starlette.testclient import TestClient

_PRICES = PriceSheet.model_validate_json(json.dumps(SYNTHETIC_PRICES))
# 4096 input tokens at 0.50 and 1024 output tokens at 1.50 per million.
_MODELS = (RouteModel("synthetic-coder", 4096, 1024, supports_tool_calls=True),)
_HEADROOM = Decimal("0.003584")
_MASTER = "sk-master"
_ROUTE = SessionRoute(
    "https://openshell.hw-s-abc.svc.cluster.local:8080",
    {"ca.crt": "ca", "tls.crt": "certificate", "tls.key": "private key"},
)


class FakeProxy:
    """LiteLLM's key routes, with spending recorded only when a test flushes it."""

    def __init__(self) -> None:
        self.keys: dict[str, dict[str, Any]] = {}
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.counter = 0

    def spend(self, token: str, amount: str) -> None:
        """Count spending in memory, as a request does before it is recorded."""
        self.keys[token]["live"] += Decimal(amount)

    def flush(self) -> None:
        for key in self.keys.values():
            key["spend"] = key["live"]

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("Authorization") != f"Bearer {_MASTER}":
            return httpx.Response(401)
        body = json.loads(request.content) if request.content else {}
        self.requests.append((request.url.path, body))
        if request.url.path == "/key/generate":
            self.counter += 1
            key = f"sk-generated-{self.counter}"
            self.keys[key_hash(key)] = {**body, "spend": Decimal(0), "live": Decimal(0)}
            return httpx.Response(200, json={"key": key, "key_alias": body["key_alias"]})
        if request.url.path == "/key/update":
            fields = dict(body)
            token = fields.pop("key")
            if token not in self.keys:
                return httpx.Response(404)
            self.keys[token].update(fields)
            return httpx.Response(200, json={})
        if request.url.path == "/key/info":
            token = request.url.params["key"]
            if token not in self.keys:
                return httpx.Response(404)
            entry = self.keys[token]
            info = {"spend": float(entry["spend"]), "max_budget": entry["max_budget"]}
            return httpx.Response(200, json={"key": token, "info": info})
        if request.url.path == "/key/delete":
            for token in body["keys"]:
                self.keys.pop(token, None)
            return httpx.Response(200, json={"deleted_keys": body["keys"]})
        return httpx.Response(404)


class FakeOpenShell:
    """The OpenShell command-line tool against one session gateway."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.credentials: list[str] = []
        self.workspaces: set[str] = set()
        self.profiles: dict[str, dict[str, Any]] = {}
        self.providers: dict[str, str] = {}
        self.fail: str | None = None
        # Requests to the gateway that fail to connect, as a new gateway's can.
        self.unreachable = 0

    def run(
        self, arguments: Sequence[str], environment: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        command = list(arguments[1:])
        self.calls.append(command)
        # The key never appears in the arguments.
        assert not any(value.startswith("sk-") for value in arguments)
        home = Path(environment["XDG_CONFIG_HOME"])

        def done(code: int = 0, stderr: str = "") -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(command, code, "", stderr)

        if self.unreachable and command[:2] != ["gateway", "add"]:
            self.unreachable -= 1
            return done(1, "Error: connect error: transport error")
        if self.fail is not None and command[:2] == self.fail.split():
            return done(1, "error: the gateway refused the request")
        if command[:2] == ["gateway", "add"]:
            mtls = home / "openshell" / "gateways" / "session" / "mtls"
            assert (mtls / "tls.key").read_text() == "private key"
            assert oct((mtls / "tls.key").stat().st_mode)[-3:] == "600"
            return done()
        assert command[-4:-2] == ["--gateway", "session"] or command[-2:] == [
            "--gateway",
            "session",
        ]
        if command[:2] == ["workspace", "get"]:
            return done(0 if command[2] in self.workspaces else 1)
        if command[:2] == ["workspace", "create"]:
            self.workspaces.add(command[3])
            return done()
        assert command[-2:] == ["--workspace", "research"]
        if command[:2] == ["profile", "describe"]:
            return done(0 if command[2] in self.profiles else 1)
        if command[:2] == ["profile", "import"]:
            profile = json.loads(Path(command[3]).read_text())
            self.profiles[profile["id"]] = profile
            return done()
        if command[:2] == ["provider", "get"]:
            return done(0 if command[2] in self.providers else 1)
        if command[:2] in (["provider", "create"], ["provider", "update"]):
            name = command[3] if command[1] == "create" else command[2]
            variable = command[command.index("--credential") + 1]
            self.providers[name] = environment[variable]
            self.credentials.append(environment[variable])
            return done()
        raise AssertionError(f"unexpected command {command}")


class Route:
    """ManagedInference with a fake proxy and a fake OpenShell gateway."""

    def __init__(self, tmp_path: Path) -> None:
        self.proxy = FakeProxy()
        self.openshell = FakeOpenShell()
        self.sleeps: list[float] = []
        client = httpx.Client(
            base_url="https://litellm.test:4000", transport=httpx.MockTransport(self.proxy.handler)
        )
        self.providers = OpenShellProviders(
            Path("/usr/local/bin/openshell"),
            ProviderRoute(
                name="platform-route",
                workspace="research",
                placeholder_env="HEARTWOOD_PLATFORM_MODEL_KEY",
                host="litellm.heartwood-platform.svc.cluster.local",
                port=4000,
                path="/v1/chat/completions",
                binaries=("/usr/local/bin/python3.12",),
            ),
            tmp_path,
            run=self.openshell.run,
            sleep=self.sleep,
        )
        self.inference = ManagedInference(
            tmp_path / "control.sqlite3",
            LiteLLM(client, lambda: _MASTER),
            self.providers,
            _MODELS,
            _PRICES,
            settle_wait_seconds=25,
            poll_seconds=5,
            sleep=self.sleep,
        )

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        # LiteLLM records the spending of the last requests during the wait.
        self.proxy.flush()

    def worker_key(self) -> tuple[str, dict[str, Any]]:
        (found,) = [
            (token, key)
            for token, key in self.proxy.keys.items()
            if key["metadata"]["purpose"] == "worker"
        ]
        return found


@pytest.fixture
def route(tmp_path: Path) -> Route:
    return Route(tmp_path)


def test_the_headroom_is_the_largest_cost_of_one_request() -> None:
    assert request_headroom(_MODELS, _PRICES) == _HEADROOM
    two = (*_MODELS, RouteModel("synthetic-coder", 8192, 1024))
    assert request_headroom(two, _PRICES) == Decimal("0.005632")
    for models in (
        (),
        (RouteModel("unpriced", 1, 1),),
        (RouteModel("synthetic-batch-small", 1, 1),),
    ):
        with pytest.raises(InferenceSetupError):
            request_headroom(models, _PRICES)


def test_a_launch_gives_the_worker_key_to_the_provider_and_a_discovery_key_to_the_gateway(
    route: Route,
) -> None:
    files = route.inference.prepare("abc", "demo", Decimal("0.006"), _ROUTE)

    token, worker = route.worker_key()
    assert worker["models"] == ["synthetic-coder"]
    assert worker["allowed_routes"] == list(WORKER_ROUTES)
    assert worker["max_budget"] == pytest.approx(0.006 - 0.003584)
    assert worker["metadata"] == {"session": "abc", "project": "demo", "purpose": "worker"}
    assert worker["max_parallel_requests"] == 1
    (discovery,) = [key for key in route.proxy.keys.values() if key is not worker]
    assert discovery["allowed_routes"] == list(DISCOVERY_ROUTES)
    assert discovery["models"] == ["synthetic-coder"]
    assert key_hash(files["model-discovery-key"]) in route.proxy.keys
    # Only the provider holds the worker key, under the route's endpoint and programs.
    assert route.openshell.providers == {"platform-route": "sk-generated-1"}
    assert key_hash(route.openshell.providers["platform-route"]) == token
    profile = route.openshell.profiles["platform-route"]
    assert profile["endpoints"] == [
        {
            "host": "litellm.heartwood-platform.svc.cluster.local",
            "port": 4000,
            "path": "/v1/chat/completions",
            "protocol": "rest",
            "enforcement": "enforce",
            "rules": [{"allow": {"method": "POST", "path": "/v1/chat/completions"}}],
        }
    ]
    assert profile["credentials"][0]["env_vars"] == ["HEARTWOOD_PLATFORM_MODEL_KEY"]
    assert profile["binaries"] == ["/usr/local/bin/python3.12"]
    assert route.openshell.workspaces == {"research"}
    # The control service keeps no key, and its scratch registrations are gone.
    database = (Path(route.providers._scratch) / "control.sqlite3").read_bytes()
    assert b"sk-generated" not in database
    assert not list(Path(route.providers._scratch).glob("openshell-*"))


def test_a_relaunch_keeps_the_worker_key_with_the_current_models(route: Route) -> None:
    route.inference.prepare("abc", "demo", Decimal("0.006"), _ROUTE)
    first, _ = route.worker_key()
    previous_discovery = {token for token, key in route.proxy.keys.items() if token != first}

    files = route.inference.prepare("abc", "demo", Decimal("0.006"), _ROUTE)

    assert route.worker_key()[0] == first
    assert ("/key/update", {"key": first, "models": ["synthetic-coder"]}) in route.proxy.requests
    # A gateway started before keeps the previous discovery key until the next launch.
    assert previous_discovery <= set(route.proxy.keys)
    assert key_hash(files["model-discovery-key"]) in route.proxy.keys
    route.inference.prepare("abc", "demo", Decimal("0.006"), _ROUTE)
    assert not previous_discovery & set(route.proxy.keys)
    assert route.openshell.credentials == ["sk-generated-1"]


def test_settlement_blocks_the_key_waits_for_its_spending_and_records_it(route: Route) -> None:
    route.inference.prepare("abc", "demo", Decimal("0.006"), _ROUTE)
    token, _ = route.worker_key()
    # Two requests: the second ended above the key's budget of 0.002416.
    route.proxy.spend(token, "0.00125")
    route.proxy.spend(token, "0.00125")

    incurred = route.inference.settle("abc")

    assert incurred == Decimal("0.0025")
    blocked = [body for path, body in route.proxy.requests if path == "/key/update"]
    assert blocked == [{"key": token, "max_budget": 0.0}]
    assert route.sleeps[0] == 25
    assert route.proxy.keys == {}
    # A repeated settlement after the keys are gone returns the recorded amount.
    assert route.inference.settle("abc") == Decimal("0.0025")
    route.inference.forget("abc")
    assert route.inference.settle("abc") == Decimal(0)


def test_settlement_reads_until_the_spending_stops_changing(route: Route) -> None:
    route.inference.prepare("abc", "demo", Decimal("0.006"), _ROUTE)
    token, _ = route.worker_key()
    route.proxy.spend(token, "0.00125")
    late = iter(["0.00125", "0"])

    def sleep(seconds: float) -> None:
        route.sleeps.append(seconds)
        # A request admitted before the block is recorded one poll later.
        route.proxy.flush()
        route.proxy.spend(token, next(late, "0"))

    route.inference._sleep = sleep

    assert route.inference.settle("abc") == Decimal("0.0025")
    assert route.sleeps == [25, 5, 5]


def test_rotation_carries_the_spending_and_replaces_the_provider_credential(route: Route) -> None:
    route.inference.prepare("abc", "demo", Decimal("0.006"), _ROUTE)
    old, _ = route.worker_key()
    route.proxy.spend(old, "0.00125")

    route.inference.rotate("abc", "demo", _ROUTE)

    new, key = route.worker_key()
    assert new != old
    assert old not in route.proxy.keys
    assert key["max_budget"] == pytest.approx(0.002416 - 0.00125)
    assert route.openshell.credentials == ["sk-generated-1", "sk-generated-3"]
    route.proxy.spend(new, "0.001")
    assert route.inference.settle("abc") == Decimal("0.00225")


def test_a_failed_rotation_keeps_the_previous_key_with_its_budget(route: Route) -> None:
    route.inference.prepare("abc", "demo", Decimal("0.006"), _ROUTE)
    old, _ = route.worker_key()
    route.proxy.spend(old, "0.00125")
    route.openshell.fail = "provider update"

    with pytest.raises(ProviderError):
        route.inference.rotate("abc", "demo", _ROUTE)

    token, key = route.worker_key()
    assert token == old
    assert key["max_budget"] == pytest.approx(0.002416)
    assert route.openshell.providers["platform-route"] == "sk-generated-1"
    route.openshell.fail = None
    route.inference.rotate("abc", "demo", _ROUTE)
    assert route.worker_key()[0] != old


def test_a_rotation_whose_new_key_is_refused_unblocks_the_previous_key(route: Route) -> None:
    route.inference.prepare("abc", "demo", Decimal("0.006"), _ROUTE)
    old, _ = route.worker_key()
    handler = route.proxy.handler

    def refuse_generate(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/key/generate":
            return httpx.Response(500)
        return handler(request)

    route.proxy.handler = refuse_generate  # type: ignore[method-assign]
    route.inference._proxy._client._transport = httpx.MockTransport(refuse_generate)

    with pytest.raises(InferenceGatewayError):
        route.inference.rotate("abc", "demo", _ROUTE)

    token, key = route.worker_key()
    assert token == old
    assert key["max_budget"] == pytest.approx(0.002416)


def test_spending_that_does_not_stop_changing_is_not_recorded(route: Route) -> None:
    route.inference.prepare("abc", "demo", Decimal("0.006"), _ROUTE)
    token, _ = route.worker_key()

    def sleep(seconds: float) -> None:
        route.sleeps.append(seconds)
        route.proxy.spend(token, "0.00001")
        route.proxy.flush()

    route.inference._sleep = sleep
    with pytest.raises(InferenceGatewayError, match="did not stop changing"):
        route.inference.settle("abc")
    assert token in route.proxy.keys
    assert not route.inference.settled("abc")


def test_a_lost_provider_gets_a_rotated_key_at_the_next_launch(route: Route) -> None:
    route.inference.prepare("abc", "demo", Decimal("0.006"), _ROUTE)
    old, _ = route.worker_key()
    route.openshell.providers.clear()

    route.inference.prepare("abc", "demo", Decimal("0.006"), _ROUTE)

    new, _ = route.worker_key()
    assert new != old
    assert key_hash(route.openshell.providers["platform-route"]) == new


def test_a_failed_provider_creation_is_reported_and_completed_by_the_next_launch(
    route: Route,
) -> None:
    route.openshell.fail = "provider create"
    with pytest.raises(ProviderError, match="could not create the provider: error: the gateway"):
        route.inference.prepare("abc", "demo", Decimal("0.006"), _ROUTE)
    route.openshell.fail = None

    route.inference.prepare("abc", "demo", Decimal("0.006"), _ROUTE)

    token, _ = route.worker_key()
    assert key_hash(route.openshell.providers["platform-route"]) == token
    assert (
        len([key for key in route.proxy.keys.values() if key["metadata"]["purpose"] == "worker"])
        == 1
    )


def test_a_new_gateway_that_refuses_connections_at_first_gets_the_provider(
    route: Route,
) -> None:
    route.openshell.unreachable = 3

    route.inference.prepare("abc", "demo", Decimal("0.006"), _ROUTE)

    token, _ = route.worker_key()
    assert key_hash(route.openshell.providers["platform-route"]) == token
    assert route.sleeps == [2.0, 2.0, 2.0]
    # A gateway that never answers fails the launch.
    route.openshell.unreachable = 1000
    with pytest.raises(ProviderError, match="could not reach the session's OpenShell gateway"):
        route.inference.rotate("abc", "demo", _ROUTE)
    # 30 lookups of the workspace, and nothing after them.
    assert route.openshell.unreachable == 1000 - 30


def test_a_settled_session_gets_no_new_key(route: Route) -> None:
    route.inference.prepare("abc", "demo", Decimal("0.006"), _ROUTE)
    route.inference.settle("abc")
    with pytest.raises(InferenceSetupError):
        route.inference.prepare("abc", "demo", Decimal("0.006"), _ROUTE)
    with pytest.raises(InferenceSetupError):
        route.inference.rotate("abc", "demo", _ROUTE)


def test_the_proxy_client_refuses_keys_without_models_and_hides_answers() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "sk-echoed-secret"})

    proxy = LiteLLM(
        httpx.Client(base_url="https://litellm.test", transport=httpx.MockTransport(handler)),
        lambda: _MASTER,
    )
    with pytest.raises(ValueError, match="names its models"):
        proxy.generate_key(
            alias="a", models=(), allowed_routes=WORKER_ROUTES, max_budget=Decimal(1), metadata={}
        )
    with pytest.raises(InferenceGatewayError) as caught:
        proxy.update_key("token", max_budget=Decimal(1))
    assert "sk-echoed" not in str(caught.value)


@pytest.mark.parametrize("spend", [float("inf"), float("nan"), -1.0, "0.1", True])
def test_the_proxy_client_refuses_amounts_that_are_not_spending(spend: object) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps({"info": {"spend": spend}}))

    proxy = LiteLLM(
        httpx.Client(base_url="https://litellm.test", transport=httpx.MockTransport(handler)),
        lambda: _MASTER,
    )
    with pytest.raises(InferenceGatewayError):
        proxy.key_info("token")


def test_the_services_image_pins_the_openshell_tool_of_the_gateway_build() -> None:
    root = Path(__file__).resolve().parents[3]
    build = json.loads((root / "images/worker/candidate/gateway.json").read_text())
    dockerfile = (root / "reference-platform/services/Dockerfile").read_text()
    pins = dict(re.findall(r"ADD --checksum=sha256:([0-9a-f]{64}) \\\n\s+(\S+)", dockerfile))
    assert {url: digest for digest, url in pins.items()} == {
        item["url"]: item["sha256"] for item in build["cli"].values()
    }


# The synthetic model.


@pytest.fixture
def synthetic(tmp_path: Path) -> TestClient:
    key = tmp_path / "api-key"
    key.write_text("synthetic-key\n")
    settings = synthetic_model.Settings(
        models=("synthetic-coder",),
        api_key_file=key,
        tls=TlsSettings(certificate_file=Path("/unused/tls.crt"), key_file=Path("/unused/tls.key")),
    )
    return TestClient(synthetic_model.create_app(settings), raise_server_exceptions=False)


_KEY = {"Authorization": "Bearer synthetic-key"}
_TOOLS = [{"type": "function", "function": {"name": name}} for name in ("terminal", "finish")]


def _complete(client: TestClient, messages: list[dict[str, Any]], **body: Any) -> Any:  # noqa: ANN401
    response = client.post(
        "/v1/chat/completions",
        json={"model": "synthetic-coder", "messages": messages, "tools": _TOOLS, **body},
        headers=_KEY,
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_the_synthetic_model_calls_the_terminal_and_then_finishes(synthetic: TestClient) -> None:
    user = {"role": "user", "content": "Record the value."}
    first = _complete(synthetic, [user])
    (call,) = first["choices"][0]["message"]["tool_calls"]
    assert (call["function"]["name"], json.loads(call["function"]["arguments"])) == (
        "terminal",
        {"command": synthetic_model.COMMAND},
    )
    assert first["usage"] == synthetic_model.USAGE
    assistant = {"role": "assistant", "content": "", "tool_calls": [call]}
    result = {"role": "tool", "tool_call_id": call["id"], "content": "done"}
    second = _complete(synthetic, [user, assistant, result])
    assert second["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "finish"
    # A new user message starts again.
    third = _complete(synthetic, [user, assistant, result, {"role": "user", "content": "Again."}])
    assert third["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "terminal"
    plain = synthetic.post(
        "/v1/chat/completions",
        json={"model": "synthetic-coder", "messages": [user]},
        headers=_KEY,
    ).json()
    assert plain["choices"][0]["message"]["content"] == synthetic_model.TEXT


def test_the_synthetic_model_streams_and_interrupts_on_request(synthetic: TestClient) -> None:
    user = {"role": "user", "content": "Record the value."}
    streamed = synthetic.post(
        "/v1/chat/completions",
        json={"model": "synthetic-coder", "messages": [user], "tools": _TOOLS, "stream": True},
        headers=_KEY,
    )
    events = [line for line in streamed.text.splitlines() if line.startswith("data: ")]
    assert events[-1] == "data: [DONE]"
    assert json.loads(events[1][6:])["usage"] == synthetic_model.USAGE
    interrupted = {"role": "user", "content": f"Please stop: {synthetic_model.INTERRUPT}"}

    def answer(stream: bool) -> str:
        response = synthetic.post(
            "/v1/chat/completions",
            json={"model": "synthetic-coder", "messages": [interrupted], "stream": stream},
            headers=_KEY,
        )
        return response.text

    # The answer ends after its first part, without the end of the stream or a document.
    cut = answer(stream=True)
    assert cut.startswith("data: ")
    assert "[DONE]" not in cut
    assert "usage" not in cut
    with pytest.raises(json.JSONDecodeError):
        json.loads(answer(stream=False))


def test_the_synthetic_model_accepts_only_its_key_and_models(synthetic: TestClient) -> None:
    assert synthetic.get("/v1/models").status_code == 401
    assert synthetic.get("/v1/models", headers={"Authorization": "Bearer other"}).status_code == 401
    listed = synthetic.get("/v1/models", headers=_KEY).json()
    assert [item["id"] for item in listed["data"]] == ["synthetic-coder"]
    unknown = synthetic.post(
        "/v1/chat/completions", json={"model": "other", "messages": []}, headers=_KEY
    )
    assert unknown.status_code == 404
    assert hashlib.sha256(b"synthetic-key").hexdigest() not in json.dumps(listed)
