# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest
from pydantic import AnyHttpUrl

from heartwood.core_adapter import SessionService
from heartwood.core_adapter.worker_attempts import (
    WorkerAttemptError,
    WorkerAttemptIntent,
    WorkerAttemptRecord,
    WorkerRevocationEvidence,
    WorkerRuntimeBinding,
)

_IMAGE = "sha256:" + "a" * 64
_POLICY = "b" * 64
_INPUTS = "c" * 64
_PLAN = {
    "image_digest": _IMAGE,
    "sandbox_policy_sha256": _POLICY,
    "input_manifest_sha256": _INPUTS,
}


@pytest.fixture
def service(tmp_path: Path) -> Iterator[SessionService]:
    value = SessionService.local_default(tmp_path, worker_project_id=uuid4())
    try:
        yield value
    finally:
        value.close()


def _runtime(_intent: WorkerAttemptIntent) -> WorkerRuntimeBinding:
    return WorkerRuntimeBinding(
        sandbox_id="synthetic-sandbox",
        generation="process-1",
        endpoint=AnyHttpUrl("http://worker.test"),
        image_digest=_IMAGE,
        sandbox_policy_sha256=_POLICY,
    )


def _evidence(record: WorkerAttemptRecord) -> WorkerRevocationEvidence:
    return WorkerRevocationEvidence(
        attempt_id=record.intent.attempt_id,
        sandbox_id=record.runtime.sandbox_id if record.runtime else None,
        observer_id="synthetic-observer",
        evidence_sha256="d" * 64,
        outcome="revoked" if record.runtime else "not-created",
    )


def test_intent_is_durable_before_launcher_receives_it(service: SessionService) -> None:
    calls: list[WorkerAttemptIntent] = []

    def launch(intent: WorkerAttemptIntent) -> WorkerRuntimeBinding:
        record = service.worker_attempt()
        assert record is not None
        assert record.phase == "prepared"
        assert record.intent == intent
        service.audit_log.verify()
        assert len(service.replay_events()) == len(service.audit_log.read()) == 1
        calls.append(intent)
        return _runtime(intent)

    lease = service.launch_worker(launch, **_PLAN)
    assert lease.active
    assert calls == [lease.intent]
    record = service.worker_attempt()
    assert record is not None
    assert record.phase == "active"
    with pytest.raises(WorkerAttemptError):
        service.launch_worker(launch, **_PLAN)
    assert calls == [lease.intent]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("generation", None),
        ("image_digest", "sha256:" + "e" * 64),
        ("sandbox_policy_sha256", "e" * 64),
    ],
)
def test_unverified_binding_leaves_attempt_fenced(
    service: SessionService, field: str, value: str | None
) -> None:
    def launch(intent: WorkerAttemptIntent) -> WorkerRuntimeBinding:
        return _runtime(intent).model_copy(update={field: value})

    with pytest.raises(WorkerAttemptError, match="outside reconciliation"):
        service.launch_worker(launch, **_PLAN)
    record = service.worker_attempt()
    assert record is not None
    assert record.phase == "fenced"
    assert record.runtime is None
    assert record.revocation is None
    service.audit_log.verify()


@pytest.mark.parametrize("interrupted", [False, True])
def test_uncertain_create_is_not_retried_or_declared_absent(
    service: SessionService, interrupted: bool
) -> None:
    calls = 0

    def launch(_intent: WorkerAttemptIntent) -> WorkerRuntimeBinding:
        nonlocal calls
        calls += 1
        if interrupted:
            raise KeyboardInterrupt
        raise TimeoutError("synthetic-credential-must-not-be-disclosed")

    expected = KeyboardInterrupt if interrupted else WorkerAttemptError
    with pytest.raises(expected) as failure:
        service.launch_worker(launch, **_PLAN)
    assert "synthetic-credential" not in str(failure.value)
    assert failure.value.__suppress_context__ or interrupted
    record = service.worker_attempt()
    assert record is not None
    assert record.phase == "fenced"
    assert record.revocation is None
    with pytest.raises(WorkerAttemptError):
        service.launch_worker(launch, **_PLAN)
    assert calls == 1


