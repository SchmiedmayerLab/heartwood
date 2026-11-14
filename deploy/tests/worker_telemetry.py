# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Synthetic OTLP receiver for native infrastructure evidence, never an application service."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, override

import grpc  # type: ignore[import-untyped]
import pytest
from google.protobuf.json_format import MessageToDict  # type: ignore[import-untyped]
from opentelemetry.proto.collector.logs.v1 import logs_service_pb2, logs_service_pb2_grpc


class InfrastructureCollector(logs_service_pb2_grpc.LogsServiceServicer):
    """Receive native records through the existing authenticated OTLP protocol."""

    def __init__(self, tls: Path, output: Path) -> None:
        self._lock = threading.Lock()
        self._unavailable = False
        self.failed_batches: dict[str, int] = {}
        self.rejected_gaps: set[str] = set()
        self.records: list[dict[str, Any]] = []
        self.forbidden_values: tuple[str, ...] = ()
        self.errors: list[str] = []
        self.path = output / "infrastructure-otlp.jsonl"
        self.path.touch(mode=0o600, exist_ok=False)
        self._executor = ThreadPoolExecutor(max_workers=4)
        self._server = grpc.server(self._executor)
        logs_service_pb2_grpc.add_LogsServiceServicer_to_server(self, self._server)  # type: ignore[no-untyped-call]
        credentials = grpc.ssl_server_credentials(
            [((tls / "server/tls.key").read_bytes(), (tls / "server/tls.crt").read_bytes())],
            root_certificates=(tls / "ca.crt").read_bytes(),
            require_client_auth=True,
        )
        self.port = self._server.add_secure_port("0.0.0.0:0", credentials)
        if not self.port:
            raise RuntimeError("Synthetic infrastructure collector could not bind")
        self._server.start()

    def verify_client_auth(self, tls: Path) -> None:
        """Reject a client without its certificate, with an authenticated positive control."""
        address = f"localhost:{self.port}"
        authority = (tls / "ca.crt").read_bytes()
        request = logs_service_pb2.ExportLogsServiceRequest()
        with grpc.secure_channel(address, grpc.ssl_channel_credentials(authority)) as channel:
            stub = logs_service_pb2_grpc.LogsServiceStub(channel)  # type: ignore[no-untyped-call]
            with pytest.raises(grpc.RpcError) as denied:
                stub.Export(request, timeout=5)
            assert denied.value.code() == grpc.StatusCode.UNAVAILABLE
        credentials = grpc.ssl_channel_credentials(
            authority,
            (tls / "client/tls.key").read_bytes(),
            (tls / "client/tls.crt").read_bytes(),
        )
        with grpc.secure_channel(address, credentials) as channel:
            stub = logs_service_pb2_grpc.LogsServiceStub(channel)  # type: ignore[no-untyped-call]
            stub.Export(request, timeout=5)

    @override
    def Export(
        self, request: logs_service_pb2.ExportLogsServiceRequest, context: grpc.ServicerContext
    ) -> logs_service_pb2.ExportLogsServiceResponse:
        encoded = request.SerializeToString()
        with self._lock:
            if self._unavailable:
                digest = hashlib.sha256(encoded).hexdigest()
                self.failed_batches[digest] = self.failed_batches.get(digest, 0) + 1
                for resource in request.resource_logs:
                    sandbox = next(
                        (
                            item.value.string_value
                            for item in resource.resource.attributes
                            if item.key == "openshell.sandbox.id"
                        ),
                        "",
                    )
                    for scope in resource.scope_logs:
                        if scope.scope.name == "openshell.ocsf" and any(
                            record.event_name == "openshell.ocsf.gap"
                            for record in scope.log_records
                        ):
                            self.rejected_gaps.add(sandbox)
                context.abort(grpc.StatusCode.UNAVAILABLE, "Synthetic collector outage")
            records = MessageToDict(request, preserving_proto_field_name=True)
            serialized = json.dumps(records, separators=(",", ":"))
            if any(value in serialized for value in self.forbidden_values):
                self.errors.append("Infrastructure export contained protected synthetic content")
                context.abort(grpc.StatusCode.INVALID_ARGUMENT, "Synthetic content boundary failed")
            with self.path.open("a") as output:
                output.write(serialized + "\n")
            self.records.append(records)
        return logs_service_pb2.ExportLogsServiceResponse()

    def unavailable(self, value: bool) -> None:
        with self._lock:
            self._unavailable = value

    def observed_loss(self, sandbox_id: str) -> bool:
        with self._lock:
            return sandbox_id in self.rejected_gaps

    def observations(self) -> list[dict[str, Any]]:
        with self._lock:
            batches = list(self.records)
        observations = []
        for batch in batches:
            for resource in batch.get("resource_logs", []):
                attributes = {
                    item["key"]: item["value"].get("string_value")
                    for item in resource.get("resource", {}).get("attributes", [])
                }
                for scope in resource.get("scope_logs", []):
                    if scope.get("scope", {}).get("name") != "openshell.ocsf":
                        continue
                    for record in scope.get("log_records", []):
                        observations.append({"resource": attributes, "record": record})
        return observations

    def wait_for(self, predicate: Callable[[], bool], *, timeout: float = 30) -> None:
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() >= deadline:
                raise AssertionError("Native infrastructure telemetry was not observed")
            time.sleep(0.1)

    def close(self) -> None:
        self._server.stop(grace=2).wait(timeout=5)
        self._executor.shutdown(wait=True)
        status = {
            "failed_batches": self.failed_batches,
            "rejected_gap_sandboxes": sorted(self.rejected_gaps),
            "accepted_batches": len(self.records),
            "errors": self.errors,
        }
        with self.path.with_name("infrastructure-otlp-status.json").open("x") as output:
            json.dump(status, output, indent=2)
            output.write("\n")
        assert not self.errors, self.errors
