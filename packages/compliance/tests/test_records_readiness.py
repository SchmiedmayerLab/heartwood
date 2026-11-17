# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Storage qualification requires an authenticated, policy-enforcing records server."""

import importlib
import importlib.util
import json
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

import pytest


def _qualification() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "records_qualification",
        Path(__file__).resolve().parents[3] / "deploy/tests/checkpoint_records_integration.py",
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("failure", ["connection", "denied", "timeout", "exited"])
def test_records_readiness_waits_only_for_startup(
    failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _qualification()
    errors = importlib.import_module("botocore.exceptions")
    unreachable = errors.EndpointConnectionError(endpoint_url="https://127.0.0.1:1")
    probe = Mock()
    if failure == "denied":
        probe.side_effect = errors.ClientError({"Error": {"Code": "AccessDenied"}}, "ListBuckets")
    else:
        probe.side_effect = [unreachable, {"Buckets": []}]
    process = Mock()
    process.poll.return_value = 1 if failure == "exited" else None
    monkeypatch.setattr(module.time, "sleep", Mock())
    monkeypatch.setattr(
        module.time, "monotonic", Mock(side_effect=[0, 31] if failure == "timeout" else [0, 1])
    )
    if failure == "denied":
        with pytest.raises(errors.ClientError, match="AccessDenied"):
            module.ObjectStore._wait_ready(process, probe)
        assert probe.call_count == 1
    elif failure == "timeout":
        with pytest.raises(TimeoutError, match="did not become ready"):
            module.ObjectStore._wait_ready(process, probe)
        assert probe.call_count == 1
    elif failure == "exited":
        with pytest.raises(RuntimeError, match="exited before readiness"):
            module.ObjectStore._wait_ready(process, probe)
        probe.assert_not_called()
    else:
        module.ObjectStore._wait_ready(process, probe)
        assert probe.call_count == 2


@pytest.mark.parametrize("store", ["enforcing", "rejects-policy", "ignores-policy", "broken"])
def test_create_only_enforcement_is_observed_not_assumed(store: str) -> None:
    module = _qualification()
    errors = importlib.import_module("botocore.exceptions")
    root, probe = Mock(), Mock()
    if store == "rejects-policy":
        root.put_bucket_policy.side_effect = errors.ClientError(
            {"Error": {"Code": "MalformedPolicy"}}, "PutBucketPolicy"
        )
    if store == "enforcing":
        probe.put_object.side_effect = [
            errors.ClientError({"Error": {"Code": "AccessDenied"}}, "PutObject"),
            {"VersionId": "conditional"},
        ]
    if store == "broken":
        probe.put_object.side_effect = errors.ClientError(
            {"Error": {"Code": "InternalError"}}, "PutObject"
        )
    server = Mock()
    server.client.side_effect = lambda identity=None: probe if identity else root
    server.identity.return_value = ("probe-access", "probe-secret")
    report: dict[str, object] = {}
    if store == "broken":
        with pytest.raises(errors.ClientError, match="InternalError"):
            module.conditional_write_policy(server, "records", report)
    else:
        assert module.conditional_write_policy(server, "records", report) is (store == "enforcing")
    assert report["conditional_write_enforcement"] is (store == "enforcing")
    if store == "rejects-policy":
        server.identity.assert_not_called()
        assert report["conditional_policy_rejection"] == {"Code": "MalformedPolicy"}
        return
    assert "s3:if-none-match" in json.dumps(report["conditional_bucket_policy"])
    writes = probe.put_object.call_args_list
    assert "IfNoneMatch" not in writes[0].kwargs
    if store == "enforcing":
        assert writes[1].kwargs["IfNoneMatch"] == "*"
        assert writes[1].kwargs["Key"] == writes[0].kwargs["Key"]
    else:
        assert len(writes) == 1
    if store == "enforcing":
        module.require_create_only_enforcement(report)
    elif store == "ignores-policy":
        with pytest.raises(module.RecordsPolicyNotEnforcedError, match="create-only"):
            module.require_create_only_enforcement(report)


@pytest.mark.parametrize("enforcement", [False, None])
def test_records_qualification_requires_an_enforced_create_only_policy(
    enforcement: bool | None,
) -> None:
    module = _qualification()
    report = {} if enforcement is None else {"conditional_write_enforcement": enforcement}
    with pytest.raises(module.RecordsPolicyNotEnforcedError, match="create-only"):
        module.require_create_only_enforcement(report)
    module.require_create_only_enforcement({"conditional_write_enforcement": True})


@pytest.mark.parametrize("unready", ["iam", "gateway"])
def test_records_readiness_closes_its_clients(
    unready: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _qualification()
    server = object.__new__(module.ObjectStore)
    server._binary, server._state, server._ports = tmp_path / "server", tmp_path / "state", [1, 2]
    server._listen_host = "127.0.0.1"
    server.access, server.secret, server.certificates = "access", "secret", None
    iam, client = Mock(), Mock()
    server.iam = Mock(return_value=iam)
    server.client = Mock(return_value=client)
    monkeypatch.setattr(module.subprocess, "Popen", Mock())

    def wait(_process: object, probe: object) -> None:
        if probe is (iam.list_users if unready == "iam" else client.list_buckets):
            raise TimeoutError("synthetic object store did not become ready")

    monkeypatch.setattr(module.ObjectStore, "_wait_ready", staticmethod(wait))
    try:
        with pytest.raises(TimeoutError):
            server._start()
    finally:
        server._private.rmdir()
    iam.close.assert_called_once_with()
    if unready == "gateway":
        client.close.assert_called_once_with()
    else:
        server.client.assert_not_called()
