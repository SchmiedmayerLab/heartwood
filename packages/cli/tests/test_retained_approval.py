# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Terminal retry and rejection of an outside approval the gateway retains unsent."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
from textual.widgets import OptionList, Static

from heartwood.cli._interactive import InteractiveSession, format_projection_lines
from heartwood.cli._tui import HeartwoodTerminalApp
from heartwood.core_adapter import BackendErrorCode, BackendErrorEvent
from heartwood.gateway._action_presentation import UNSENDABLE_APPROVAL_NOTICE
from packages.gateway.tests.outside_retry_fixtures import SESSION, OutsideReview, outside_review


@pytest.fixture
def review(tmp_path: Path) -> Iterator[OutsideReview]:
    with outside_review(tmp_path) as opened:
        yield opened


def _interrupted(review: OutsideReview) -> InteractiveSession:
    session = InteractiveSession(review.gateway, session_id=SESSION)
    review.records.unavailable = True
    result = session.submit("/allow")
    review.records.unavailable = False
    assert result.failed
    assert result.message is not None
    assert "recorded but was not sent" in result.message
    assert "/retry" in result.message
    return session


def test_terminal_retry_resends_the_recorded_approval_once(review: OutsideReview) -> None:
    session = _interrupted(review)
    rendered = "\n".join(format_projection_lines(session.replay()))
    assert "Approval not sent: Outside Operation" in rendered
    assert "Resend the recorded approval: /retry" in rendered
    assert "/allow" not in rendered

    # A new approval is refused while the recorded one waits.
    refused = session.submit("/allow")
    assert refused.failed
    assert review.phase() == "authorized"

    sent = session.submit("/retry")

    assert not sent.failed
    assert review.phase() == "observed"
    assert review.outside.submissions == 1
    assert sent.projection is not None
    assert sent.projection.pending_approval is None
    # Once sent, nothing is offered for a retry and nothing is sent again.
    again = session.submit("/retry")
    assert again.failed
    assert again.message == "No approval is waiting to be sent."
    assert review.outside.submissions == 1


@pytest.mark.parametrize("ending", ["withdrawn", "expired"])
def test_terminal_retry_reports_why_a_closed_approval_cannot_be_sent(
    review: OutsideReview, ending: str
) -> None:
    session = _interrupted(review)
    if ending == "withdrawn":
        rejected = session.submit("/reject")
        assert not rejected.failed
    else:
        review.expire()

    result = session.submit("/retry")

    assert result.failed
    assert result.message == (
        "The approval is no longer waiting to be sent: "
        + (
            "Approval withdrawn before dispatch."
            if ending == "withdrawn"
            else "Review expired before dispatch."
        )
    )
    assert review.phase() == ending
    assert review.outside.submissions == 0


def test_another_terminal_can_only_reject_a_retained_approval(review: OutsideReview) -> None:
    _interrupted(review)
    other = InteractiveSession(review.gateway, session_id=SESSION)

    retry = other.submit("/retry")

    assert retry.failed
    assert retry.message is not None
    assert "another window" in retry.message
    other.submit("/reject")
    assert review.phase() == "withdrawn"
    assert review.outside.submissions == 0


def test_terminal_offers_only_rejection_once_the_session_cannot_continue(
    review: OutsideReview,
) -> None:
    session = _interrupted(review)
    review.service.backend.emit(  # type: ignore[attr-defined]
        (BackendErrorEvent(error_code=BackendErrorCode.ACTION_OUTCOME_UNKNOWN),)
    )

    rendered = "\n".join(format_projection_lines(session.replay()))
    assert "session cannot continue" in rendered
    assert "/retry" not in rendered
    assert "Reject the review to withdraw it: /reject" in rendered
    retry = session.submit("/retry")
    assert retry.failed
    assert retry.message == UNSENDABLE_APPROVAL_NOTICE
    allow = session.submit("/allow")
    assert allow.failed
    assert allow.message is not None
    assert "/retry" not in allow.message
    assert review.phase() == "authorized"

    async def exercise() -> None:
        app = HeartwoodTerminalApp(session)
        async with app.run_test(size=(80, 30)) as pilot:
            options = app.query_one("#approval-options", OptionList)
            for _ in range(100):
                if options.has_focus:
                    break
                await pilot.pause(0.05)
            assert "session cannot continue" in str(
                app.query_one("#approval-help", Static).render()
            )
            retry_option = options.get_option_at_index(1)
            assert retry_option.disabled
            assert "Unavailable" in str(retry_option.prompt)

    asyncio.run(exercise())
    session.submit("/reject")
    assert review.phase() == "withdrawn"
    assert review.outside.submissions == 0


def test_full_screen_terminal_offers_retry_and_rejection(review: OutsideReview) -> None:
    session = _interrupted(review)

    async def exercise() -> None:
        app = HeartwoodTerminalApp(session)
        async with app.run_test(size=(80, 30)) as pilot:
            options = app.query_one("#approval-options", OptionList)
            title = app.query_one("#approval-title", Static)
            for _ in range(100):
                if options.has_focus:
                    break
                await pilot.pause(0.05)
            assert "Approval Not Sent" in str(title.render())
            prompts = [str(options.get_option_at_index(index).prompt) for index in range(2)]
            assert prompts == ["Reject 1 Action", "Retry the Recorded Approval"]
            options.highlighted = 1
            await pilot.press("enter")
            await asyncio.wait_for(app.workers.wait_for_complete(), timeout=10.0)
            await pilot.pause()

    asyncio.run(exercise())
    assert review.phase() == "observed"
    assert review.outside.submissions == 1
