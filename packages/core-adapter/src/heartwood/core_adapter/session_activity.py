# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Reduce a session journal to the run state that decisions about the session depend on."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

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

ReviewDecision = Literal["approved", "denied"]


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
    stopped_by: Literal["error", "decision", "review"] | None = None
    """What stopped the run after its last report: a recoverable error, a review decision,
    or an action set returned to the researcher for review."""
    open_reviews: tuple[str, ...] = ()
    """Action sets, in the order they were first reviewed, that wait for a researcher decision."""
    retained_approvals: tuple[str, ...] = ()
    """Approved outside action sets that were neither sent, withdrawn nor expired."""

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


@dataclass(slots=True)
class SessionActivityReducer:
    """Apply journal events in order; the service and the interface projection share it."""

    status: str = "idle"
    paused: bool = False
    fatal: bool = False
    stopped_by: Literal["error", "decision", "review"] | None = None
    awaiting_review: set[str] = field(default_factory=set)
    review_groups: dict[str, list[str]] = field(default_factory=dict)
    """Every reviewed action set, in the order it was first reviewed, with its actions."""
    review_decisions: dict[str, ReviewDecision | None] = field(default_factory=dict)
    review_resolutions: dict[str, dict[str, ReviewDecision]] = field(default_factory=dict)
    retained_groups: set[str] = field(default_factory=set)

    def review_group(self, tool_call_id: str) -> str:
        """Return the action set that reviewed a tool call, or an empty identifier."""
        return next(
            (
                group_id
                for group_id, tool_call_ids in self.review_groups.items()
                if tool_call_id in tool_call_ids
            ),
            "",
        )

    def apply(self, event: SessionEvent) -> None:
        """Reduce one verified journal event."""
        payload = event.payload
        kind = str(event.kind)
        if kind == EventKind.AGENT_LIFECYCLE_UPDATED.value:
            self.status, self.stopped_by = str(payload.get("status")), None
        elif kind == EventKind.COMMAND_RECEIVED.value:
            if (
                payload.get("command_kind") in _CONTINUING_COMMANDS
                and payload.get("actor_id") != AUTOMATIC_REVIEW_ACTOR
            ):
                self.paused = False
                if self.status == "paused":
                    # The researcher continued the agent; neither state is settled.
                    self.status = "running"
        elif kind == EventKind.SESSION_PAUSED.value:
            self.status, self.paused, self.stopped_by = "paused", True, None
        elif kind == EventKind.SESSION_RESUMED.value:
            self.status, self.paused, self.stopped_by = "running", False, None
        elif kind == EventKind.ERROR_RECORDED.value:
            self.fatal = self.fatal or backend_error_is_fatal(payload.get("code"))
            if payload.get("automatic_review") == "not-applied":
                # A withdrawn gateway decision returns its action set to the researcher.
                group_id = _text(payload.get("group_id"))
                self.review_decisions[group_id] = None
                self.review_resolutions.pop(group_id, None)
                self.stopped_by = "review"
            elif payload.get("affects_lifecycle") is not False:
                self.stopped_by = "error"
        elif kind == EventKind.CONFIRMATION_REQUESTED.value:
            self._review_requested(payload.get("request"))
        elif kind == EventKind.APPROVAL_RECORDED.value:
            self._decision_recorded(payload)
        elif kind == EventKind.CONFIRMATION_RESOLVED.value:
            self._review_resolved(payload)
        elif kind in {
            EventKind.EXTERNAL_ACTION_RECORDED.value,
            EventKind.EXTERNAL_RESULT_RECORDED.value,
        }:
            self._outside_recorded(event)

    def activity(self) -> SessionActivity:
        """Return what the events applied so far establish."""
        return SessionActivity(
            status=self.status,
            paused=self.paused,
            fatal=self.fatal,
            awaiting_review=frozenset(self.awaiting_review),
            stopped_by=self.stopped_by,
            open_reviews=tuple(
                group_id
                for group_id in self.review_groups
                if self.review_decisions.get(group_id) is None
            ),
            retained_approvals=tuple(
                group_id for group_id in self.review_groups if group_id in self.retained_groups
            ),
        )

    def _review_requested(self, request: object) -> None:
        if not isinstance(request, dict):
            self.awaiting_review.add("")
            request = {}
        else:
            self.awaiting_review.add(str(request.get("tool_call_id")))
        self.status, self.stopped_by = "waiting-for-confirmation", None
        tool_call_id = _text(request.get("tool_call_id"))
        if not tool_call_id:
            return
        group_id = _text(request.get("group_id")) or "pending-action-set"
        group = self.review_groups.setdefault(group_id, [])
        if tool_call_id not in group:
            group.append(tool_call_id)
        self.review_decisions[group_id] = None

    def _decision_recorded(self, payload: Mapping[str, object]) -> None:
        group_id = _text(payload.get("group_id"))
        decision = _decision(payload.get("decision"))
        listed = payload.get("tool_call_ids")
        tool_call_ids = [
            item for item in map(_text, listed if isinstance(listed, list) else ()) if item
        ]
        if not (group_id and decision is not None and tool_call_ids):
            return
        group = self.review_groups.setdefault(group_id, [])
        group.extend(item for item in tool_call_ids if item not in group)
        self.review_decisions[group_id] = decision
        self.review_resolutions[group_id] = dict.fromkeys(tool_call_ids, decision)
        self.stopped_by = "decision"

    def _review_resolved(self, payload: Mapping[str, object]) -> None:
        self.awaiting_review.discard(str(payload.get("tool_call_id")))
        if not self.awaiting_review and self.status == "waiting-for-confirmation":
            self.status = "idle"
        tool_call_id = _text(payload.get("tool_call_id"))
        group_id = _text(payload.get("group_id")) or self.review_group(tool_call_id)
        decision = _decision(payload.get("decision"))
        group = self.review_groups.get(group_id)
        recorded = self.review_decisions.get(group_id)
        # A resolution that contradicts the recorded decision changes nothing.
        if group is None or decision is None or recorded not in {None, decision}:
            return
        resolutions = self.review_resolutions.setdefault(group_id, {})
        resolutions[tool_call_id] = decision
        if recorded is None and all(resolutions.get(item) == decision for item in group):
            self.review_decisions[group_id] = decision
            self.stopped_by = "decision"

    def _outside_recorded(self, event: SessionEvent) -> None:
        # Imported here: outside records depend on worker attempts, which use this reducer.
        from heartwood.core_adapter.external_actions import ExternalActionRecord
        from heartwood.core_adapter.external_results import ExternalResultRecord

        content = json.dumps(event.payload["record"])
        record = (
            ExternalActionRecord.model_validate_json(content)
            if str(event.kind) == EventKind.EXTERNAL_ACTION_RECORDED.value
            else ExternalResultRecord.model_validate_json(content)
        )
        group_id = record.proposal.group_id
        self.review_groups[group_id] = [record.proposal.action_id]
        self.review_decisions[group_id] = (
            # Closed before dispatch, so neither awaiting review nor sent.
            "denied"
            if record.phase in {"expired", "withdrawn"}
            else None
            if record.decision is None
            else record.decision.decision
        )
        if record.phase == "authorized":
            self.retained_groups.add(group_id)
        else:
            self.retained_groups.discard(group_id)


def session_activity(events: Sequence[SessionEvent]) -> SessionActivity:
    """Reduce verified journal events; the result grants no capability by itself."""
    reducer = SessionActivityReducer()
    for event in events:
        reducer.apply(event)
    return reducer.activity()


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


def _text(value: object) -> str:
    if isinstance(value, str):
        return value
    return str(value) if isinstance(value, bool | int | float) else ""


def _decision(value: object) -> ReviewDecision | None:
    return "approved" if value == "approved" else "denied" if value == "denied" else None
