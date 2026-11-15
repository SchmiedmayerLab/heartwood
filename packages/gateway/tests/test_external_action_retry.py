# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""A retained outside approval is sent through the gateway only on its exact retry."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from heartwood.core_adapter import BackendErrorCode, BackendErrorEvent
from heartwood.core_adapter._state import SessionRecoveryError
from heartwood.core_adapter.checkpoint_evidence import CheckpointEvidenceUnavailableError
from heartwood.gateway import approval_retry_unavailable_reason, retained_approval
from heartwood.session import SessionCommand
from packages.gateway.tests.outside_retry_fixtures import SESSION, OutsideReview, outside_review


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
