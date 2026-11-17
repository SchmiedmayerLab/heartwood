# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Reject network-qualification false positives without claiming sandbox isolation."""

from __future__ import annotations

import errno
import importlib.util
import ipaddress
import json
import queue
import socket
from contextlib import nullcontext
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

_ROOT = Path(__file__).resolve().parents[3]
# The sandbox proxy's refusal, as the pinned OpenShell supervisor writes it.
_PROXY_DENIAL = (
    b"HTTP/1.1 403 Forbidden\r\nContent-Type: application/json\r\nContent-Length: 50\r\n"
    b'Connection: close\r\n\r\n{"error":"policy_denied","detail":"synthetic denial"}'
)


@pytest.fixture
def probe() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "worker_network_probe", _ROOT / "deploy/tests/worker_network_probe.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("code", [errno.ETIMEDOUT, errno.ENETUNREACH, errno.ECONNREFUSED])
def test_unavailable_destination_is_not_a_policy_denial(probe: ModuleType, code: int) -> None:
    with patch.object(probe.socket, "socket") as factory:
        factory.return_value.__enter__.return_value.connect.side_effect = OSError(code, "synthetic")
        with pytest.raises(AssertionError, match="without a policy denial"):
            probe.denied_connection("127.0.0.1", 80, socket.AF_INET, socket.SOCK_STREAM, "canary")


@pytest.mark.parametrize("code", [errno.EACCES, errno.EPERM])
def test_explicit_kernel_denial_is_recognized(probe: ModuleType, code: int) -> None:
    with patch.object(probe.socket, "socket") as factory:
        factory.return_value.__enter__.return_value.connect.side_effect = OSError(code, "synthetic")
        assert (
            probe.denied_connection("127.0.0.1", 80, socket.AF_INET, socket.SOCK_DGRAM, "canary")
            == errno.errorcode[code]
        )


@pytest.mark.parametrize("reply", [b"", b"HTTP/1.1 200 OK\r\n", b"ACK"])
def test_successful_connection_cannot_pass(probe: ModuleType, reply: bytes) -> None:
    with patch.object(probe.socket, "socket") as factory:
        factory.return_value.__enter__.return_value.recv.return_value = reply
        with pytest.raises(AssertionError, match="did not receive a policy denial"):
            probe.denied_connection("127.0.0.1", 80, socket.AF_INET, socket.SOCK_STREAM, "canary")


def test_http_denial_does_not_prove_udp_denial(probe: ModuleType) -> None:
    with patch.object(probe.socket, "socket") as factory:
        connection = factory.return_value.__enter__.return_value
        connection.recv.side_effect = [_PROXY_DENIAL, b""]
        assert (
            probe.denied_connection("127.0.0.1", 80, socket.AF_INET, socket.SOCK_STREAM, "canary")
            == "policy-403"
        )
        connection.recv.side_effect = [_PROXY_DENIAL, b""]
        with pytest.raises(AssertionError, match="did not receive a policy denial"):
            probe.denied_connection("127.0.0.1", 80, socket.AF_INET, socket.SOCK_DGRAM, "canary")


@pytest.mark.parametrize(
    "reply",
    [
        # A real service that answers 403 itself was reached; only the proxy's refusal counts.
        b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n",
        b'HTTP/1.1 403 Forbidden\r\nContent-Type: application/json\r\n\r\n{"error":"forbidden"}',
        b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{"error":"policy_denied"}',
        b'HTTP/1.1 403 Forbidden\r\n\r\n{"error":"policy_denied"',
    ],
)
def test_a_service_refusal_is_not_a_policy_denial(probe: ModuleType, reply: bytes) -> None:
    with patch.object(probe.socket, "socket") as factory:
        factory.return_value.__enter__.return_value.recv.side_effect = [reply, b""]
        with pytest.raises(AssertionError, match="did not receive a policy denial"):
            probe.denied_connection("127.0.0.1", 80, socket.AF_INET, socket.SOCK_STREAM, "canary")


