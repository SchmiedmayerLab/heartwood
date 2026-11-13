# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Reviewed, bounded release of outside observations into a worker conversation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Literal, Self, cast
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, model_validator

from heartwood.core_adapter._facade import ProposedToolCall, pending_action_group
from heartwood.core_adapter.external_actions import (
    ExternalActionDecision,
    ExternalActionError,
    ExternalActionRecord,
    canonical_request,
    external_action_records,
)
from heartwood.core_adapter.worker_attempts import WorkerAttemptIntent
from heartwood.schemas import JsonValue
from heartwood.schemas.experiments import Digest, Reference
from heartwood.session import EventKind, SessionEvent

RESULT_PREFIX = "outside-result-"


class ExternalResultProposal(BaseModel):
    """Exact context and destination reviewed independently of the outside operation."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, revalidate_instances="always", hide_input_in_errors=True
    )
    release_id: UUID
    operation_id: UUID
    operation_sha256: Digest
    observation_event_id: Reference
    worker: WorkerAttemptIntent
    service_policy_sha256: Digest
    model_route_json: str
    content_json: str
    created_at: AwareDatetime
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def _content(self) -> Self:
        if self.expires_at <= self.created_at:
            raise ValueError("result release requires a positive validity interval")
        for value in (self.content_json, self.model_route_json):
            if canonical_request(value) != value:
                raise ValueError("result release requires canonical bounded JSON")
        # Bound the actual native message, including its provenance envelope.
        canonical_request(self.message)
        return self

    @property
    def message(self) -> str:
        """Render data for the native conversation; labels confer no authority."""
        return json.dumps(
            {
                "kind": "outside-result",
                "release_id": str(self.release_id),
                "operation_id": str(self.operation_id),
                "operation_sha256": self.operation_sha256,
                "content": json.loads(self.content_json),
            },
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    @property
    def sha256(self) -> str:
        """Bind the complete result, destination, and validity interval."""
        return hashlib.sha256(
            json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    @property
    def action_id(self) -> str:
        """Keep result approval identities separate from worker tool calls."""
        return f"{RESULT_PREFIX}{self.release_id}"

    @property
    def review_call(self) -> ProposedToolCall:
        """Present the exact content and destination through shared action review."""
        arguments = cast(dict[str, JsonValue], self.model_dump(mode="json"))
        arguments["content"] = json.loads(self.content_json)
        arguments["model_route"] = json.loads(self.model_route_json)
        del arguments["content_json"], arguments["model_route_json"]
        return ProposedToolCall(
            tool_call_id=self.action_id,
            action_id=self.action_id,
            tool_name="release_outside_result",
            risk="unknown",
            summary="Send reviewed outside result to the worker and its model",
            arguments=arguments,
        )

    @property
    def group_id(self) -> str:
        """Use the existing complete-content approval digest."""
        group = pending_action_group((self.review_call,))
        assert group is not None
        return group.group_id


class ExternalResultRecord(BaseModel):
    """A release intent is durable before native delivery and can never be resent."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, revalidate_instances="always", hide_input_in_errors=True
    )
    phase: Literal["proposed", "denied", "submitting", "unknown", "submitted"]
    proposal: ExternalResultProposal
    decision: ExternalActionDecision | None = None

    @model_validator(mode="after")
    def _decision(self) -> Self:
        if self.phase == "proposed":
            if self.decision is not None:
                raise ValueError("proposed release already has a decision")
        elif (
            self.decision is None
            or self.decision.proposal_sha256 != self.proposal.sha256
            or (self.phase == "denied") != (self.decision.decision == "denied")
        ):
            raise ValueError("result decision does not match its review")
        elif (
            self.decision.decision == "approved"
            and not self.proposal.created_at <= self.decision.decided_at < self.proposal.expires_at
        ):
            raise ValueError("result release decision is expired")
        return self


def validate_result_transition(
    previous: ExternalResultRecord | None, current: ExternalResultRecord
) -> None:
    """Allow one decision and at most one native delivery attempt."""
    if previous is None:
        if current.phase != "proposed":
            raise ExternalActionError("result release has no retained proposal")
        return
    if current.proposal != previous.proposal:
        raise ExternalActionError("result release content or destination changed")
    if previous.phase == "proposed" and current.phase in {"denied", "submitting"}:
        return
    if (
        previous.phase == "submitting"
        and current.phase in {"unknown", "submitted"}
        and current.decision == previous.decision
    ):
        return
    raise ExternalActionError("result release cannot be replayed or changed")


def external_result_records(events: Sequence[SessionEvent]) -> dict[UUID, ExternalResultRecord]:
    """Validate retained release history without restoring delivery authority."""
    outside = external_action_records(events)
    sources = {event.event_id: event for event in events}
    records: dict[UUID, ExternalResultRecord] = {}
    operations: set[UUID] = set()
    decisions = {
        record.decision.command_id for record in outside.values() if record.decision is not None
    }
    for event in events:
        if event.kind != EventKind.EXTERNAL_RESULT_RECORDED:
            continue
        current = ExternalResultRecord.model_validate_json(json.dumps(event.payload["record"]))
        proposal = current.proposal
        previous = records.get(proposal.release_id)
        if proposal.worker.session_id != event.session_id:
            raise ExternalActionError("result release belongs to another session")
        if previous is None:
            source = sources.get(proposal.observation_event_id)
            if (
                source is None
                or source.sequence >= event.sequence
                or source.kind != EventKind.EXTERNAL_ACTION_RECORDED
            ):
                raise ExternalActionError("result release has no earlier outside observation")
            observed = ExternalActionRecord.model_validate_json(
                json.dumps(source.payload["record"])
            )
            if (
                not observed.terminal
                or observed.observation is None
                or observed.proposal.operation_id != proposal.operation_id
                or observed.proposal.sha256 != proposal.operation_sha256
                or observed.proposal.worker != proposal.worker
                or proposal.operation_id not in outside
                or proposal.operation_id in operations
            ):
                raise ExternalActionError(
                    "result release has no unique matching outside observation"
                )
            operations.add(proposal.operation_id)
        if current.decision is not None and (previous is None or previous.decision is None):
            if current.decision.command_id in decisions:
                raise ExternalActionError("result release reuses another decision")
            decisions.add(current.decision.command_id)
        validate_result_transition(previous, current)
        records[proposal.release_id] = current
    return records
