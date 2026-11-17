# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""A retained outside approval is sent through the gateway only on its exact retry."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Literal
from uuid import uuid4

import pytest

from heartwood.core_adapter import BackendErrorCode, BackendErrorEvent, SessionService
from heartwood.core_adapter._state import FileSessionStore, SessionRecoveryError
from heartwood.core_adapter.checkpoint_evidence import (
    CheckpointEvidenceUnavailableError,
    SessionCheckpointEvidence,
)
from heartwood.gateway import (
    SessionGateway,
    approval_retry_unavailable_reason,
    retained_approval,
)
from heartwood.session import CommandKind, EventKind, SessionCommand
from packages.gateway.tests.outside_retry_fixtures import (
    NOW,
    SESSION,
    OutsideReview,
    outside_review,
)

UNSENDABLE_APPROVAL_REASON = (
    "This session cannot continue, so the approval can no longer be sent; "
    "rejecting the review withdraws it."
)


@pytest.fixture
def review(tmp_path: Path) -> Iterator[OutsideReview]:
    with outside_review(tmp_path) as opened:
        yield opened


def _interrupted_approval(review: OutsideReview) -> SessionCommand:
    approved = review.decision()
    review.records.unavailable = True
    with pytest.raises(CheckpointEvidenceUnavailableError):
        review.gateway.handle(approved)
    review.records.unavailable = False
    return approved


def test_gateway_retry_of_the_approval_dispatches_a_retained_authorization_once(
    review: OutsideReview,
) -> None:
    approved = _interrupted_approval(review)

    # A read and any other command leave the authorization unsent.
    projection = review.gateway.session_projection(session_id=SESSION)
    retained = retained_approval(projection)
    assert retained is not None
    assert retained.group_id == review.group_id
    assert [action.state for action in retained.actions] == ["approved"]
    assert projection.available_commands == ("deny",)
    assert approval_retry_unavailable_reason(projection, approved) is None
    with pytest.raises(SessionRecoveryError, match="not been dispatched"):
        review.gateway.handle(review.decision())
    assert review.phase() == "authorized"
    assert review.outside.submissions == 0

    assert review.gateway.handle(approved).replayed
    assert review.phase() == "observed"
    assert review.outside.submissions == 1
    assert review.service.store.unresolved_command_ids() == ()
    sent = review.gateway.session_projection(session_id=SESSION)
    assert retained_approval(sent) is None
    assert approval_retry_unavailable_reason(sent, approved) == (
        "The approval is no longer waiting to be sent: Platform operation completed."
    )
    assert review.gateway.handle(approved).replayed
    assert review.outside.submissions == 1


@pytest.mark.parametrize("fatal", [False, True])
def test_rejecting_the_review_withdraws_a_retained_approval_through_the_gateway(
    review: OutsideReview, fatal: bool
) -> None:
    approved = _interrupted_approval(review)
    if fatal:
        review.service.backend.emit(  # type: ignore[attr-defined]
            (BackendErrorEvent(error_code=BackendErrorCode.ACTION_OUTCOME_UNKNOWN),)
        )
        projection = review.gateway.session_projection(session_id=SESSION)
        assert not projection.lifecycle.can_steer
        # Withdrawal sends nothing, so it stays available in an unrecoverable session.
        assert projection.available_commands == ("deny",)
        assert approval_retry_unavailable_reason(projection, approved) == (
            UNSENDABLE_APPROVAL_REASON
        )

    review.gateway.handle(review.decision(approved=False))

    assert review.phase() == "withdrawn"
    withdrawn = review.gateway.session_projection(session_id=SESSION)
    assert withdrawn.pending_approval is None
    assert approval_retry_unavailable_reason(withdrawn, approved) == (
        "The approval is no longer waiting to be sent: Approval withdrawn before dispatch."
    )
    if not fatal:
        assert review.gateway.handle(approved).replayed
    assert review.outside.submissions == 0


def _released_after_a_fatal_error(review: OutsideReview) -> SessionGateway:
    review.service.backend.emit(  # type: ignore[attr-defined]
        (BackendErrorEvent(error_code=BackendErrorCode.ACTION_OUTCOME_UNKNOWN),)
    )
    gateway = review.gateway
    # A restart or an idle release leaves no live service; the deployment's records remain.
    with gateway._state_lock:
        gateway._close_service(gateway._services.pop(SESSION))
        gateway._service_configurations.pop(SESSION, None)
    gateway._checkpoint_evidence_factory = lambda _: review.evidence
    return gateway