def test_journal_failure_prevents_control_plane_call(
    service: SessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError("synthetic storage failure")

    called = False

    def launch(intent: WorkerAttemptIntent) -> WorkerRuntimeBinding:
        nonlocal called
        called = True
        return _runtime(intent)

    monkeypatch.setattr(service, "_record_worker_transition_locked", fail)
    with pytest.raises(OSError, match="storage failure"):
        service.launch_worker(launch, **_PLAN)
    assert not called


def test_failed_binding_fences_the_already_recorded_runtime(
    service: SessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(**_kwargs: object) -> None:
        raise RuntimeError("synthetic backend binding failure")

    monkeypatch.setattr(service.backend, "bind_runtime", fail)
    with pytest.raises(WorkerAttemptError, match="outside reconciliation"):
        service.launch_worker(_runtime, **_PLAN)
    record = service.worker_attempt()
    assert record is not None
    assert record.phase == "fenced"
    assert record.runtime is not None
    assert record.revocation is None


def test_uncertain_create_stays_unadmitted_when_fence_write_fails(
    service: SessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(_record: WorkerAttemptRecord) -> None:
        raise OSError("synthetic fence write failure")

    def launch(_intent: WorkerAttemptIntent) -> WorkerRuntimeBinding:
        monkeypatch.setattr(service, "_record_worker_transition_locked", fail)
        raise TimeoutError("synthetic uncertain create")

    with pytest.raises(WorkerAttemptError, match="outside reconciliation"):
        service.launch_worker(launch, **_PLAN)
    record = service.worker_attempt()
    assert record is not None
    assert record.phase == "prepared"
    assert record.runtime is None
    assert record.revocation is None
    with pytest.raises(WorkerAttemptError):
        service.launch_worker(_runtime, **_PLAN)


def test_closed_service_cannot_contact_launcher(service: SessionService) -> None:
    service.close()
    with pytest.raises(WorkerAttemptError, match="owned deployment"):
        service.launch_worker(lambda _: pytest.fail("launcher must not be invoked"), **_PLAN)


def test_revocation_closes_admission_before_observation(service: SessionService) -> None:
    lease = service.launch_worker(_runtime, **_PLAN)
    calls = []

    def revoke(record: WorkerAttemptRecord) -> WorkerRevocationEvidence:
        assert not lease.active
        assert record.phase == "fenced"
        assert service.worker_attempt() == record
        service.audit_log.verify()
        calls.append(record)
        return _evidence(record)

    evidence = service.revoke_worker(revoke)
    assert evidence == service.revoke_worker(revoke)
    assert len(calls) == 1
    record = service.worker_attempt()
    assert record is not None
    assert record.phase == "revoked"
    service.audit_log.verify()


@pytest.mark.parametrize("mismatch", [False, True])
def test_failed_or_mismatched_revocation_cannot_authorize_replacement(
    service: SessionService, mismatch: bool
) -> None:
    lease = service.launch_worker(_runtime, **_PLAN)

    def revoke(record: WorkerAttemptRecord) -> WorkerRevocationEvidence:
        if mismatch:
            return _evidence(record).model_copy(update={"attempt_id": uuid4()})
        raise TimeoutError("synthetic observer credential")

    with pytest.raises(WorkerAttemptError, match="outside reconciliation") as failure:
        service.revoke_worker(revoke)
    assert "credential" not in str(failure.value)
    assert not lease.active
    record = service.worker_attempt()
    assert record is not None
    assert record.phase == "fenced"
    assert record.revocation is None
    with pytest.raises(WorkerAttemptError):
        service.launch_worker(_runtime, **_PLAN)

    evidence = service.revoke_worker(_evidence)
    assert evidence.attempt_id == lease.intent.attempt_id
    assert evidence.outcome == "revoked"
    assert not lease.active


def test_uncertain_prepared_attempt_can_be_reconciled_without_creation(
    service: SessionService,
) -> None:
    service.prepare_worker_attempt(**_PLAN)
    evidence = service.revoke_worker(_evidence)
    assert evidence.outcome == "not-created"
    assert evidence.sandbox_id is None


def test_revocation_without_an_attempt_does_not_call_observer(service: SessionService) -> None:
    with pytest.raises(WorkerAttemptError, match="no worker attempt"):
        service.revoke_worker(lambda _: pytest.fail("observer must not be invoked"))


@pytest.mark.parametrize("operation", ["close", "revoke"])
def test_recursive_lifecycle_calls_are_rejected(service: SessionService, operation: str) -> None:
    def launch(intent: WorkerAttemptIntent) -> WorkerRuntimeBinding:
        nested = service.close if operation == "close" else lambda: service.revoke_worker(_evidence)
        with pytest.raises(WorkerAttemptError, match="already in progress"):
            nested()
        return _runtime(intent)

    assert service.launch_worker(launch, **_PLAN).active


@pytest.mark.parametrize("operation", ["close", "revoke"])
def test_shutdown_and_revocation_wait_for_launch_without_blocking_reads(
    service: SessionService, operation: str
) -> None:
    entered, release, requested = Event(), Event(), Event()

    def launch(intent: WorkerAttemptIntent) -> WorkerRuntimeBinding:
        entered.set()
        assert release.wait(5)
        return _runtime(intent)

    def after() -> None:
        requested.set()
        if operation == "close":
            service.close()
        else:
            service.revoke_worker(_evidence)

    with ThreadPoolExecutor(max_workers=2) as pool:
        starting = pool.submit(service.launch_worker, launch, **_PLAN)
        assert entered.wait(5)
        finishing = pool.submit(after)
        try:
            assert requested.wait(5)
            with pytest.raises(TimeoutError):
                finishing.result(timeout=0.05)
            record = service.worker_attempt()
            assert record is not None
            assert record.phase == "prepared"
            assert service.store.writer_identity is not None
        finally:
            release.set()
        lease = starting.result(timeout=5)
        finishing.result(timeout=5)
    assert not lease.active
