# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Reduce a session journal to the run state that decisions about the session depend on."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from heartwood.core_adapter._facade import backend_error_is_fatal
from heartwood.core_adapter.action_review import AUTOMATIC_REVIEW_ACTOR
from heartwood.core_adapter.external_actions import external_action_records
from heartwood.core_adapter.external_results import external_result_records
from heartwood.session import CommandKind, EventKind, SessionEvent

# Researcher commands that continue the agent and so end a pause; the gateway's own
# decisions and anything the backend reports never do.
_CONTINUING_COMMANDS = frozenset(
    {
        CommandKind.CHAT.value,
        CommandKind.RESUME.value,
        CommandKind.APPROVE.value,
        CommandKind.WORKFLOW.value,
    }
)


@dataclass(frozen=True, slots=True)
class SessionActivity:
    """What the journal establishes about a session's run, pause and open reviews."""

    status: str
    """The last recorded run state; a recorded error alone never ends a run."""
    paused: bool
    """A researcher pause that no later researcher command continuing the agent has ended."""
    fatal: bool
    """An error after which the session accepts no further work."""
    awaiting_review: frozenset[str]
    """Tool calls whose review was requested and not resolved."""

    @property
    def settled(self) -> bool:
        """Return whether nothing recorded still depends on the conversation that ran it."""
        return (
            not self.paused
            and not self.fatal
            and not self.awaiting_review
            and self.status in {"idle", "finished", "error"}
        )

    @property
    def awaits_decision(self) -> bool:
        """Return whether the agent is stopped only by a review that may still be decided."""
        return (
            not self.paused
            and not self.fatal
            and bool(self.awaiting_review)
            and self.status == "waiting-for-confirmation"
        )


def session_activity(events: Sequence[SessionEvent]) -> SessionActivity:
    """Reduce verified journal events; the result grants no capability by itself."""
    status = "idle"
    paused = fatal = False
    awaiting_review: set[str] = set()
    for event in events:
        payload = event.payload
        if event.kind == EventKind.AGENT_LIFECYCLE_UPDATED:
            status = str(payload.get("status"))
        elif event.kind == EventKind.COMMAND_RECEIVED:
            if (
                payload.get("command_kind") in _CONTINUING_COMMANDS
                and payload.get("actor_id") != AUTOMATIC_REVIEW_ACTOR
            ):
                paused = False
        elif event.kind == EventKind.SESSION_PAUSED:
            status, paused = "paused", True
        elif event.kind == EventKind.SESSION_RESUMED:
            status, paused = "running", False
        elif event.kind == EventKind.ERROR_RECORDED:
            fatal = fatal or backend_error_is_fatal(payload.get("code"))
        elif event.kind == EventKind.CONFIRMATION_REQUESTED:
            request = payload.get("request")
            awaiting_review.add(
                str(request.get("tool_call_id")) if isinstance(request, dict) else ""
            )
            status = "waiting-for-confirmation"
        elif event.kind == EventKind.CONFIRMATION_RESOLVED:
            awaiting_review.discard(str(payload.get("tool_call_id")))
            if not awaiting_review and status == "waiting-for-confirmation":
                status = "idle"
    return SessionActivity(
        status=status, paused=paused, fatal=fatal, awaiting_review=frozenset(awaiting_review)
    )


def worker_replacement_blocker(events: Sequence[SessionEvent]) -> str | None:
    """Explain why retained work still depends on the revoked worker's own conversation.

    A replacement worker starts a new conversation in a new sandbox, so it cannot finish
    a run, a pause or an action review that the revoked worker left open, and outside
    reviews and operations stay bound to the attempt that proposed them.
    """
    activity = session_activity(events)
    if activity.awaiting_review:
        return "an action proposed by the revoked worker still awaits review"
    if activity.fatal:
        return "the session recorded an outcome that cannot be recovered"
    if not activity.settled:
        return "the revoked worker's run did not settle"
    if any(not record.terminal for record in external_action_records(events).values()) or any(
        record.phase not in {"denied", "expired", "withdrawn", "submitted"}
        for record in external_result_records(events).values()
    ):
        return "an outside operation or result release of the revoked worker is unresolved"
    return None
