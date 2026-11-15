# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Notebook retry and rejection of an outside approval the gateway retains unsent."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from heartwood.core_adapter.checkpoint_evidence import CheckpointEvidenceUnavailableError
from heartwood.notebook import NotebookSession, build_widget_spec
from packages.gateway.tests.outside_retry_fixtures import SESSION, OutsideReview, outside_review


@pytest.fixture
def review(tmp_path: Path) -> Iterator[OutsideReview]:
    with outside_review(tmp_path) as opened:
        yield opened


def _interrupted(review: OutsideReview) -> NotebookSession:
    session = NotebookSession(gateway=review.gateway, session_id=SESSION)
    review.records.unavailable = True
    with pytest.raises(CheckpointEvidenceUnavailableError) as raised:
        session.approve(group_id=review.group_id)
    review.records.unavailable = False
    assert any("session.retry()" in note for note in raised.value.__notes__)
    return session


def test_notebook_retry_resends_the_recorded_approval_once(review: OutsideReview) -> None:
    session = _interrupted(review)
    view = session.replay()
    assert view.pending_approval is not None
    assert view.pending_approval.decision == "approved"
    assert view.available_commands == ("deny",)
    review_items = next(
        section.items for section in build_widget_spec(view) if section.title == "Action Review"
    )
    assert review_items[0].startswith("Approval not sent:")

    # A new approval is refused while the recorded one waits.
    with pytest.raises(RuntimeError, match="recorded but was not sent"):
        session.approve(group_id=review.group_id)
    assert review.phase() == "authorized"

    sent = session.retry()

    assert sent.pending_approval is None
    assert review.phase() == "observed"
    assert review.outside.submissions == 1
    with pytest.raises(RuntimeError, match="No approval is waiting to be sent"):
        session.retry()
    assert review.outside.submissions == 1


@pytest.mark.parametrize("ending", ["withdrawn", "expired"])
def test_notebook_retry_reports_why_a_closed_approval_cannot_be_sent(
    review: OutsideReview, ending: str
) -> None:
    session = _interrupted(review)
    if ending == "withdrawn":
        session.deny(group_id=review.group_id)
    else:
        review.expire()

    with pytest.raises(RuntimeError) as raised:
        session.retry()

    assert str(raised.value) == (
        "The approval is no longer waiting to be sent: "
        + (
            "Approval withdrawn before dispatch."
            if ending == "withdrawn"
            else "Review expired before dispatch."
        )
    )
    assert review.phase() == ending
    assert review.outside.submissions == 0


def test_another_notebook_can_only_reject_a_retained_approval(review: OutsideReview) -> None:
    _interrupted(review)
    other = NotebookSession(gateway=review.gateway, session_id=SESSION)

    with pytest.raises(RuntimeError, match="another window"):
        other.retry()
    other.deny(group_id=review.group_id)

    assert review.phase() == "withdrawn"
    assert review.outside.submissions == 0
