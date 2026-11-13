# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Outside operation records and the trusted platform adapter boundary."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import datetime
from typing import Literal, Protocol, Self, cast
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from heartwood.core_adapter._facade import ProposedToolCall, pending_action_group
from heartwood.core_adapter.worker_attempts import WorkerAttemptIntent
from heartwood.schemas import JsonValue, PlatformOperationObservation
from heartwood.schemas.experiments import Digest, Reference
from heartwood.session import EventKind, SessionEvent

_MAX_REQUEST_BYTES = 64 * 1024
_MAX_INPUT_BYTES = 16 * 1024 * 1024
_OUTSIDE_PREFIX = "outside-operation-"


class ExternalActionError(ValueError):
    """An outside operation cannot be admitted or its outcome cannot be established."""


class _Record(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, revalidate_instances="always", hide_input_in_errors=True
    )


def _unique_keys(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate request key")
        result[key] = value
    return result


def canonical_request(value: str) -> str:
    """Validate bounded native request bytes without inventing a backend request schema."""
    try:
        if len(value.encode("utf-8")) > _MAX_REQUEST_BYTES:
            raise ValueError
        parsed = json.loads(value, object_pairs_hook=_unique_keys)
        if not isinstance(parsed, dict):
            raise ValueError
        pending: list[tuple[JsonValue, int]] = [(parsed, 0)]
        while pending:
            item, depth = pending.pop()
            if depth > 64:
                raise ValueError
            if isinstance(item, dict):
                pending.extend((entry, depth + 1) for entry in item.values())
            elif isinstance(item, list):
                pending.extend((entry, depth + 1) for entry in item)
        result = json.dumps(parsed, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(result.encode("utf-8")) > _MAX_REQUEST_BYTES:
            raise ValueError
        return result
    except (ValueError, TypeError, RecursionError):
        raise ExternalActionError(
            "outside operation request is invalid or exceeds its limit"
        ) from None


class ExternalInputReference(_Record):
    """An immutable input resolved by the trusted adapter, never an implicit worker path."""

    reference: Reference
    sha256: Digest
    size_bytes: int = Field(ge=0, le=_MAX_INPUT_BYTES)


class ExternalActionRequest(_Record):
    """Native operation content resolved from trusted service configuration."""

    service_id: Reference
    tool_name: Reference
    destination: Reference
    summary: str = Field(min_length=1, max_length=512)
    request_json: str = Field(max_length=_MAX_REQUEST_BYTES)
    inputs: tuple[ExternalInputReference, ...] = Field(default=(), max_length=16)

    @model_validator(mode="after")
    def _content(self) -> Self:
        if self.request_json != canonical_request(self.request_json):
            raise ValueError("outside operation request must use canonical JSON")
        if len({item.reference for item in self.inputs}) != len(self.inputs):
            raise ValueError("outside operation input references must be unique")
        if sum(item.size_bytes for item in self.inputs) > _MAX_INPUT_BYTES:
            raise ValueError("outside operation inputs exceed the byte limit")
        return self


class ExternalActionProposal(_Record):
    """Gateway-issued review binding; a worker message cannot supply its authority."""

    schema_version: Literal["heartwood.external-action.v1"] = "heartwood.external-action.v1"
    operation_id: UUID
    worker: WorkerAttemptIntent
    source_event_id: Reference
    service_policy_sha256: Digest
    request: ExternalActionRequest
    created_at: AwareDatetime
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def _validity(self) -> Self:
        if self.expires_at <= self.created_at:
            raise ValueError("outside proposal requires a positive validity interval")
        return self

    @property
    def sha256(self) -> str:
        """Bind the complete request, inputs, destination, scope, policy, and validity."""
        content = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(content.encode()).hexdigest()

    @property
    def action_id(self) -> str:
        """Return a gateway-owned identity distinct from native worker tool-call identities."""
        return f"{_OUTSIDE_PREFIX}{self.operation_id}"

    @property
    def review_call(self) -> ProposedToolCall:
        """Reuse the shared action review without granting native worker execution authority."""
        arguments = cast(dict[str, JsonValue], self.model_dump(mode="json"))
        request = cast(dict[str, JsonValue], arguments["request"])
        request["arguments"] = json.loads(self.request.request_json)
        del request["request_json"]
        return ProposedToolCall(
            tool_call_id=self.action_id,
            action_id=self.action_id,
            tool_name=self.request.tool_name,
            risk="unknown",
            summary=self.request.summary,
            arguments=arguments,
        )

    @property
    def group_id(self) -> str:
        """Use the existing complete-content action-set digest for researcher approval."""
        group = pending_action_group((self.review_call,))
        assert group is not None
        return group.group_id


class ExternalActionDecision(_Record):
    """One trusted researcher decision retained before any outside dispatch."""

    command_id: Reference
    actor_id: Reference
    decision: Literal["approved", "denied"]
    decided_at: AwareDatetime
    proposal_sha256: Digest
    permitted_uses: Literal[1] = 1


class ExternalActionObservation(_Record):
    """A platform observation bound to the exact submitted proposal."""

    proposal_sha256: Digest
    operation: PlatformOperationObservation


class ExternalActionRecord(_Record):
    """One complete observation in the existing paired session and audit journal."""

    phase: Literal["proposed", "denied", "submitting", "unknown", "observed"]
    proposal: ExternalActionProposal
    decision: ExternalActionDecision | None = None
    observation: ExternalActionObservation | None = None

    @model_validator(mode="after")
    def _binding(self) -> Self:
        if self.phase == "proposed":
            if self.decision is not None or self.observation is not None:
                raise ValueError("a proposed operation has no decision or result")
            return self
        if self.decision is None or self.decision.proposal_sha256 != self.proposal.sha256:
            raise ValueError("outside decision does not match the reviewed proposal")
        if (
            self.decision.decision == "approved"
            and not self.proposal.created_at <= self.decision.decided_at < self.proposal.expires_at
        ):
            raise ValueError("outside decision is expired")
        if (self.phase == "denied") != (self.decision.decision == "denied"):
            raise ValueError("outside operation phase does not match its decision")
        if (self.phase == "observed") != (self.observation is not None):
            raise ValueError("observed operations require an outside observation")
        if self.observation is not None:
            observed = self.observation
            operation = observed.operation
            if (
                observed.proposal_sha256 != self.proposal.sha256
                or operation.operation_id != str(self.proposal.operation_id)
                or operation.project_context_id != str(self.proposal.worker.project_id)
                or operation.service_id != self.proposal.request.service_id
            ):
                raise ValueError("outside observation belongs to another operation")
        return self

    @property
    def terminal(self) -> bool:
        """Whether the trusted record establishes a terminal operation outcome."""
        return self.phase == "denied" or (
            self.observation is not None
            and self.observation.operation.status in {"completed", "failed", "denied", "cancelled"}
        )


def external_action_records(events: Sequence[SessionEvent]) -> dict[UUID, ExternalActionRecord]:
    """Reduce canonical records without converting replay into execution authority."""
    records: dict[UUID, ExternalActionRecord] = {}
    sources = {event.event_id: event for event in events}
    used_sources: set[str] = set()
    decisions: dict[str, UUID] = {}
    for event in events:
        if event.kind != EventKind.EXTERNAL_ACTION_RECORDED:
            continue
        current = ExternalActionRecord.model_validate_json(json.dumps(event.payload["record"]))
        if current.proposal.worker.session_id != event.session_id:
            raise ExternalActionError("outside operation belongs to another session")
        previous = records.get(current.proposal.operation_id)
        if previous is None:
            source = sources.get(current.proposal.source_event_id)
            if (
                source is None
                or source.sequence >= event.sequence
                or source.kind != EventKind.TOOL_CALL_PROPOSED
                or source.session_id != event.session_id
                or source.payload.get("worker_attempt_id")
                != str(current.proposal.worker.attempt_id)
                or source.payload.get("worker_project_id")
                != str(current.proposal.worker.project_id)
                or source.payload.get("tool_name") != current.proposal.request.tool_name
                or source.event_id in used_sources
            ):
                raise ExternalActionError("outside operation has no unique worker proposal")
            used_sources.add(source.event_id)
        if current.decision is not None:
            command_id = current.decision.command_id
            if command_id in decisions and decisions[command_id] != current.proposal.operation_id:
                raise ExternalActionError("outside decision was reused for another operation")
            decisions[command_id] = current.proposal.operation_id
        validate_external_transition(previous, current)
        records[current.proposal.operation_id] = current
    return records


def validate_external_transition(
    previous: ExternalActionRecord | None, current: ExternalActionRecord
) -> None:
    """Require one immutable proposal and one decision, with no replayed dispatch."""
    if previous is None:
        if current.phase != "proposed":
            raise ExternalActionError("outside operation has no retained proposal")
        return
    if current.proposal != previous.proposal or previous.terminal:
        raise ExternalActionError("outside operation identity changed or is already terminal")
    if previous.phase == "proposed":
        if current.phase not in {"denied", "submitting"}:
            raise ExternalActionError("outside operation requires a trusted decision")
    elif current.phase not in {"unknown", "observed"} or current.decision != previous.decision:
        raise ExternalActionError("outside operation cannot reuse or change its decision")


def reserved_external_identity(value: str | None) -> bool:
    """Prevent worker events from selecting a gateway-owned operation identity."""
    return value is not None and value.startswith(_OUTSIDE_PREFIX)


class ExternalActionAdapter(Protocol):
    """Trusted platform client; implementations own native service auth and idempotency.

    Preparation and policy checks must have no external effects. Submission must
    independently enforce the actor, project, live attempt, policy, expiry and
    content binding using the service's own authorization and operation contract.
    Repeating an operation ID must never create a second effect. Observations are
    read-only and cannot manufacture a new submission. No worker receives this client.
    """

    @property
    def tool_names(self) -> frozenset[str]:
        """Native client-tool names supplied by trusted deployment configuration."""
        ...

    @property
    def policy_sha256(self) -> str:
        """Return the current trusted service policy revision."""
        ...

    def prepare(self, tool_name: str, arguments: dict[str, JsonValue]) -> ExternalActionRequest:
        """Resolve a native request and immutable references without performing an effect."""
        ...

    def check_policy(self, proposal: ExternalActionProposal, actor_id: str, now: datetime) -> None:
        """Raise when current deployment policy or actor entitlement forbids dispatch."""
        ...

    def read_input(self, reference: ExternalInputReference) -> bytes:
        """Read bounded bytes from trusted input storage; the caller verifies them before use."""
        ...

    def submit(
        self,
        proposal: ExternalActionProposal,
        decision: ExternalActionDecision,
        inputs: tuple[bytes, ...],
    ) -> ExternalActionObservation:
        """Submit once with independently enforced authority through the native service."""
        ...

    def observe(self, proposal: ExternalActionProposal) -> ExternalActionObservation:
        """Query the existing operation identity without submitting or retrying an effect."""
        ...

    def prepare_result(self, record: ExternalActionRecord) -> str:
        """Read a sanitized bounded JSON result through native service authorization.

        The service must enforce this gateway's entitlement to read the result.
        This is not permission to disclose it to a worker or inference provider.
        Never return credentials or implicitly follow worker-selected paths.
        """
        ...

    def check_result_policy(
        self,
        record: ExternalActionRecord,
        content_json: str,
        model_route_json: str,
        actor_id: str,
        now: datetime,
    ) -> None:
        """Independently authorize exact result disclosure to the reviewed model route."""
        ...