@pytest.mark.parametrize("error", ["policy_denied", "ssrf_denied"])
def test_the_sandbox_proxy_refusal_is_a_policy_denial(probe: ModuleType, error: str) -> None:
    reply = _PROXY_DENIAL.replace(b"policy_denied", error.encode())
    with patch.object(probe.socket, "socket") as factory:
        # The refusal may arrive in several reads.
        factory.return_value.__enter__.return_value.recv.side_effect = [
            reply[:40],
            reply[40:],
            b"",
        ]
        assert (
            probe.denied_connection("127.0.0.1", 80, socket.AF_INET, socket.SOCK_STREAM, "canary")
            == "policy-403"
        )


def test_service_targets_name_real_endpoints(probe: ModuleType) -> None:
    assert probe.service_targets(
        ["records-service-via-host-gateway=172.17.0.2:9000", "signer-via-host-alias=h.internal:1"]
    ) == {
        "records-service-via-host-gateway": ("172.17.0.2", 9000),
        "signer-via-host-alias": ("h.internal", 1),
    }
    for invalid in (
        ["records=172.17.0.2"],
        ["records=172.17.0.2:0"],
        ["records=:9000"],
        ["records service=172.17.0.2:9000"],
        ["records=172.17.0.2:9000", "records=172.17.0.3:9000"],
    ):
        with pytest.raises(ValueError, match="NAME=ADDRESS:PORT"):
            probe.service_targets(invalid)


def test_tunnel_request_through_an_outside_proxy_requires_a_denial(probe: ModuleType) -> None:
    with patch.object(probe.socket, "socket") as factory:
        connection = factory.return_value.__enter__.return_value
        connection.recv.side_effect = [_PROXY_DENIAL, b""]
        assert (
            probe.denied_connection(
                "127.0.0.1",
                3128,
                socket.AF_INET,
                socket.SOCK_STREAM,
                "approved.invalid:443",
                method="CONNECT approved.invalid:443",
            )
            == "policy-403"
        )
        assert connection.sendall.call_args.args[0].startswith(
            b"CONNECT approved.invalid:443 HTTP/1.1\r\nHost: approved.invalid:443\r\n"
        )
        connection.recv.side_effect = [b"HTTP/1.1 200 Connection established\r\n\r\n", b""]
        with pytest.raises(AssertionError, match="did not receive a policy denial"):
            probe.denied_connection(
                "127.0.0.1",
                3128,
                socket.AF_INET,
                socket.SOCK_STREAM,
                "approved.invalid:443",
                method="CONNECT approved.invalid:443",
            )


@pytest.fixture
def qualification() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "worker_network_qualification", _ROOT / "deploy/tests/worker_network_qualification.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_canary_listener_failure_is_retained(qualification: ModuleType) -> None:
    listener = MagicMock()
    stopping = MagicMock()
    stopping.is_set.return_value = False
    observations: queue.SimpleQueue[bytes] = queue.SimpleQueue()
    errors: queue.SimpleQueue[Exception] = queue.SimpleQueue()
    with patch.object(
        qualification.select, "select", side_effect=OSError("synthetic listener fault")
    ):
        qualification.serve_canary(listener, stopping, observations, errors)
    assert observations.empty()
    assert str(errors.get_nowait()) == "synthetic listener fault"


def test_missing_admission_cannot_start_probe(qualification: ModuleType) -> None:
    control = MagicMock()
    control.get.return_value = SimpleNamespace(status=SimpleNamespace(configuration_admission=None))
    with pytest.raises(AssertionError, match="active admitted configuration"):
        qualification.qualify_network(
            control,
            "synthetic",
            workspace="default",
            ipv4=ipaddress.IPv4Address("127.0.0.1"),
            ipv6=ipaddress.IPv6Address("::1"),
            port=12345,
            ipv6_port=12346,
        )
    control.exec.assert_not_called()


