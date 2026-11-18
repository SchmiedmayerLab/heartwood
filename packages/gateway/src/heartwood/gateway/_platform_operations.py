# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Platform operations on the gateway-owned MCP path: discovery, proposals and plan links.

A destination with an operation profile is a platform service that runs work, such as a
job service. Heartwood reads what the service declares, asks it to describe each
submission before review, and links each reviewed operation, the researcher's decision
and what the platform reported to the plan revision it belongs to. The platform
schedules, accounts for and authorizes every operation; Heartwood does neither.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast

from pydantic import ValidationError

from heartwood.core_adapter.external_actions import (
    ExternalActionError,
    ExternalActionProposal,
    ExternalActionRecord,
)
from heartwood.gateway._mcp_policy import McpDestination
from heartwood.gateway._platform_planning import (
    PlanningRecordError,
    PlanningStateError,
    PlatformPlanning,
)
from heartwood.schemas import (
    BackendRequestReference,
    OperationSetProposal,
    PlanningReference,
    PlatformAuthorization,
    PlatformEstimate,
    PlatformOperationCapabilities,
    PlatformOperationObservation,
    PlatformOperationProposal,
    ProposedOperation,
    ResearcherDecision,
)
from heartwood.session import EventKind

if TYPE_CHECKING:
    from openhands.sdk.mcp import MCPClient

    from heartwood.core_adapter import SessionService

OPTIONAL_CAPABILITIES = ("gpu-resources", "dataset-attachment", "application-discovery")
_REQUEST_PROTOCOL = "mcp-tools-call"


