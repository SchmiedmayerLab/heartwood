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
from heartwood.session import EventKind, SessionEvent


@dataclass(frozen=True, slots=True)
class SessionActivity:
    """What the journal establishes about a session's run, pause and open reviews."""

    status: str
    """The last recorded run state; a recorded error alone never ends a run."""
    paused: bool
    """A researcher pause that neither a resume nor a new run has ended."""
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
            if status == "running":
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
