# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Generated journals: the projection advertises exactly what gateway admission accepts.

Its pause, reviews and unrecoverable state agree with the reduction the service decides with.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from heartwood.core_adapter import BackendErrorCode
from heartwood.core_adapter.action_review import AUTOMATIC_REVIEW_ACTOR
from heartwood.core_adapter.external_actions import (
    ExternalActionDecision,
    ExternalActionProposal,
    ExternalActionRecord,
    ExternalActionRequest,
)
from heartwood.core_adapter.session_activity import session_activity
from heartwood.core_adapter.worker_attempts import WorkerAttemptIntent
from heartwood.gateway._session_projection import (
    SessionLifecycle,
    command_unavailable_reason,
    project_session,
)
from heartwood.session import EventKind, JsonValue, SessionEvent

_SESSION = "synthetic-session"
_NOW = datetime(2026, 1, 1, tzinfo=UTC)
_PROJECT, _ATTEMPT = UUID(int=1), UUID(int=2)


class _Journal:
    """Build one random but well-formed journal from the events the gateway records."""

    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self.events: list[SessionEvent] = []
        self.open_groups: list[tuple[str, list[str]]] = []
        self.outside: dict[UUID, ExternalActionRecord] = {}

    def add(self, kind: EventKind, payload: dict[str, JsonValue]) -> None:
        sequence = len(self.events)
        self.events.append(
            SessionEvent(
                event_id=f"event-{sequence}",
                session_id=_SESSION,
                sequence=sequence,
                kind=kind,
                occurred_at=_NOW.isoformat(),
                payload=payload,
            )
        )

    def step(self) -> None:
        steps: list[Callable[[], None]] = [
            self.lifecycle,
            self.command,
            self.command,
            lambda: self.add(EventKind.SESSION_PAUSED, {}),
            lambda: self.add(EventKind.SESSION_RESUMED, {}),
            self.error,
            self.review,
            self.review,
            self.outside_operation,
        ]
        if self.open_groups:
            steps += [self.decision, self.decision, self.review_not_applied]
        if self.outside:
            steps += [self.outside_decision, self.outside_decision]
        self.rng.choice(steps)()

    def lifecycle(self) -> None:
        status = self.rng.choice(
            ("running", "idle", "finished", "error", "paused", "waiting-for-confirmation")
        )
        self.add(EventKind.AGENT_LIFECYCLE_UPDATED, {"status": status})

    def command(self) -> None:
        kind = self.rng.choice(("chat", "pause", "resume", "approve", "deny", "workflow"))
        actor = AUTOMATIC_REVIEW_ACTOR if self.rng.random() < 0.15 else "researcher"
        self.add(
            EventKind.COMMAND_RECEIVED,
            {"command_id": f"command-{len(self.events)}", "command_kind": kind, "actor_id": actor},
        )
        if self.rng.random() < 0.3:
            self.add(
                EventKind.ERROR_RECORDED,
                {"command": kind, "reason": "synthetic refusal", "affects_lifecycle": False},
            )

    def error(self) -> None:
        payload: dict[str, JsonValue] = {
            "reason": "synthetic",
            "code": self.rng.choice(
                (
                    BackendErrorCode.ACTION_FAILED.value,
                    BackendErrorCode.ACTION_OUTCOME_UNKNOWN.value,
                )
            ),
        }
        if self.rng.random() < 0.5:
            payload["affects_lifecycle"] = False
        self.add(EventKind.ERROR_RECORDED, payload)

    def review(self) -> None:
        group = f"group-{len(self.events)}"
        calls = [f"{group}-call-{index}" for index in range(self.rng.randint(1, 2))]
        for call in calls:
            self.add(
                EventKind.TOOL_CALL_PROPOSED,
                {"tool_call_id": call, "tool_name": "terminal", "arguments": {"command": "ls"}},
            )
        for call in calls:
            self.add(
                EventKind.CONFIRMATION_REQUESTED,
                {
                    "request": {
                        "request_id": f"{call}-confirm",
                        "session_id": _SESSION,
                        "group_id": group,
                        "tool_call_id": call,
                        "tool_name": "terminal",
                        "risk": "medium",
                        "summary": "Run terminal",
                        "arguments": {"command": "ls"},
                    }
                },
            )
        self.open_groups.append((group, calls))

    def decision(self) -> None:
        group, calls = self.open_groups.pop(self.rng.randrange(len(self.open_groups)))
        decision = self.rng.choice(("approved", "denied"))
        if self.rng.random() < 0.7:
            self.add(
                EventKind.APPROVAL_RECORDED,
                {"group_id": group, "decision": decision, "tool_call_ids": list(calls)},
            )
        for call in calls[: self.rng.randint(0, len(calls))]:
            self.add(
                EventKind.CONFIRMATION_RESOLVED,
                {"group_id": group, "tool_call_id": call, "decision": decision},
            )

    def review_not_applied(self) -> None:
        group, _ = self.rng.choice(self.open_groups)
        self.add(
            EventKind.ERROR_RECORDED,
            {
                "command": "approve",
                "group_id": group,
                "automatic_review": "not-applied",
                "reason": "synthetic",
                "affects_lifecycle": False,
            },
        )

    def outside_operation(self) -> None:
        source = f"event-{len(self.events)}"
        self.add(
            EventKind.TOOL_CALL_PROPOSED,
            {
                "tool_call_id": f"worker-{source}",
                "tool_name": "synthetic-analysis",
                "worker_attempt_id": str(_ATTEMPT),
                "worker_project_id": str(_PROJECT),
            },
        )
        proposal = ExternalActionProposal(
            operation_id=UUID(int=len(self.events) + 100),
            source_event_id=source,
            service_policy_sha256="a" * 64,
            worker=WorkerAttemptIntent(
                project_id=_PROJECT,
                session_id=_SESSION,
                attempt_id=_ATTEMPT,
                conversation_id=UUID(int=3),
                image_digest="sha256:" + "b" * 64,
                gateway_policy_sha256="c" * 64,
                sandbox_policy_sha256="d" * 64,
                input_manifest_sha256="e" * 64,
            ),
            request=ExternalActionRequest(
                service_id="synthetic-service",
                tool_name="synthetic-analysis",
                destination="synthetic-results",
                summary="Submit synthetic analysis",
                request_json="{}",
            ),
            created_at=_NOW,
            expires_at=_NOW + timedelta(minutes=5),
        )
        self.record(ExternalActionRecord(phase="proposed", proposal=proposal))

    def outside_decision(self) -> None:
        current = self.rng.choice(list(self.outside.values()))
        proposal = current.proposal

        def decided(decision: str, command_id: str) -> ExternalActionDecision:
            return ExternalActionDecision(
                command_id=command_id,
                actor_id="researcher",
                decision=decision,  # type: ignore[arg-type]
                decided_at=_NOW,
                proposal_sha256=proposal.sha256,
            )

        approval = decided("approved", f"approve-{proposal.operation_id}")
        following: dict[str, list[ExternalActionRecord]] = {
            "proposed": [
                current.model_copy(update={"phase": "authorized", "decision": approval}),
                ExternalActionRecord(
                    phase="denied",
                    proposal=proposal,
                    decision=decided("denied", f"deny-{proposal.operation_id}"),
                ),
                ExternalActionRecord(phase="expired", proposal=proposal),
            ],
            "authorized": [
                current.model_copy(
                    update={
                        "phase": "withdrawn",
                        "withdrawal": decided("denied", f"withdraw-{proposal.operation_id}"),
                    }
                ),
                current.model_copy(update={"phase": "expired"}),
                current.model_copy(update={"phase": "submitting"}),
            ],
            "submitting": [current.model_copy(update={"phase": "unknown"})],
        }
        if choices := following.get(current.phase):
            self.record(self.rng.choice(choices))

    def record(self, record: ExternalActionRecord) -> None:
        self.outside[record.proposal.operation_id] = record
        self.add(EventKind.EXTERNAL_ACTION_RECORDED, {"record": record.model_dump(mode="json")})