@pytest.mark.parametrize("before", [True, False])
def test_positive_controls_do_not_erase_worker_traffic(
    qualification: ModuleType, before: bool
) -> None:
    observations: queue.SimpleQueue[bytes] = queue.SimpleQueue()
    if before:
        observations.put(b"unexpected worker traffic")
    for _ in range(5):
        observations.put(b"synthetic-outside-positive-control")
    if not before:
        observations.put(b"unexpected worker traffic")
    with patch.object(qualification.socket, "socket") as factory:
        factory.return_value.__enter__.return_value.recv.return_value = b"ACK"
        with pytest.raises(AssertionError, match="Unexpected"):
            qualification.positive_controls(
                ipaddress.IPv4Address("127.0.0.1"),
                ipaddress.IPv6Address("::1"),
                12345,
                12346,
                observations,
            )


def test_the_outside_proxy_canary_needs_its_own_positive_control(
    qualification: ModuleType,
) -> None:
    observations: queue.SimpleQueue[bytes] = queue.SimpleQueue()
    for _ in range(5):
        observations.put(b"synthetic-outside-positive-control")
    # Traffic after the five address-family controls is not the expected proxy control.
    observations.put(b"unexpected worker traffic")
    with patch.object(qualification.socket, "socket") as factory:
        connection = factory.return_value.__enter__.return_value
        connection.recv.return_value = b"ACK"
        with pytest.raises(AssertionError, match="Unexpected"):
            qualification.positive_controls(
                ipaddress.IPv4Address("127.0.0.1"),
                ipaddress.IPv6Address("::1"),
                12345,
                12346,
                observations,
            )
    targets = {call.args[0] for call in connection.connect.call_args_list}
    assert ("127.0.0.1", qualification.ALTERNATE_PROXY_PORT) in targets


_SERVICES = {
    "records-service-via-host-gateway": ("172.17.0.2", 9000),
    "signer-via-bridge-gateway": ("172.17.0.1", 9443),
}


def _admitted(protocol: ModuleType) -> SimpleNamespace:
    return SimpleNamespace(
        id="original",
        status=SimpleNamespace(
            main_process_instance_id="original",
            configuration_activated=True,
            configuration_admission=SimpleNamespace(
                state=protocol.CONFIGURATION_ADMISSION_STATE_ACCEPTED, policy_hash="original"
            ),
        ),
    )


def _denied_checks(services: dict[str, tuple[str, int]]) -> dict[str, bool | str]:
    return {
        "allowed-dns": True,
        "unlisted-dns-synthetic": True,
        "unknown-host-tcp": "EACCES",
        "ipv4-tcp": "EACCES",
        "mapped-ipv6-tcp": "EACCES",
        "direct-udp": "EACCES",
        "native-ipv6-tcp": "EACCES",
        "native-ipv6-udp": "EACCES",
        "metadata-ipv4-tcp": "policy-403",
        "metadata-ipv6-tcp": "EACCES",
        "alternate-proxy-tcp": "policy-403",
        **{f"{name}-tcp": "policy-403" for name in services},
    }


