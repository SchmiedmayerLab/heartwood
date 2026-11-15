# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Recorded session journals replay to their expected projection and service state.

`fixtures/session-journals/README.md` states how each expected state differs from 0.5.0.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from heartwood.core_adapter.session_activity import session_activity
from heartwood.gateway._session_projection import project_session
from heartwood.session import JsonValue, SessionEvent

_JOURNALS = Path(__file__).parent / "fixtures" / "session-journals"


@pytest.mark.parametrize(
    "name",
    [
        "pause-then-resume",
        "backend-report-after-pause",
        "review-rejected-during-pause",
        "refused-chat-during-pause",
        "policy-change-before-refused-turn",
        "unrecoverable-open-review",
        "retained-outside-approval",
    ],
)
def test_a_recorded_journal_replays_to_its_expected_state(name: str) -> None:
    fixture = json.loads((_JOURNALS / f"{name}.json").read_text())
    events = tuple(SessionEvent.model_validate(event) for event in fixture["events"])

    projection = project_session(events, session_id=events[0].session_id)
    activity = session_activity(events)
    pending = projection.pending_approval
    replayed: dict[str, JsonValue] = {
        "projection": {
            "lifecycle": projection.lifecycle.model_dump(mode="json"),
            "paused": projection.paused,
            "researcher_status": projection.researcher_status.code,
            "available_commands": list(projection.available_commands),
            "pending_approval": None
            if pending is None
            else {"group_id": pending.group_id, "decision": pending.decision},
        },
        "service": {
            "status": activity.status,
            "paused": activity.paused,
            "fatal": activity.fatal,
            "open_reviews": list(activity.open_reviews),
            "retained_approvals": list(activity.retained_approvals),
        },
    }

    assert replayed == fixture["expected"]
