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
        factory.return_value.__enter__.return_value.recv.return_value = (
            b"HTTP/1.1 403 Forbidden\r\n"
        )
        assert (
            probe.denied_connection("127.0.0.1", 80, socket.AF_INET, socket.SOCK_STREAM, "canary")
            == "policy-403"
        )
        with pytest.raises(AssertionError, match="did not receive a policy denial"):
            probe.denied_connection("127.0.0.1", 80, socket.AF_INET, socket.SOCK_DGRAM, "canary")


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


@pytest.mark.parametrize("changed", ["id", "generation", "policy", "activation"])
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
    control.exec.return_value = SimpleNamespace(exit_code=0, stdout=json.dumps({}))
    with (
        patch.object(
            qualification, "outside_canaries", return_value=nullcontext(queue.SimpleQueue())
        ),
        patch.object(qualification, "positive_controls"),
        pytest.raises(AssertionError, match="changed during the probe"),
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
