# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Policy-probe cleanup must not discard an uncertain native resource."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import cast
from unittest.mock import MagicMock, patch

import pytest

_ROOT = Path(__file__).resolve().parents[3]


def rpc_error(native: ModuleType, code: object) -> Exception:
    error = native.grpc.RpcError()
    error.code = lambda: code
    return cast(Exception, error)


@pytest.fixture
def native(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    if importlib.util.find_spec("openshell") is None:
        pytest.skip("Native SDK fixture checks run in the candidate environment")
    # Expose fixture namespaces without adding any application source directories.
    monkeypatch.syspath_prepend(str(_ROOT))
    monkeypatch.syspath_prepend(str(_ROOT / "deploy"))
    monkeypatch.syspath_prepend(str(_ROOT / "deploy/tests"))
    spec = importlib.util.spec_from_file_location(
        "worker_native_fixture", _ROOT / "deploy/tests/worker_native_fixture.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def native_evidence() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "qualification_evidence", _ROOT / "deploy/qualification_evidence.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture_owner(*, known: bool = True) -> SimpleNamespace:
    owner = SimpleNamespace()
    owner.identity = "synthetic-owner"
    owner.probes = {"probe": "original" if known else None}
    owner.control = MagicMock()
    owner.control.get.return_value = SimpleNamespace(
        id="original", labels={"qualification": "synthetic-owner"}
    )
    return owner


def test_provisioning_retry_keeps_the_same_probe(native: ModuleType) -> None:
    owner = fixture_owner()
    owner.control.delete.side_effect = [rpc_error(native, native.grpc.StatusCode.UNAVAILABLE), None]
    with patch.object(native.time, "sleep"):
        native.NativeFixture.revoke_probes(owner)
    assert owner.control.delete.call_count == 2
    owner.control.wait_deleted.assert_called_once_with(
        "probe", workspace="default", expected_sandbox_id="original", timeout_seconds=15
    )
    assert owner.probes == {}


@pytest.mark.parametrize("changed", ["identity", "owner"])
def test_changed_probe_is_not_deleted(native: ModuleType, changed: str) -> None:
    owner = fixture_owner()
    observed = owner.control.get.return_value
    if changed == "identity":
        observed.id = "replacement"
    else:
        observed.labels = {"qualification": "another-owner"}
    with pytest.raises(RuntimeError, match="ownership changed"):
        native.NativeFixture.revoke_probes(owner)
    owner.control.delete.assert_not_called()
    assert owner.probes == {"probe": "original"}


def test_uncertain_create_waits_for_owned_identity(native: ModuleType) -> None:
    owner = fixture_owner(known=False)
    owner.control.get.side_effect = [
        rpc_error(native, native.grpc.StatusCode.NOT_FOUND),
        owner.control.get.return_value,
    ]
    with patch.object(native.time, "sleep"):
        native.NativeFixture.revoke_probes(owner)
    owner.control.delete.assert_called_once()
    assert owner.probes == {}


def test_unobserved_create_retains_reconciliation_state(native: ModuleType) -> None:
    owner = fixture_owner(known=False)
    owner.control.get.side_effect = rpc_error(native, native.grpc.StatusCode.NOT_FOUND)
    with (
        patch.object(native.time, "monotonic", side_effect=[0, 0, 100]),
        patch.object(native.time, "sleep"),
        pytest.raises(RuntimeError, match="revocation is uncertain"),
    ):
        native.NativeFixture.revoke_probes(owner)
    owner.control.delete.assert_not_called()
    owner.control.wait_deleted.assert_not_called()
    assert owner.probes == {"probe": None}


def test_worker_container_record_is_retained_before_admission_decides(
    native: ModuleType, tmp_path: Path
) -> None:
    from heartwood.core_adapter.worker_attempts import WorkerAttemptError
    from heartwood.gateway.worker_volumes import WorkerVolumes

    observed = {
        "Id": "native-id",
        "Name": "/openshell-default--hw-synthetic-native-id",
        "Image": "sha256:" + "a" * 64,
        "Config": {"Labels": {"heartwood.project": "withheld-project"}, "Env": ["KEY=withheld"]},
        "HostConfig": {
            "Privileged": False,
            "CapAdd": ["SYS_ADMIN"],
            "CapDrop": None,
            "SecurityOpt": ["apparmor=unconfined"],
            "NetworkMode": "none",
            "PidMode": "",
            "IpcMode": "private",
            "Binds": ["/home/operator/withheld:/mnt"],
        },
        "Mounts": [
            {
                "Type": "bind",
                "Source": "/home/operator/withheld",
                "Destination": "/mnt",
                "RW": True,
            }
        ],
    }
    volumes = SimpleNamespace(_inspect=MagicMock(return_value=observed))
    decided: list[str] = []

    def reject(_volumes: object, name: str) -> None:
        decided.append((tmp_path / native.WORKER_CONTAINERS).read_text())
        raise WorkerAttemptError(name)

    with patch.object(WorkerVolumes, "inspect_worker", reject):
        with native.retain_worker_containers(tmp_path), pytest.raises(WorkerAttemptError):
            WorkerVolumes.inspect_worker(cast(WorkerVolumes, volumes), "worker")
        assert WorkerVolumes.inspect_worker is reject
    retained = (tmp_path / native.WORKER_CONTAINERS).read_text()
    assert decided == [retained]
    assert "withheld" not in retained
    assert json.loads(retained) == [
        {
            "image": "sha256:" + "a" * 64,
            "host_config": {
                "Privileged": False,
                "CapAdd": ["SYS_ADMIN"],
                "CapDrop": None,
                "SecurityOpt": ["apparmor=unconfined"],
                "NetworkMode": "none",
                "PidMode": "",
                "IpcMode": "private",
                "UsernsMode": "<absent>",
                "CgroupnsMode": "<absent>",
                "Devices": "<absent>",
                "DeviceRequests": "<absent>",
                "DeviceCgroupRules": "<absent>",
                "NanoCpus": "<absent>",
                "Memory": "<absent>",
                "PidsLimit": "<absent>",
                "ReadonlyRootfs": "<absent>",
            },
            "mounts": [{"type": "bind", "destination": "/mnt", "read_write": True}],
        }
    ]
    assert native.WORKER_CONTAINERS in {
        pattern.removeprefix("*/") for pattern in native_evidence().RETAINED
    }


def test_unavailable_container_observation_leaves_the_admission_decision_unchanged(
    native: ModuleType, tmp_path: Path
) -> None:
    from heartwood.gateway.worker_volumes import WorkerVolumes

    volumes = SimpleNamespace(_inspect=MagicMock(side_effect=RuntimeError("synthetic outage")))
    decided: list[str] = []
    with (
        patch.object(WorkerVolumes, "inspect_worker", lambda _volumes, name: decided.append(name)),
        native.retain_worker_containers(tmp_path),
    ):
        WorkerVolumes.inspect_worker(cast(WorkerVolumes, volumes), "worker")
    assert decided == ["worker"]
    assert not (tmp_path / native.WORKER_CONTAINERS).exists()