@pytest.mark.parametrize("read_first", [False, True])
def test_rejection_after_a_fatal_error_and_a_service_release_starts_no_worker(
    review: OutsideReview, read_first: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The services the gateway builds itself read the same clock as the review's service.
    monkeypatch.setattr("heartwood.core_adapter._service._utc_now", lambda: NOW.isoformat())
    approved = _interrupted_approval(review)
    gateway = _released_after_a_fatal_error(review)
    opened: list[str] = []
    factory = gateway._service_factory
    assert factory is not None

    def counting(root: Path, session_id: str) -> SessionService:
        opened.append(session_id)
        return factory(root, session_id)

    gateway._service_factory = counting

    if read_first:
        journal = FileSessionStore(gateway.sessions_root, SESSION).replay_events()
        for _ in range(2):
            projection = gateway.session_projection(session_id=SESSION)
            assert projection.available_commands == ("deny",)
            # The approval can no longer be sent, so no interface may offer its retry.
            assert approval_retry_unavailable_reason(projection, approved) == (
                UNSENDABLE_APPROVAL_REASON
            )
        # Reading the session records nothing and binds nothing to the records service.
        assert FileSessionStore(gateway.sessions_root, SESSION).replay_events() == journal
    gateway.handle(review.decision(approved=False))

    # The session cannot continue, so nothing may prepare or launch a worker for it.
    assert opened == []
    assert review.phase() == "withdrawn"
    withdrawn = gateway.session_projection(session_id=SESSION)
    assert withdrawn.pending_approval is None
    assert withdrawn.available_commands == ()
    assert approval_retry_unavailable_reason(withdrawn, approved) == (
        "The approval is no longer waiting to be sent: Approval withdrawn before dispatch."
    )
    assert review.outside.submissions == 0


def test_an_approval_expiring_after_a_fatal_error_is_recorded_once_by_a_read(
    review: OutsideReview, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [NOW]
    monkeypatch.setattr("heartwood.core_adapter._service._utc_now", lambda: now[0].isoformat())
    _interrupted_approval(review)
    gateway = _released_after_a_fatal_error(review)
    now[0] = NOW + timedelta(minutes=5)

    projection = gateway.session_projection(session_id=SESSION)

    # The expiry is recorded with its checkpoint; later reads record nothing.
    assert review.phase() == "expired"
    assert projection.pending_approval is None
    assert projection.available_commands == ()
    journal = FileSessionStore(gateway.sessions_root, SESSION).replay_events()
    gateway.session_projection(session_id=SESSION)
    assert FileSessionStore(gateway.sessions_root, SESSION).replay_events() == journal
    assert review.outside.submissions == 0


@pytest.mark.parametrize("mode", ["buffered", "required"])
def test_an_audit_export_without_a_live_service_needs_no_checkpoint(
    review: OutsideReview, mode: Literal["buffered", "required"]
) -> None:
    gateway = review.gateway
    with gateway._state_lock:
        gateway._close_service(gateway._services.pop(SESSION))
        gateway._service_configurations.pop(SESSION, None)
    evidence = review.evidence
    gateway._checkpoint_evidence_factory = lambda _: SessionCheckpointEvidence(
        records=evidence.records,
        signer=evidence.signer,
        trusted_keys=evidence.trusted_keys,
        mode=mode,
    )
    review.records.unavailable = True
    journal = FileSessionStore(gateway.sessions_root, SESSION).replay_events()

    result = gateway.handle(
        SessionCommand(
            session_id=SESSION,
            command_id=str(uuid4()),
            kind=CommandKind.AUDIT_EXPORT,
            actor_id="researcher",
            created_at=NOW.isoformat(),
            payload={},
        )
    )

    # Storage-only commands read and record the journal; they open and close no session.
    assert [event.kind for event in result.events] == [
        EventKind.COMMAND_RECEIVED,
        EventKind.AUDIT_EXPORT_RECORDED,
    ]
    after = FileSessionStore(gateway.sessions_root, SESSION).replay_events()
    assert after == (*journal, *result.events)


def test_an_expired_retained_approval_is_reported_and_never_sent(review: OutsideReview) -> None:
    approved = _interrupted_approval(review)
    review.expire()

    projection = review.gateway.session_projection(session_id=SESSION)

    assert review.phase() == "expired"
    assert retained_approval(projection) is None
    assert approval_retry_unavailable_reason(projection, approved) == (
        "The approval is no longer waiting to be sent: Review expired before dispatch."
    )
    assert review.gateway.handle(approved).replayed
    assert review.outside.submissions == 0