def call_structured(
    client: MCPClient, destination: McpDestination, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    """Call one tool and return its bounded structured content, or refuse the result."""
    from mcp.types import CallToolResult

    result = cast(
        CallToolResult,
        client.call_async_from_sync(
            client.call_tool_mcp, name, arguments, timeout=destination.call_timeout_seconds
        ),
    )
    size = len(result.model_dump_json(by_alias=True, exclude_none=True).encode())
    if size > destination.max_result_bytes:
        raise ExternalActionError("the research service result exceeds its size limit")
    if result.is_error or not isinstance(result.structured_content, dict):
        raise ExternalActionError("the research service did not report a result")
    return result.structured_content


def request_proposal(
    client: MCPClient,
    destination: McpDestination,
    tool: str,
    project_id: str,
    arguments: Mapping[str, Any],
) -> PlatformOperationProposal | None:
    """Ask the service what it would run; return None when it describes nothing usable."""
    assert destination.operations is not None
    try:
        proposal = PlatformOperationProposal.model_validate_json(
            json.dumps(
                call_structured(
                    client,
                    destination,
                    destination.operations.proposal_tool,
                    {"project_id": project_id, "tool": tool, "arguments": dict(arguments)},
                )
            )
        )
    except (ExternalActionError, ValidationError, ValueError, TimeoutError, OSError):
        return None
    if proposal.service_id != destination.service_id or proposal.tool != tool:
        return None
    return proposal


def operations_report(
    client: MCPClient | None,
    destination: McpDestination,
    project_id: str | None,
    definitions: Mapping[str, str],
) -> dict[str, object] | None:
    """Combine what a service declares with what it lists and the deployment allows.

    The report describes; it authorizes nothing, and the platform checks every call.
    """
    if destination.operations is None:
        return None
    declared: PlatformOperationCapabilities | None = None
    if client is not None and project_id is not None:
        try:
            declared = PlatformOperationCapabilities.model_validate_json(
                json.dumps(
                    call_structured(
                        client,
                        destination,
                        destination.operations.capabilities_tool,
                        {"project_id": project_id},
                    )
                )
            )
        except (ExternalActionError, ValidationError, ValueError, TimeoutError, OSError):
            declared = None
        if declared is not None and declared.service_id != destination.service_id:
            declared = None
    roles = _deployment_roles(destination)
    operations = []
    for item in () if declared is None else declared.operations:
        definition = definitions.get(item.tool)
        read_only = _read_only(definition)
        operations.append(
            {
                "tool": item.tool,
                "operation": item.operation,
                "required_permission": item.required_permission,
                "permitted": item.permitted,
                "listed": definition is not None,
                "read_only": read_only,
                "consistent": roles.get(item.tool) == item.operation
                and read_only is (item.operation in {"status", "proposal"}),
            }
        )
    offered = {} if declared is None else {i.capability: i for i in declared.optional_capabilities}
    return {
        "status": "unavailable" if declared is None else "available",
        "operations": operations,
        "capabilities": [
            {
                "capability": name,
                "status": offered[name].status if name in offered else "unavailable",
                "reason": offered[name].reason if name in offered else None,
            }
            for name in OPTIONAL_CAPABILITIES
        ],
        "limitations": [] if declared is None else list(declared.limitations),
    }


def _deployment_roles(destination: McpDestination) -> dict[str, str]:
    assert destination.operations is not None
    roles = {
        destination.observation_tool: "status",
        destination.operations.proposal_tool: "proposal",
    }
    for tool in destination.tools:
        roles[tool] = "cancel" if destination.cancels(tool) else "submit"
    return roles


def _read_only(definition: str | None) -> bool | None:
    if definition is None:
        return None
    annotations = json.loads(definition).get("annotations")
    value = annotations.get("readOnlyHint") if isinstance(annotations, dict) else None
    return value if isinstance(value, bool) else None


def proposal_reference(proposal: ExternalActionProposal) -> PlanningReference:
    """Return the planning identity of one reviewed operation."""
    return PlanningReference(record_id=f"operation-{proposal.operation_id}", revision=1)


def operation_set_document(
    proposal: ExternalActionProposal, estimate: PlatformEstimate | None
) -> bytes | None:
    """Derive the exact reviewed-operation document for a submission bound to a plan element.

    The document follows from the reviewed proposal alone, so its digest is fixed by the
    researcher's decision on that proposal.
    """
    request = proposal.request
    review = request.operation
    if (
        review is None
        or review.kind != "submit"
        or review.proposal is None
        or review.estimate is None
        or review.estimate.element_id is None
        or review.estimate.plan is None
        or review.estimate.prediction is None
        or request.project_context_id is None
        or (review.estimate.estimate_id is None) != (estimate is None)
    ):
        return None
    platform = review.proposal
    document = OperationSetProposal(
        project_context_id=request.project_context_id,
        proposal=proposal_reference(proposal),
        plan=review.estimate.plan,
        prediction=review.estimate.prediction,
        created_at=proposal.created_at,
        operations=(
            ProposedOperation(
                operation_id=str(proposal.operation_id),
                element_ids=(review.estimate.element_id,),
                description=platform.description,
                resource_limits=platform.resource_limits,
                data_scope=platform.data_scope,
                destination=platform.destination,
                estimate_ids=() if estimate is None else (estimate.estimate_id,),
                request=BackendRequestReference(
                    service_id=request.service_id,
                    protocol=_REQUEST_PROTOCOL,
                    request_ref=request.tool_name,
                    request_sha256=hashlib.sha256(request.request_json.encode()).hexdigest(),
                ),
            ),
        ),
        estimates=() if estimate is None else (estimate,),
    )
    return json.dumps(
        document.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()


def link_operations(
    service: SessionService, planning: PlatformPlanning, *, after_sequence: int
) -> int:
    """Link reviewed submissions, decisions and platform reports recorded after a sequence.

    Each reviewed submission that names a plan element is linked with its exact document;
    each researcher decision on it becomes a researcher decision record with the session
    event as audit reference; each observation is linked, and the first one is recorded as
    the platform's admission decision. Returns the last sequence read.
    """
    events = service.replay_events()
    first_observed: set[str] = set()
    last = after_sequence
    for event in events:
        if event.kind != EventKind.EXTERNAL_ACTION_RECORDED:
            continue
        record = ExternalActionRecord.model_validate_json(json.dumps(event.payload["record"]))
        operation_id = str(record.proposal.operation_id)
        admission = record.observation is not None and operation_id not in first_observed
        if record.observation is not None:
            first_observed.add(operation_id)
        if event.sequence <= after_sequence:
            continue
        last = event.sequence
        try:
            _link_record(planning, record, event.event_id, admission=admission)
        except (PlanningRecordError, PlanningStateError):
            # The planning projection reports a damaged journal; a link that does not fit
            # its plan revision is left out.
            continue
    return last


def _link_record(
    planning: PlatformPlanning, record: ExternalActionRecord, event_id: str, *, admission: bool
) -> None:
    proposal = record.proposal
    review = proposal.request.operation
    if (
        review is None
        or review.estimate is None
        or proposal.request.project_context_id != planning.launch.project_context_id
    ):
        return
    estimate = (
        None
        if review.estimate.estimate_id is None
        else planning.estimate(review.estimate.estimate_id)
    )
    document = operation_set_document(proposal, estimate)
    if document is None:
        return
    reference = proposal_reference(proposal)
    digest = hashlib.sha256(document).hexdigest()
    if record.phase == "proposed":
        planning.link_operation_set(
            document, reference=f"outside-operation:{proposal.operation_id}"
        )
    decided = record.withdrawal if record.phase == "withdrawn" else record.decision
    if decided is not None and record.phase in {"authorized", "denied", "withdrawn"}:
        planning.link_platform_record(
            ResearcherDecision(
                project_context_id=planning.launch.project_context_id,
                decision_id=decided.command_id,
                proposal=reference,
                document_sha256=digest,
                decision="withdrawn" if record.phase == "withdrawn" else decided.decision,
                decided_at=decided.decided_at,
                actor_ref=decided.actor_id,
                audit_ref=f"session-event:{event_id}",
            )
        )
    if record.observation is None:
        return
    observed = record.observation.operation
    if admission and observed.status != "unknown":
        planning.link_platform_record(_admission(observed, reference, digest))
    planning.link_platform_record(observed)


def _admission(
    observed: PlatformOperationObservation, reference: PlanningReference, digest: str
) -> PlatformAuthorization:
    """Record the platform's admission decision as the submission's first observation reports it."""
    return PlatformAuthorization(
        project_context_id=observed.project_context_id,
        authorization_id=f"admission-{observed.operation_id}",
        proposal=reference,
        document_sha256=digest,
        status="denied" if observed.status == "denied" else "authorized",
        observed_at=observed.observed_at,
        reason="; ".join(observed.limitations)
        or f"The platform reported the operation as {observed.status}.",
        platform_record_ref=observed.platform_record_ref,
    )