@pytest.mark.parametrize("omitted", [None, "records-service-via-host-gateway"])
def test_host_services_are_probed_at_their_real_endpoints_with_outside_controls(
    qualification: ModuleType, omitted: str | None
) -> None:
    if importlib.util.find_spec("openshell") is None:
        pytest.skip("Native SDK admission checks run in the candidate qualification environment")
    protocol = importlib.import_module("openshell._proto.openshell_pb2")
    control = MagicMock()
    control.get.return_value = _admitted(protocol)
    checks = _denied_checks(_SERVICES)
    if omitted is not None:
        del checks[f"{omitted}-tcp"]
    control.exec.return_value = SimpleNamespace(exit_code=0, stdout=json.dumps(checks))
    service_controls = MagicMock(return_value=3)
    metadata_control = MagicMock(
        return_value={"metadata-ipv4": "disposable-listener", "metadata-ipv6": "host-service"}
    )
    with (
        patch.object(
            qualification, "outside_canaries", return_value=nullcontext(queue.SimpleQueue())
        ),
        patch.object(qualification, "positive_controls"),
    ):
        arguments = {
            "workspace": "default",
            "ipv4": ipaddress.IPv4Address("127.0.0.1"),
            "ipv6": ipaddress.IPv6Address("::1"),
            "port": 12345,
            "ipv6_port": 12346,
            "host_services": _SERVICES,
            "service_controls": service_controls,
            "metadata_control": metadata_control,
        }
        if omitted is not None:
            with pytest.raises(AssertionError, match="observations are incomplete"):
                qualification.qualify_network(control, "synthetic", **arguments)
            return
        record = qualification.qualify_network(control, "synthetic", **arguments)
    command = control.exec.call_args.args[1]
    assert "--service=records-service-via-host-gateway=172.17.0.2:9000" in command
    assert "--service=signer-via-bridge-gateway=172.17.0.1:9443" in command
    # Outside clients reach the same endpoints before and after the worker probe.
    assert service_controls.call_count == metadata_control.call_count == 2
    assert record["host_service_targets"] == {
        name: f"{address}:{port}" for name, (address, port) in _SERVICES.items()
    }
    assert record["host_service_positive_controls"] == 6
    assert record["metadata_positive_controls"] == metadata_control.return_value


def test_outside_reachability_distinguishes_a_listener_from_none(
    qualification: ModuleType,
) -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        assert qualification.reachable([("127.0.0.1", port)]) == [True]
    assert qualification.reachable([("127.0.0.1", port)]) == [False]


@pytest.mark.parametrize(
    "changed",
    ["id", "generation", "policy", "activation", "claims", "dns", "tcp", "udp", "service"],
)
def test_sandbox_replacement_invalidates_network_evidence(
    qualification: ModuleType, changed: str
) -> None:
    if importlib.util.find_spec("openshell") is None:
        pytest.skip("Native SDK admission checks run in the candidate qualification environment")
    protocol = importlib.import_module("openshell._proto.openshell_pb2")

    def sandbox(*, modified: bool) -> SimpleNamespace:
        return SimpleNamespace(
            id="replacement" if modified and changed == "id" else "original",
            status=SimpleNamespace(
                main_process_instance_id=(
                    "new-generation" if modified and changed == "generation" else "original"
                ),
                configuration_activated=not (modified and changed == "activation"),
                configuration_admission=SimpleNamespace(
                    state=protocol.CONFIGURATION_ADMISSION_STATE_ACCEPTED,
                    policy_hash="new-policy" if modified and changed == "policy" else "original",
                ),
            ),
        )

    control = MagicMock()
    control.get.side_effect = [sandbox(modified=False), sandbox(modified=True)]
    checks: dict[str, bool | str] = {
        "allowed-dns": True,
        "unlisted-dns-synthetic": True,
        "unknown-host-tcp": "EACCES",
        "ipv4-tcp": "EACCES",
        "mapped-ipv6-tcp": "EACCES",
        "direct-udp": "EACCES",
        "native-ipv6-tcp": "EACCES",
        "native-ipv6-udp": "EACCES",
        "metadata-ipv4-tcp": "policy-403",
        "metadata-ipv6-tcp": "EACCES",
        "alternate-proxy-tcp": "policy-403",
    }
    if changed == "claims":
        checks = {}
    elif changed == "dns":
        checks["allowed-dns"] = False
    elif changed == "tcp":
        checks["ipv4-tcp"] = "ECONNREFUSED"
    elif changed == "udp":
        checks["direct-udp"] = "policy-403"
    elif changed == "service":
        del checks["metadata-ipv6-tcp"]
    control.exec.return_value = SimpleNamespace(exit_code=0, stdout=json.dumps(checks))
    with (
        patch.object(
            qualification, "outside_canaries", return_value=nullcontext(queue.SimpleQueue())
        ),
        patch.object(qualification, "positive_controls"),
        pytest.raises(
            AssertionError, match=r"changed during the probe|observations are incomplete"
        ),
    ):
        qualification.qualify_network(
            control,
            "synthetic",
            workspace="default",
            ipv4=ipaddress.IPv4Address("127.0.0.1"),
            ipv6=ipaddress.IPv6Address("::1"),
            port=12345,
            ipv6_port=12346,
        )


