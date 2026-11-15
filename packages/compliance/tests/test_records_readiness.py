# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Storage readiness requires an authenticated API response, not a live listener."""

import importlib
import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest


@pytest.mark.parametrize("failure", ["initializing", "connection", "denied", "timeout", "exited"])
def test_records_readiness_waits_only_for_startup(
    failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = importlib.util.spec_from_file_location(
        "records_qualification",
        Path(__file__).resolve().parents[3] / "deploy/tests/checkpoint_records_integration.py",
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    errors = importlib.import_module("botocore.exceptions")
    initializing = errors.ClientError(
        {"Error": {"Code": "XMinioServerNotInitialized"}}, "ListBuckets"
    )
    client = Mock()
    if failure == "connection":
        client.list_buckets.side_effect = [
            errors.EndpointConnectionError(endpoint_url="https://127.0.0.1:1"),
            {"Buckets": []},
        ]
    elif failure == "denied":
        client.list_buckets.side_effect = errors.ClientError(
            {"Error": {"Code": "AccessDenied"}}, "ListBuckets"
        )
    else:
        client.list_buckets.side_effect = [initializing, {"Buckets": []}]
    server = object.__new__(module.ObjectStore)
    server.client = Mock(return_value=client)
    server.process = Mock()
    server.process.poll.return_value = 1 if failure == "exited" else None
    monkeypatch.setattr(module.time, "sleep", Mock())
    monkeypatch.setattr(
        module.time, "monotonic", Mock(side_effect=[0, 31] if failure == "timeout" else [0, 1])
    )
    if failure == "denied":
        with pytest.raises(errors.ClientError, match="AccessDenied"):
            server._wait_ready()
        assert client.list_buckets.call_count == 1
    elif failure == "timeout":
        with pytest.raises(TimeoutError, match="did not become ready"):
            server._wait_ready()
        assert client.list_buckets.call_count == 1
    elif failure == "exited":
        with pytest.raises(RuntimeError, match="exited before readiness"):
            server._wait_ready()
        client.list_buckets.assert_not_called()
    else:
        server._wait_ready()
        assert client.list_buckets.call_count == 2
    client.close.assert_called_once_with()