def _journals(count: int) -> list[tuple[SessionEvent, ...]]:
    rng = random.Random(51)
    journals = []
    for _ in range(count):
        journal = _Journal(rng)
        for _ in range(rng.randint(1, 9)):
            journal.step()
        journals.append(tuple(journal.events))
    return journals


@pytest.mark.parametrize("journal", _journals(400))
def test_advertised_commands_are_admitted_and_agree_with_the_service_state(
    journal: tuple[SessionEvent, ...],
) -> None:
    for end in range(1, len(journal) + 1):
        events = journal[:end]
        projection = project_session(events, session_id=_SESSION)
        activity = session_activity(events)
        commands = projection.available_commands
        lifecycle = projection.lifecycle
        unrecoverable = lifecycle.status == SessionLifecycle.ERROR and not lifecycle.can_steer
        retained = projection.pending_approval
        withdrawal_only = retained is not None and retained.decision == "approved"

        # The gateway admits every advertised command, and only those.
        for kind in ("chat", "pause", "resume", "approve", "deny"):
            assert (command_unavailable_reason(projection, kind) is None) == (kind in commands)
        # An unrecoverable session refuses all but a withdrawal, which sends nothing; the
        # service refuses all work after a fatal error.
        if unrecoverable or activity.fatal:
            assert unrecoverable
            assert commands == (("deny",) if withdrawal_only else ())
        # Every control and status matches what is advertised.
        assert lifecycle.can_pause == ("pause" in commands)
        assert lifecycle.can_resume == ("resume" in commands)
        assert lifecycle.can_steer == ("chat" in commands)
        assert (projection.researcher_status.code == "paused") == ("resume" in commands)
        assert (retained is not None and not unrecoverable) == (
            projection.researcher_status.code == "waiting-for-review"
        )
        if not unrecoverable:
            assert ("deny" in commands) == (retained is not None)
            assert ("approve" in commands) == (retained is not None and not withdrawal_only)
        # A refused command changes neither the pause nor what is advertised for it.
        if end >= 2 and journal[end - 1].payload.get("reason") == "synthetic refusal":
            before = journal[: end - 2]
            assert activity.paused == session_activity(before).paused
            if not activity.fatal:
                assert ("resume" in commands) == (
                    "resume" in project_session(before, session_id=_SESSION).available_commands
                )
        # Pause and open reviews are the service's own; the backend may also report a pause.
        assert not projection.paused or activity.paused or activity.status == "paused"
        if activity.paused and retained is None and not activity.fatal:
            assert projection.paused
        if retained is not None:
            assert retained.group_id in (
                activity.retained_approvals if withdrawal_only else activity.open_reviews
            )
        elif not unrecoverable:
            assert not activity.retained_approvals