@pytest.fixture
def hostile_probe() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "worker_hostile_probe", _ROOT / "deploy/tests/worker_hostile_probe.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("code", [303, 307, 308, 401, 404, 500])
def test_hostile_probe_requires_policy_denial(hostile_probe: ModuleType, code: int) -> None:
    with (
        patch.object(
            hostile_probe.Path, "read_bytes", side_effect=PermissionError(errno.EACCES, "denied")
        ),
        patch.object(
            hostile_probe.Path, "write_text", side_effect=PermissionError(errno.EACCES, "denied")
        ),
        patch.object(
            hostile_probe.urllib.request,
            "urlopen",
            side_effect=hostile_probe.urllib.error.HTTPError(
                "https://canary.invalid", code, "synthetic", {}, None
            ),
        ),
        pytest.raises(AssertionError, match="without a policy denial"),
    ):
        hostile_probe.probe("/outside/state", "/outside/key", "https://canary.invalid")


@pytest.mark.parametrize("surface", ["read_bytes", "write_text"])
def test_hostile_probe_rejects_forbidden_filesystem_access(
    hostile_probe: ModuleType, surface: str
) -> None:
    with (
        patch.object(
            hostile_probe.Path, "read_bytes", side_effect=PermissionError(errno.EACCES, "denied")
        ),
        patch.object(
            hostile_probe.Path, "write_text", side_effect=PermissionError(errno.EACCES, "denied")
        ),
        patch.object(hostile_probe.Path, surface, return_value=b"outside synthetic content"),
        pytest.raises(AssertionError, match="Forbidden"),
    ):
        hostile_probe.probe("/outside/state", "/outside/key", "https://canary.invalid")


def test_hostile_probe_cannot_pass_when_canary_is_reachable(hostile_probe: ModuleType) -> None:
    with (
        patch.object(
            hostile_probe.Path, "read_bytes", side_effect=PermissionError(errno.EACCES, "denied")
        ),
        patch.object(
            hostile_probe.Path, "write_text", side_effect=PermissionError(errno.EACCES, "denied")
        ),
        patch.object(hostile_probe.urllib.request, "urlopen"),
        pytest.raises(AssertionError, match="Forbidden provider path was reachable"),
    ):
        hostile_probe.probe("/outside/state", "/outside/key", "https://canary.invalid")


def test_filesystem_only_probe_never_contacts_provider(hostile_probe: ModuleType) -> None:
    with (
        patch.object(
            hostile_probe.Path, "read_bytes", side_effect=PermissionError(errno.EACCES, "denied")
        ),
        patch.object(
            hostile_probe.Path, "write_text", side_effect=PermissionError(errno.EROFS, "denied")
        ),
        patch.object(
            hostile_probe.urllib.request, "urlopen", side_effect=AssertionError("network access")
        ),
    ):
        result = hostile_probe.probe_filesystem("/outside/state", "/outside/key")
    assert set(result) == {
        "protected-state-read",
        "control-credential-read",
        "container-control-read",
        "input-write",
        "skill-write",
        "protected-state-write",
    }
    assert all(result.values())


@pytest.mark.parametrize(
    "name", ["SESSION_API_KEY", "OH_SESSION_API_KEYS_0", "OH_SESSION_API_KEYS_7"]
)
def test_hostile_probe_fails_when_an_agent_command_holds_the_attempt_key(
    hostile_probe: ModuleType, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.delenv("SESSION_API_KEY", raising=False)
    assert hostile_probe.probe_session_key() == {"session-key-environment-absent": True}
    monkeypatch.setenv(name, "synthetic-attempt-key")
    with pytest.raises(AssertionError, match="attempt key"):
        hostile_probe.probe_session_key()
