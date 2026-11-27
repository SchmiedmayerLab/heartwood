# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Plan research work against the budgets and estimates a platform reports.

The platform prices work, keeps allocations, reservations and spending, and authorizes
every operation. This module asks its planning service for budget observations and
estimates through a trusted client, keeps them with the plan revisions they belong to,
and projects one overview for every interface. It calculates no price, keeps no
ledger, monitors no spending and submits no work.
"""

from __future__ import annotations

import hashlib
import json
import ssl
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, Any, Literal, Protocol, Self, cast
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from heartwood.core_adapter.external_actions import (
    ExternalActionError,
    ExternalOperationEstimate,
    canonical_request,
)
from heartwood.gateway._access import (
    AccessUnavailableError,
    ServiceTokens,
    TokenExchangeRefusedError,
)
from heartwood.gateway._diagnostics import DiagnosticDefinition, diagnostic_for
from heartwood.gateway._mcp_policy import (
    McpServerError,
    TokenExchange,
    check_resolved_host,
    exact_origin,
)
from heartwood.persistence import DurableFileError, LockedJsonlStore
from heartwood.schemas import (
    BudgetObservation,
    JsonValue,
    PlanningOverview,
    PlanningReference,
    PlatformAuthorization,
    PlatformEstimate,
    PlatformLaunchContext,
    PlatformOperationObservation,
    ResearcherDecision,
    ReviewedOperationSet,
    WorkloadPrediction,
    reviewed_operation_set,
)
from heartwood.schemas.planning import (
    ElementReview,
    EstimateComparison,
    EstimateState,
    ObservationEvidence,
    PlanningAgentTools,
    PlanningEstimateRequest,
    PlanningOutcome,
    PlanningProjection,
    PlanningServices,
    PlanningServiceStatus,
    PlanProjection,
    PlanRevision,
)

if TYPE_CHECKING:
    from fastmcp.client.transports import StreamableHttpTransport
    from mcp.types import CallToolResult
    from openhands.sdk.mcp import MCPClient
    from openhands.sdk.tool.client_tool import ClientToolSpec

    from heartwood.gateway.platform_conformance import McpEndpoint

type ServiceFailure = Literal["unavailable", "refused"]
type _Record = (
    WorkloadPrediction
    | PlatformEstimate
    | BudgetObservation
    | ReviewedOperationSet
    | ResearcherDecision
    | PlatformAuthorization
    | PlatformOperationObservation
)

BUDGET_TOOL = "get_budgets"
ESTIMATE_TOOL = "estimate_workload"
# The tools a worker's agent calls; each call runs only after the researcher reviews it.
OVERVIEW_AGENT_TOOL = "planning_overview"
ESTIMATE_AGENT_TOOL = "request_plan_estimate"
PREDICTION_AGENT_TOOL = "record_plan_prediction"
# The service identity of agent planning calls in outside-operation records.
PLANNING_SERVICE_ID = "heartwood-planning"
# The largest planning result released to an agent, as canonical JSON.
AGENT_RESULT_BYTES = 48 * 1024
# The request of a call refused before it runs; no planning tool takes this argument.
_REFUSAL = "refusal"
_TIMEOUT_SECONDS = 15.0
_MAX_RESULT_BYTES = 256 * 1024
_KINDS: dict[str, type[BaseModel]] = {
    "prediction": WorkloadPrediction,
    "estimate": PlatformEstimate,
    "budget": BudgetObservation,
    "operation-set": ReviewedOperationSet,
    "decision": ResearcherDecision,
    "authorization": PlatformAuthorization,
    "operation": PlatformOperationObservation,
}


class PlanningServiceError(Exception):
    """The planning service could not be reached, answered invalidly, or refused a call."""

    def __init__(self, failure: ServiceFailure) -> None:
        """Keep only the outcome; service text is untrusted and never shown."""
        super().__init__(failure)
        self.failure: ServiceFailure = failure


class PlanningRecordError(ValueError):
    """A plan record conflicts with recorded planning state or the launch context."""


class PlanningStateError(RuntimeError):
    """The local planning journal requires operator attention before it can be used."""


class PlanningNotOfferedError(RuntimeError):
    """This deployment connects no planning service."""


@dataclass(frozen=True, slots=True)
class PlanningDiscovery:
    """Which planning capabilities the service offers to the calling identity."""

    budgets: bool
    estimates: bool


class PlanningServiceClient(Protocol):
    """A trusted client for one platform planning service and one launch context.

    Implementations hold the platform credential outside model context and worker
    environments and return parsed records; Heartwood still checks their bindings.
    """

    def discover(self) -> PlanningDiscovery:
        """Report the offered capabilities without assuming any of them."""

    def budgets(self) -> tuple[BudgetObservation, ...]:
        """Return the platform's current budget observations."""

    def estimate(
        self,
        prediction: WorkloadPrediction,
        *,
        scope: Literal["plan", "elements"],
        element_ids: Sequence[str],
    ) -> PlatformEstimate:
        """Return the platform's estimate for the prediction."""


class McpPlanningClient:
    """Call a planning service's read-only MCP tools with a platform-issued token.

    ``get_budgets`` returns ``{"budgets": [...]}`` budget observations and
    ``estimate_workload`` returns one platform estimate. A tool counts as offered only
    when the server lists it as read-only, so an estimate that would persist data,
    charge or reserve is never requested here. Annotations are hints and authorize
    nothing; the service enforces project scope and its own policy on every call.
    """

    def __init__(self, endpoint: McpEndpoint, *, project_id: str) -> None:
        """Call the service at ``endpoint`` for the platform project ``project_id``."""
        self.endpoint = endpoint
        self.project_id = project_id

    def discover(self) -> PlanningDiscovery:
        """List the tools and report which planning tools are offered read-only."""
        result = self._session(lambda client: client.list_tools_mcp())
        offered = {
            tool.name
            for tool in result.tools
            if tool.annotations is not None and tool.annotations.read_only_hint is True
        }
        return PlanningDiscovery(BUDGET_TOOL in offered, ESTIMATE_TOOL in offered)

    def budgets(self) -> tuple[BudgetObservation, ...]:
        """Return the project's budget observations."""
        content = self._call(BUDGET_TOOL, {"project_id": self.project_id})
        items = content.get("budgets")
        if not isinstance(items, list):
            raise PlanningServiceError("unavailable")
        return tuple(cast(BudgetObservation, _parse(BudgetObservation, item)) for item in items)

    def estimate(
        self,
        prediction: WorkloadPrediction,
        *,
        scope: Literal["plan", "elements"],
        element_ids: Sequence[str],
    ) -> PlatformEstimate:
        """Return the platform's estimate for the whole prediction or selected elements."""
        content = self._call(
            ESTIMATE_TOOL,
            {
                "project_id": self.project_id,
                "scope": scope,
                "prediction": prediction.model_dump(mode="json"),
                "element_ids": list(element_ids) if scope == "elements" else None,
            },
        )
        return cast(PlatformEstimate, _parse(PlatformEstimate, content))

    def _call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result: CallToolResult = self._session(lambda client: client.call_tool_mcp(tool, arguments))
        if result.is_error:
            raise PlanningServiceError("refused")
        content = result.structured_content
        if not isinstance(content, dict) or len(json.dumps(content).encode()) > _MAX_RESULT_BYTES:
            raise PlanningServiceError("unavailable")
        return content

    def _transport(self) -> StreamableHttpTransport:
        from fastmcp.client.transports import StreamableHttpTransport

        return StreamableHttpTransport(str(self.endpoint.profile.url), auth=self.endpoint.token)

    def _session[T](self, request: Callable[[MCPClient], Awaitable[T]]) -> T:
        # The MCP client loads with the OpenHands SDK, so it is imported when first used.
        from mcp.shared.exceptions import MCPError as McpProtocolError
        from openhands.sdk.mcp import MCPClient
        from openhands.sdk.mcp.exceptions import MCPError

        client = MCPClient(self._transport(), timeout=_TIMEOUT_SECONDS)  # type: ignore[no-untyped-call]
        try:
            client.call_async_from_sync(client.connect, timeout=_TIMEOUT_SECONDS)
            return cast(T, client.call_async_from_sync(request(client), timeout=_TIMEOUT_SECONDS))
        except (MCPError, McpProtocolError, OSError, TimeoutError):
            # Transport and authentication failures carry server text; keep only the outcome.
            raise PlanningServiceError("unavailable") from None
        finally:
            client.sync_close()


class PlanningDeployment(BaseModel):
    """A deployment's planning service, reached with a token exchanged for the sign-in.

    The launch context file holds the platform launch context that the platform issued
    for this gateway; project files and agent-writable settings cannot replace it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    url: str = Field(min_length=1, max_length=2048)
    # Certificate authorities that replace the default bundle for the service.
    ca_file: Path | None = None
    # The operator's opt-in for a cluster or other private address of the service.
    private_address: bool = False
    token_exchange: TokenExchange
    launch_context_file: Path

    @model_validator(mode="after")
    def _service_location(self) -> Self:
        parts = urlsplit(self.url)
        if parts.query or parts.fragment or parts.username or parts.password:
            raise ValueError("the planning URL has no credentials, query or fragment")
        exact_origin(f"{parts.scheme}://{parts.netloc}", private_address=self.private_address)
        if parts.scheme != "https":
            raise ValueError("the planning service is reached over HTTPS")
        for path in (self.ca_file, self.launch_context_file):
            if path is not None and not path.is_absolute():
                raise ValueError("planning files are absolute paths")
        return self


class HostedPlanningClient(McpPlanningClient):
    """Call the planning tools with a token exchanged for the researcher's sign-in.

    Each request exchanges the sign-in for the deployment's scope and audience, checks
    the service address before connecting, and trusts the deployment's certificate
    authorities. Without a live sign-in the service counts as unavailable.
    """

    def __init__(
        self, deployment: PlanningDeployment, tokens: ServiceTokens, *, project_id: str
    ) -> None:
        """Call the service of ``deployment`` for the platform project ``project_id``."""
        self.deployment = deployment
        self.tokens = tokens
        self.project_id = project_id

    def _transport(self) -> StreamableHttpTransport:
        from fastmcp.client.transports import StreamableHttpTransport

        deployment = self.deployment
        # The address and the authorities are checked before a token is exchanged.
        parts = urlsplit(deployment.url)
        try:
            check_resolved_host(
                parts.hostname or "", parts.port or 443, private_address=deployment.private_address
            )
            verify = (
                None
                if deployment.ca_file is None
                else ssl.create_default_context(cafile=str(deployment.ca_file))
            )
        except (McpServerError, OSError, ValueError):
            raise PlanningServiceError("unavailable") from None
        exchange = deployment.token_exchange
        try:
            token = self.tokens(exchange.scope, exchange.audience)
        except AccessUnavailableError:
            raise PlanningServiceError("unavailable") from None
        except TokenExchangeRefusedError:
            raise PlanningServiceError("refused") from None
        if token is None:
            raise PlanningServiceError("unavailable")
        return StreamableHttpTransport(deployment.url, auth=token, verify=verify)


def _parse(model: type[BaseModel], value: object) -> BaseModel:
    try:
        return model.model_validate_json(json.dumps(value))
    except (ValidationError, TypeError, ValueError):
        raise PlanningServiceError("unavailable") from None


class _RepeatError(Exception):
    """Stop an exact repeat before the appender writes a second line."""


@dataclass(frozen=True, slots=True)
class _Entry:
    sequence: int
    record: _Record


class PlanningJournal:
    """Owner-private record of plan revisions and the platform records linked to them.

    It holds observations and decisions, never balances Heartwood computes. Each append
    is validated against the recorded entries under the journal's lock.
    """

    def __init__(self, path: Path) -> None:
        """Use a dedicated file in the project's protected state."""
        self._store = LockedJsonlStore(path)

    def entries(self) -> tuple[_Entry, ...]:
        """Return every recorded entry in order."""
        try:
            return _entries(self._store.read_objects())
        except DurableFileError:
            raise PlanningStateError("Planning records require integrity recovery") from None

    def append(self, record: _Record, check: Callable[[tuple[_Entry, ...]], bool]) -> None:
        """Append ``record`` when ``check`` accepts it; ``check`` returns False for a repeat."""
        kind = next(name for name, model in _KINDS.items() if isinstance(record, model))

        def build(objects: tuple[dict[str, Any], ...]) -> dict[str, Any]:
            if not check(_entries(objects)):
                raise _RepeatError
            return {
                "schema_version": "heartwood.planning-entry.v2",
                "kind": kind,
                "record": record.model_dump(mode="json"),
            }

        try:
            self._store.append_derived(build)
        except _RepeatError:
            return
        except DurableFileError:
            raise PlanningStateError("Planning records require integrity recovery") from None


def _legacy_prediction(value: object) -> bool:
    """Distinguish valid free-text v1 references from a damaged prediction."""
    if not isinstance(value, dict) or not isinstance(value.get("evidence"), list):
        return False
    evidence = []
    for item in value["evidence"]:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("reference"), str)
            or not item["reference"].strip()
        ):
            return False
        evidence.append({**item, "reference": "project://legacy"})
    try:
        WorkloadPrediction.model_validate_json(json.dumps({**value, "evidence": evidence}))
    except ValidationError:
        return False
    return True


def _entries(objects: tuple[dict[str, Any], ...]) -> tuple[_Entry, ...]:
    entries: list[_Entry] = []
    for sequence, item in enumerate(objects, 1):
        model = _KINDS.get(str(item.get("kind")))
        version = item.get("schema_version")
        if (
            version not in ("heartwood.planning-entry.v1", "heartwood.planning-entry.v2")
            or model is None
        ):
            raise PlanningStateError("Persisted planning record is invalid")
        try:
            record = model.model_validate_json(json.dumps(item.get("record")))
        except ValidationError:
            if (
                version == "heartwood.planning-entry.v1"
                and model is WorkloadPrediction
                and _legacy_prediction(item.get("record"))
            ):
                raise PlanningStateError(
                    "This planning journal uses earlier evidence references. Preserve the journal "
                    "before starting a new one. See Research Planning in Troubleshooting."
                ) from None
            raise PlanningStateError("Persisted planning record is invalid") from None
        entries.append(_Entry(sequence, cast(_Record, record)))
    return tuple(entries)


@dataclass(frozen=True, slots=True)
class PlatformPlanningBinding:
    """A deployment's planning service and the platform launch context it was issued.

    The launcher supplies both from deployment-owned configuration; project files and
    agent-writable settings cannot select or replace the platform project or billing
    binding.
    """

    launch: PlatformLaunchContext
    client: PlanningServiceClient
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)


class PlatformPlanning:
    """Gateway-owned planning state for one project and launch context.

    Platform calls run without the gateway's state lock; the journal serializes appends
    across threads and processes, and this object's lock guards the service status.
    """

    def __init__(
        self,
        binding: PlatformPlanningBinding,
        *,
        journal_path: Path,
        contains_file: Callable[[Path], bool],
        run_ids: Callable[[], set[str]],
        agent_tools_unavailable: str | None,
    ) -> None:
        """Keep planning records in ``journal_path``; the binding's clock judges validity.

        ``agent_tools_unavailable`` is why no session's agent receives the planning tools,
        or None when worker sessions receive them.
        """
        self.launch = binding.launch
        self.client = binding.client
        self.journal = PlanningJournal(journal_path)
        self.clock = binding.clock
        self._contains_file = contains_file
        self._run_ids = run_ids
        self._agent_tools = _agent_tool_availability(agent_tools_unavailable)
        self._lock = Lock()
        self._discovery: PlanningDiscovery | None = None
        self._services = PlanningServices(budgets="unchecked", estimates="unchecked")

    def refresh(self) -> None:
        """Discover the offered capabilities and read the current budgets."""
        discovery = self._discover()
        if discovery is None or not discovery.budgets:
            return
        try:
            observations = self.client.budgets()
        except PlanningServiceError as error:
            self._set("budgets", error.failure)
            return
        if any(item.project_context_id != self.launch.project_context_id for item in observations):
            self._set("budgets", "unavailable")
            return
        for observation in observations:
            self.journal.append(observation, self._scoped(_new_budget(observation)))
        self._set("budgets", "available")

    def record_prediction(self, prediction: WorkloadPrediction) -> None:
        """Record one immutable prediction revision for this launch context."""
        if prediction.project_context_id != self.launch.project_context_id:
            raise PlanningRecordError("The prediction belongs to another platform project context")

        runs = {
            item.reference.removeprefix("experiment-run:")
            for item in prediction.evidence
            if item.reference.startswith("experiment-run:")
        }
        if runs and not runs <= self._run_ids():
            raise PlanningRecordError("Experiment run is not recorded in this project")
        for item in prediction.evidence:
            if item.reference.startswith("project://") and not self._contains_file(
                Path(item.reference.removeprefix("project://"))
            ):
                raise PlanningRecordError("Evidence path is outside the research project")

        def check(entries: tuple[_Entry, ...]) -> bool:
            predictions = _predictions(entries)
            key = _key(prediction.prediction)
            if key in predictions:
                if predictions[key] != prediction:
                    raise PlanningRecordError("This prediction revision is already recorded")
                return False
            if prediction.supersedes is None:
                if prediction.prediction.revision != 1:
                    raise PlanningRecordError(
                        "A later prediction revision must name the one it supersedes"
                    )
            else:
                superseded = predictions.get(_key(prediction.supersedes))
                if superseded is None:
                    raise PlanningRecordError("The superseded prediction revision is not recorded")
                if (
                    superseded.plan.record_id != prediction.plan.record_id
                    or superseded.plan.revision > prediction.plan.revision
                ):
                    raise PlanningRecordError(
                        "A revision must keep its plan and must not lower the plan revision"
                    )
            cited = {
                item.reference
                for item in prediction.evidence
                if item.reference.startswith("observation:")
            }
            if cited and not cited <= {
                _observation_reference(record)
                for record in _of(entries, PlatformOperationObservation)
            }:
                raise PlanningRecordError("Evidence operation observation is not recorded")
            prior_predictions = {
                item.reference
                for item in prediction.evidence
                if item.reference.startswith("prediction:")
            }
            if prior_predictions and not prior_predictions <= {
                f"prediction:{name}@{number}" for name, number in predictions
            }:
                raise PlanningRecordError("Evidence prediction revision is not recorded")
            return True

        self.journal.append(prediction, self._scoped(check))

    def request_estimate(self, request: PlanningEstimateRequest) -> None:
        """Ask the platform to estimate a recorded prediction and record its answer."""
        prediction = _predictions(self._entries()).get(_key(request.prediction))
        if prediction is None:
            raise PlanningRecordError("The prediction revision is not recorded")
        elements = {item.element_id for item in prediction.elements}
        if (request.scope == "plan") == bool(request.element_ids) or not set(
            request.element_ids
        ) <= elements:
            raise PlanningRecordError(
                "Name existing elements for an element estimate, and none for a plan estimate"
            )
        with self._lock:
            discovery = self._discovery
        if discovery is None:
            discovery = self._discover()
        if discovery is None or not discovery.estimates:
            return
        try:
            estimate = self.client.estimate(
                prediction, scope=request.scope, element_ids=request.element_ids
            )
        except PlanningServiceError as error:
            self._set("estimates", error.failure)
            return
        if not _answers(estimate, prediction, request):
            self._set("estimates", "unavailable")
            return

        def check(entries: tuple[_Entry, ...]) -> bool:
            recorded = _of(entries, PlatformEstimate)
            if estimate in recorded:
                return False
            if any(item.estimate_id == estimate.estimate_id for item in recorded):
                raise _InvalidAnswerError
            try:
                _overview((*entries, _Entry(_next(entries), estimate)), prediction)
            except ValueError:
                raise _InvalidAnswerError from None
            return True

        try:
            self.journal.append(estimate, self._scoped(check))
        except _InvalidAnswerError:
            self._set("estimates", "unavailable")
            return
        self._set("estimates", "available")

    def link_operation_set(self, document: bytes, *, reference: str) -> None:
        """Link the exact proposal document a researcher reviews to its plan revision."""
        try:
            reviewed = reviewed_operation_set(document, reference=reference)
        except ValueError:
            raise PlanningRecordError("The proposal document is invalid") from None
        self._link(reviewed)

    def link_platform_record(
        self, record: ResearcherDecision | PlatformAuthorization | PlatformOperationObservation
    ) -> None:
        """Link a researcher decision or a platform authorization or operation observation.

        The caller is the trusted embedding that obtained the record from the session's
        action review or from the platform; Heartwood checks its links, not its origin.
        """
        self._link(record)

    def estimate(self, estimate_id: str) -> PlatformEstimate | None:
        """Return a recorded estimate of this launch context by identity."""
        return next(
            (
                item
                for item in _of(self._entries(), PlatformEstimate)
                if item.estimate_id == estimate_id
            ),
            None,
        )

    def operation_estimate(self, runtime_profile_ref: str) -> ExternalOperationEstimate:
        """Locate the batch element and the latest estimate that a platform operation runs.

        The element is the one batch element of the latest revision of each plan that uses
        the operation's runtime profile. No element, several elements, or no estimate
        covering the element are reported as a missing estimate with the reason.
        """
        entries = self._entries()
        latest: dict[str, WorkloadPrediction] = {}
        for prediction in _predictions(entries).values():
            current = latest.get(prediction.plan.record_id)
            order = (prediction.plan.revision, prediction.prediction.revision)
            if current is None or order > (current.plan.revision, current.prediction.revision):
                latest[prediction.plan.record_id] = prediction
        candidates = [
            (prediction, element)
            for _, prediction in sorted(latest.items())
            for element in prediction.elements
            if element.kind == "batch" and element.runtime_profile_ref == runtime_profile_ref
        ]
        if len(candidates) != 1:
            return ExternalOperationEstimate(
                state="missing",
                limitation=(
                    f"No batch element of a current plan revision uses {runtime_profile_ref}."
                    if not candidates
                    else f"Several batch elements use {runtime_profile_ref}."
                )[:512],
            )
        prediction, element = candidates[0]
        covering = [
            item
            for item in _of(entries, PlatformEstimate)
            if _key(item.prediction) == _key(prediction.prediction)
            and element.element_id in item.covered_element_ids
        ]
        estimate = covering[-1] if covering else None
        component = (
            None
            if estimate is None
            else next(
                (
                    item.cost
                    for item in estimate.element_estimates
                    if item.included_in_total and item.element_ids == (element.element_id,)
                ),
                None,
            )
        )
        return ExternalOperationEstimate(
            state=(
                "missing"
                if estimate is None
                else "expired"
                if self.clock() >= estimate.valid_until
                else "current"
            ),
            element_id=element.element_id,
            plan=prediction.plan,
            prediction=prediction.prediction,
            estimate_id=None if estimate is None else estimate.estimate_id,
            total=None if estimate is None else component or estimate.total,
            valid_until=None if estimate is None else estimate.valid_until,
            limitation=(
                "No estimate covers this plan element."
                if estimate is None
                else None
                if component is not None or estimate.covered_element_ids == (element.element_id,)
                else "The estimate total covers more than this element."
            ),
        )

    def projection(self) -> PlanningProjection:
        """Return plans, estimates, budgets and outcomes as of now."""
        entries = self._entries()
        now = self.clock()
        budgets = _latest_budgets(entries)
        plans: dict[str, list[WorkloadPrediction]] = {}
        for prediction in _predictions(entries).values():
            plans.setdefault(prediction.plan.record_id, []).append(prediction)
        with self._lock:
            services = self._services
        outcomes = _service_outcomes(services)
        observations = tuple(
            ObservationEvidence(
                reference=_observation_reference(record),
                operation_id=record.operation_id,
                service_id=record.service_id,
                backend_operation_ref=record.backend_operation_ref,
                observed_at=record.observed_at,
                status=record.status,
                platform_record_ref=record.platform_record_ref,
            )
            for record in _of(entries, PlatformOperationObservation)
        )
        projected: list[PlanProjection] = []
        for plan_id in sorted(plans):
            revisions = sorted(
                plans[plan_id],
                key=lambda item: (item.plan.revision, _key(item.prediction)),
            )
            by_prediction: dict[tuple[str, int], PlanRevision] = {}
            for prediction in revisions:
                parent = (
                    by_prediction[_key(prediction.supersedes)]
                    if prediction.supersedes is not None
                    else None
                )
                by_prediction[_key(prediction.prediction)] = _revision(
                    entries, prediction, parent, now, observations
                )
            views = tuple(by_prediction.values())
            outcomes.extend(_revision_outcomes(views[-1]))
            projected.append(
                PlanProjection(plan_id=plan_id, revisions=views, comparison=_comparison(views))
            )
        return PlanningProjection(
            offered=True,
            launch=self.launch,
            services=services,
            agent_tools=self._agent_tools,
            budgets=tuple(budgets[key] for key in sorted(budgets)),
            plans=tuple(projected),
            outcomes=tuple(outcomes),
        )

    # Agent tools

    @property
    def client_tools(self) -> tuple[ClientToolSpec, ...]:
        """Return the fixed planning tools a worker's agent receives."""
        return _agent_tools()

    def agent_request(self, tool_name: str, arguments: dict[str, JsonValue]) -> tuple[str, str]:
        """Check an agent's planning call and return its review summary and exact request.

        Nothing is recorded or sent. A call this state refuses, such as a prediction for
        another launch context or arguments the tool does not define, becomes a refusal:
        its review names the reason, and once released the agent receives that reason.
        """
        try:
            summary, content = self._agent_request(tool_name, arguments)
            return summary, canonical_request(content)
        except PlanningRecordError as error:
            reason = str(error)
        except ExternalActionError:
            reason = "The planning call is invalid or exceeds the request size limit"
        return f"Tell the agent its {tool_name} call was refused: {reason}"[
            :512
        ], canonical_request(json.dumps({_REFUSAL: reason}))

    def _agent_request(self, tool_name: str, values: dict[str, JsonValue]) -> tuple[str, str]:
        if tool_name == OVERVIEW_AGENT_TOOL and not values:
            return "Read the platform planning overview", "{}"
        if tool_name == PREDICTION_AGENT_TOOL and set(values) == {"prediction"}:
            prediction = cast(WorkloadPrediction, _parsed(WorkloadPrediction, values["prediction"]))
            if prediction.project_context_id != self.launch.project_context_id:
                raise PlanningRecordError(
                    "The prediction belongs to another platform project context"
                )
            reference = prediction.prediction
            return (
                f"Record workload prediction {reference.record_id} revision {reference.revision}"
            )[:512], json.dumps({"prediction": prediction.model_dump(mode="json")})
        if tool_name == ESTIMATE_AGENT_TOOL:
            request = cast(PlanningEstimateRequest, _parsed(PlanningEstimateRequest, values))
            reference = request.prediction
            return (
                f"Request a platform estimate for prediction {reference.record_id} "
                f"revision {reference.revision}"
            )[:512], request.model_dump_json()
        raise PlanningRecordError("The arguments do not match the planning tool")

    def agent_call(
        self, tool_name: str, request_json: str, *, operation_id: str
    ) -> PlatformOperationObservation:
        """Run a reviewed planning call through the same methods every interface uses.

        The result is the planning overview afterwards. A refusal, a refused record or a
        journal that needs recovery is reported as a failed call with the reason; a
        refusal records and contacts nothing.
        """
        request = json.loads(request_json)
        if _REFUSAL in request:
            return self._agent_failure(tool_name, operation_id, str(request[_REFUSAL]))
        try:
            if tool_name == OVERVIEW_AGENT_TOOL:
                self.refresh()
            elif tool_name == PREDICTION_AGENT_TOOL:
                self.record_prediction(
                    WorkloadPrediction.model_validate_json(json.dumps(request["prediction"]))
                )
            elif tool_name == ESTIMATE_AGENT_TOOL:
                self.request_estimate(PlanningEstimateRequest.model_validate_json(request_json))
            else:
                raise PlanningRecordError("This is not a planning tool")
            return self.agent_observation(tool_name, operation_id=operation_id)
        except (PlanningRecordError, PlanningStateError) as error:
            return self._agent_failure(tool_name, operation_id, str(error))

    def agent_observation(
        self, tool_name: str, *, operation_id: str, limitation: str | None = None
    ) -> PlatformOperationObservation:
        """Report the current planning overview as a completed call; this contacts nothing.

        An overview larger than an agent result keeps the latest revision of each plan,
        and then no revisions; service status, budgets and outcomes remain, and the call
        fails only when they alone exceed the limit.
        """
        try:
            full = _agent_view(self.projection(), self.launch)
        except PlanningStateError as error:
            return self._agent_failure(tool_name, operation_id, str(error))
        plans = cast(list[dict[str, Any]], full["plans"])
        latest = [{**plan, "revisions": plan["revisions"][-1:]} for plan in plans]
        shortened = (
            "The overview lists only the latest revision of each plan; "
            "the researcher can read every revision in the browser, terminal or notebook."
        )
        omitted = (
            "The overview lists no plan revisions because they exceed an agent result; "
            "the researcher can read them in the browser, terminal or notebook."
        )
        for view, note in (
            (full, None),
            ({**full, "plans": cast(JsonValue, latest)}, shortened),
            ({**full, "plans": []}, omitted),
        ):
            notes = tuple(item for item in (limitation, note) if item is not None)
            observation = PlatformOperationObservation(
                project_context_id=self.launch.project_context_id,
                operation_id=operation_id,
                service_id=PLANNING_SERVICE_ID,
                backend_operation_ref=tool_name,
                observed_at=self.clock(),
                status="completed",
                platform_record_ref=f"planning:{operation_id}",
                limitations=notes,
                result=view,
            )
            released = json.dumps(
                observation.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
            )
            if len(released.encode()) <= AGENT_RESULT_BYTES:
                return observation
        return self._agent_failure(
            tool_name,
            operation_id,
            "The planning overview is larger than an agent result; "
            "the researcher can read it in the browser, terminal or notebook.",
        )

    def agent_outcome(
        self, tool_name: str, request_json: str, *, operation_id: str
    ) -> PlatformOperationObservation:
        """Report a call whose outcome was not recorded, without running it again.

        A prediction counts as recorded only when the journal holds that exact revision;
        for an estimate the result says whether the journal holds one for the revision,
        scope and elements.
        """
        request = json.loads(request_json)
        if _REFUSAL in request:
            return self._agent_failure(tool_name, operation_id, str(request[_REFUSAL]))
        note = "The outcome of this call was not observed; this is the current planning overview."
        try:
            if tool_name == PREDICTION_AGENT_TOOL:
                prediction = WorkloadPrediction.model_validate_json(
                    json.dumps(request["prediction"])
                )
                if _predictions(self._entries()).get(_key(prediction.prediction)) != prediction:
                    return self._agent_failure(
                        tool_name, operation_id, "The prediction revision was not recorded"
                    )
            elif tool_name == ESTIMATE_AGENT_TOOL:
                estimate = PlanningEstimateRequest.model_validate_json(request_json)
                held = any(
                    _key(item.prediction) == _key(estimate.prediction)
                    and item.scope == estimate.scope
                    and (
                        estimate.scope == "plan"
                        or set(item.covered_element_ids) == set(estimate.element_ids)
                    )
                    for item in _of(self._entries(), PlatformEstimate)
                )
                note = (
                    "The outcome of this call was not observed. The journal "
                    + ("holds an" if held else "holds no")
                    + " estimate for this revision, scope and elements; this is the current "
                    "planning overview."
                )
        except PlanningStateError as error:
            return self._agent_failure(tool_name, operation_id, str(error))
        return self.agent_observation(tool_name, operation_id=operation_id, limitation=note)

    def _agent_failure(
        self, tool_name: str, operation_id: str, reason: str
    ) -> PlatformOperationObservation:
        return PlatformOperationObservation(
            project_context_id=self.launch.project_context_id,
            operation_id=operation_id,
            service_id=PLANNING_SERVICE_ID,
            backend_operation_ref=tool_name,
            observed_at=self.clock(),
            status="failed",
            platform_record_ref=f"planning:{operation_id}",
            limitations=(reason,),
        )

    def _entries(self) -> tuple[_Entry, ...]:
        return self._scope(self.journal.entries())

    def _scope(self, entries: tuple[_Entry, ...]) -> tuple[_Entry, ...]:
        """Keep the records of this launch context; another launch keeps its own."""
        context = self.launch.project_context_id
        return tuple(item for item in entries if _context(item.record) == context)

    def _scoped(
        self, check: Callable[[tuple[_Entry, ...]], bool]
    ) -> Callable[[tuple[_Entry, ...]], bool]:
        return lambda entries: check(self._scope(entries))

    def _link(
        self,
        record: ReviewedOperationSet
        | ResearcherDecision
        | PlatformAuthorization
        | PlatformOperationObservation,
    ) -> None:
        if _context(record) != self.launch.project_context_id:
            raise PlanningRecordError("The record belongs to another platform project context")

        def check(entries: tuple[_Entry, ...]) -> bool:
            identity = _identity(record)
            if any(_identity(item.record) == identity for item in entries):
                if any(item.record == record for item in entries):
                    return False
                raise PlanningRecordError("A different record with this identity is linked")
            extended = (*entries, _Entry(_next(entries), record))
            prediction = _prediction_for(extended, record)
            if prediction is None:
                raise PlanningRecordError("The record does not belong to a recorded plan revision")
            try:
                _overview(extended, prediction)
            except ValueError:
                raise PlanningRecordError(
                    "The record is inconsistent with its plan revision"
                ) from None
            return True

        self.journal.append(record, self._scoped(check))

    def _discover(self) -> PlanningDiscovery | None:
        try:
            discovery = self.client.discover()
        except PlanningServiceError as error:
            with self._lock:
                self._discovery = None
                self._services = PlanningServices(
                    budgets=error.failure, estimates=error.failure, checked_at=self.clock()
                )
            return None
        with self._lock:
            self._discovery = discovery
            self._services = PlanningServices(
                budgets=_offered(discovery.budgets),
                estimates=_offered(discovery.estimates),
                checked_at=self.clock(),
            )
        return discovery

    def _set(
        self, capability: Literal["budgets", "estimates"], status: PlanningServiceStatus
    ) -> None:
        with self._lock:
            self._services = self._services.model_copy(update={capability: status})


class _InvalidAnswerError(Exception):
    """The platform's answer does not fit the recorded plan; it is discarded."""


def _context(record: _Record) -> str:
    if isinstance(record, ReviewedOperationSet):
        return record.proposal.project_context_id
    return record.project_context_id


def _next(entries: tuple[_Entry, ...]) -> int:
    return entries[-1].sequence + 1 if entries else 1


def _observation_reference(record: PlatformOperationObservation) -> str:
    content = json.dumps(record.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return "observation:" + hashlib.sha256(content.encode()).hexdigest()


def _revision(
    entries: tuple[_Entry, ...],
    prediction: WorkloadPrediction,
    previous: PlanRevision | None,
    now: datetime,
    observations: tuple[ObservationEvidence, ...],
) -> PlanRevision:
    """Project one revision; an unchanged element keeps the approval of its last revision."""
    overview = _overview(entries, prediction)
    operations = {item.operation_id for item in overview.operations}
    cited = {
        item.reference for item in prediction.evidence if item.reference.startswith("observation:")
    }
    return PlanRevision(
        observation_evidence=tuple(
            item
            for item in observations
            if item.operation_id in operations or item.reference in cited
        ),
        overview=overview,
        elements=_element_reviews(overview, previous),
        estimates=tuple(
            EstimateState(
                estimate_id=item.estimate_id,
                state="expired" if now >= item.valid_until else "current",
                budget_changed=_budget_changed(entries, item),
            )
            for item in overview.estimates
        ),
    )


def _service_outcomes(services: PlanningServices) -> list[PlanningOutcome]:
    """Report each failed capability; one failure of the whole service is reported once."""
    failed = {
        name: status
        for name, status in (("budget", services.budgets), ("estimate", services.estimates))
        if status in {"unavailable", "refused"}
    }
    outcomes: list[PlanningOutcome] = []
    for status in dict.fromkeys(failed.values()):
        names = [name for name, value in failed.items() if value == status]
        subject = "planning service" if len(names) == 2 else f"{names[0]} service"
        check = "planning-unavailable" if status == "unavailable" else "planning-refused"
        outcomes.append(_outcome(check, subject))
    return outcomes


def _new_budget(observation: BudgetObservation) -> Callable[[tuple[_Entry, ...]], bool]:
    """Skip an observation identical to the latest one of the same budget."""
    return lambda entries: _latest_budgets(entries).get(observation.budget_id) != observation


def _offered(value: bool) -> PlanningServiceStatus:
    return "available" if value else "not-offered"


def _key(reference: PlanningReference) -> tuple[str, int]:
    return reference.record_id, reference.revision


def _of[T](entries: Iterable[_Entry], kind: type[T]) -> list[T]:
    return [item.record for item in entries if isinstance(item.record, kind)]


def _predictions(entries: Iterable[_Entry]) -> dict[tuple[str, int], WorkloadPrediction]:
    return {_key(item.prediction): item for item in _of(entries, WorkloadPrediction)}


def _latest_budgets(entries: Iterable[_Entry]) -> dict[str, BudgetObservation]:
    """Keep each budget's newest observation; a late older one does not replace it."""
    latest: dict[str, BudgetObservation] = {}
    for item in _of(entries, BudgetObservation):
        current = latest.get(item.budget_id)
        if current is None or item.observed_at >= current.observed_at:
            latest[item.budget_id] = item
    return latest


def _identity(record: _Record) -> tuple[str, ...]:
    if isinstance(record, ReviewedOperationSet):
        return ("operation-set", *map(str, _key(record.proposal.proposal)))
    if isinstance(record, ResearcherDecision):
        return ("decision", record.decision_id)
    if isinstance(record, PlatformAuthorization):
        return ("authorization", record.authorization_id)
    if isinstance(record, PlatformOperationObservation):
        return ("operation", _observation_reference(record))
    # Predictions, estimates and budgets have their own repeat checks.
    return ("unlinked",)


def _prediction_for(entries: tuple[_Entry, ...], record: _Record) -> WorkloadPrediction | None:
    sets = _of(entries, ReviewedOperationSet)
    reviewed: ReviewedOperationSet | None
    if isinstance(record, ReviewedOperationSet):
        reviewed = record
    elif isinstance(record, (ResearcherDecision, PlatformAuthorization)):
        reviewed = next(
            (item for item in sets if _key(item.proposal.proposal) == _key(record.proposal)), None
        )
    elif isinstance(record, PlatformOperationObservation):
        reviewed = next(
            (
                item
                for item in sets
                if any(
                    op.operation_id == record.operation_id
                    and op.request.service_id == record.service_id
                    for op in item.proposal.operations
                )
            ),
            None,
        )
    else:
        return None
    if reviewed is None:
        return None
    return _predictions(entries).get(_key(reviewed.proposal.prediction))


def _overview(entries: tuple[_Entry, ...], prediction: WorkloadPrediction) -> PlanningOverview:
    """Build the contract overview for one revision; its validator checks every link."""
    key = _key(prediction.prediction)
    sets = [
        item for item in _of(entries, ReviewedOperationSet) if _key(item.proposal.prediction) == key
    ]
    proposals = {_key(item.proposal.proposal) for item in sets}
    operations = {op.operation_id for item in sets for op in item.proposal.operations}
    latest: dict[str, PlatformOperationObservation] = {}
    for observation in _of(entries, PlatformOperationObservation):
        if observation.operation_id not in operations:
            continue
        current = latest.get(observation.operation_id)
        if current is None or observation.observed_at >= current.observed_at:
            latest[observation.operation_id] = observation
    return PlanningOverview(
        prediction=prediction,
        estimates=tuple(
            item for item in _of(entries, PlatformEstimate) if _key(item.prediction) == key
        ),
        operation_sets=tuple(sets),
        researcher_decisions=tuple(
            item for item in _of(entries, ResearcherDecision) if _key(item.proposal) in proposals
        ),
        authorizations=tuple(
            item for item in _of(entries, PlatformAuthorization) if _key(item.proposal) in proposals
        ),
        budgets=tuple(_latest_budgets(entries).values()),
        operations=tuple(latest.values()),
    )


def _answers(
    estimate: PlatformEstimate, prediction: WorkloadPrediction, request: PlanningEstimateRequest
) -> bool:
    """Accept only an estimate for this launch context, prediction revision and scope."""
    return (
        estimate.project_context_id == prediction.project_context_id
        and estimate.prediction == prediction.prediction
        and estimate.plan == prediction.plan
        and estimate.scope == request.scope
        and (
            request.scope == "plan" or set(estimate.covered_element_ids) == set(request.element_ids)
        )
    )


def _element_reviews(
    overview: PlanningOverview, previous: PlanRevision | None
) -> tuple[ElementReview, ...]:
    """Derive each element's researcher review and platform authorization state."""
    decisions: dict[tuple[str, int], ResearcherDecision] = {}
    for decision in sorted(overview.researcher_decisions, key=lambda item: item.decided_at):
        decisions[_key(decision.proposal)] = decision
    authorizations: dict[tuple[str, int], PlatformAuthorization] = {}
    for authorization in sorted(overview.authorizations, key=lambda item: item.observed_at):
        authorizations[_key(authorization.proposal)] = authorization
    inherited: dict[str, PlanningReference] = {}
    if previous is not None:
        predecessor = previous.overview.prediction
        prior_evidence = {item.evidence_id: item for item in predecessor.evidence}
        unchanged_evidence = {
            item.evidence_id
            for item in overview.prediction.evidence
            if prior_evidence.get(item.evidence_id) == item
        }
        current = {item.element_id: item for item in overview.prediction.elements}
        for element, review in zip(predecessor.elements, previous.elements, strict=True):
            if (
                review.approved_in is not None
                and current.get(element.element_id) == element
                and unchanged_evidence.issuperset(element.evidence_ids)
            ):
                inherited[element.element_id] = review.approved_in
    reviews: list[ElementReview] = []
    for element in overview.prediction.elements:
        covering = [
            item
            for item in overview.operation_sets
            if any(element.element_id in op.element_ids for op in item.proposal.operations)
        ]
        decided = sorted(
            (
                decisions[_key(item.proposal.proposal)]
                for item in covering
                if _key(item.proposal.proposal) in decisions
            ),
            key=lambda item: item.decided_at,
        )
        authorized = sorted(
            (
                authorizations[_key(item.proposal.proposal)]
                for item in covering
                if _key(item.proposal.proposal) in authorizations
            ),
            key=lambda item: item.observed_at,
        )
        latest = decided[-1].decision if decided else None
        approved_in = None
        state: Literal["approved", "denied", "unchanged-since-approval", "requires-review"]
        if latest == "approved":
            state = "approved"
            approved_in = overview.prediction.prediction
        elif latest == "denied":
            state = "denied"
        elif latest is None and element.element_id in inherited:
            state = "unchanged-since-approval"
            approved_in = inherited[element.element_id]
        else:
            state = "requires-review"
        reviews.append(
            ElementReview(
                element_id=element.element_id,
                kind=element.kind,
                runtime_profile_ref=element.runtime_profile_ref,
                review=state,
                approved_in=approved_in,
                platform=authorized[-1].status if authorized else "not-requested",
            )
        )
    return tuple(reviews)


def _budget_changed(entries: tuple[_Entry, ...], estimate: PlatformEstimate) -> bool:
    """Compare budgets observed before the estimate was recorded with the latest ones."""
    position = next(
        item.sequence
        for item in entries
        if isinstance(item.record, PlatformEstimate)
        and item.record.estimate_id == estimate.estimate_id
    )
    before = _latest_budgets(item for item in entries if item.sequence < position)
    if not before:
        return False
    latest = _latest_budgets(entries)
    return any(
        _amounts(observation) != _amounts(latest[budget_id])
        for budget_id, observation in before.items()
    ) or set(latest) != set(before)


def _amounts(observation: BudgetObservation) -> tuple[str | None, ...]:
    return (
        observation.denomination.unit,
        observation.allocated,
        observation.reserved,
        observation.spent,
        observation.available,
    )


def _revision_outcomes(revision: PlanRevision) -> list[PlanningOutcome]:
    """Report the conditions that need attention in a plan's latest revision."""
    outcomes: list[PlanningOutcome] = []
    estimates = {item.estimate_id: item for item in revision.overview.estimates}
    # A newer estimate for the same scope and elements replaces the older one here.
    latest = {
        (item.scope, frozenset(item.covered_element_ids)): item.estimate_id
        for item in revision.overview.estimates
    }
    for state in revision.estimates:
        if state.estimate_id not in latest.values():
            continue
        subject = f"estimate {state.estimate_id}"
        if state.state == "expired":
            outcomes.append(_outcome("planning-estimate-expired", subject))
        if state.budget_changed:
            outcomes.append(_outcome("planning-budget-changed", subject))
        if estimates[state.estimate_id].total.coverage != "complete":
            outcomes.append(_outcome("planning-estimate-incomplete", subject))
    authorizations: dict[tuple[str, int], PlatformAuthorization] = {}
    for authorization in sorted(
        revision.overview.authorizations, key=lambda item: item.observed_at
    ):
        authorizations[_key(authorization.proposal)] = authorization
    for authorization in authorizations.values():
        if authorization.status in {"denied", "revoked"}:
            outcomes.append(
                _outcome("planning-denied", f"authorization {authorization.authorization_id}")
            )
    for operation in revision.overview.operations:
        if operation.status == "denied":
            outcomes.append(_outcome("planning-denied", f"operation {operation.operation_id}"))
    return outcomes


def _comparison(revisions: Sequence[PlanRevision]) -> tuple[EstimateComparison, ...]:
    """List complete-plan estimates across revisions; amounts are never converted."""
    rows: list[EstimateComparison] = []
    denomination = None
    for revision in revisions:
        overview = revision.overview
        states = {item.estimate_id: item.state for item in revision.estimates}
        profiles = {
            item.element_id: item.runtime_profile_ref for item in overview.prediction.elements
        }
        for estimate in overview.estimates:
            if estimate.scope != "plan":
                continue
            denomination = denomination or estimate.total.denomination
            rows.append(
                EstimateComparison(
                    prediction=overview.prediction.prediction,
                    estimate_id=estimate.estimate_id,
                    runtime_profile_refs=tuple(
                        sorted({profiles[item] for item in estimate.covered_element_ids})
                    ),
                    total=estimate.total,
                    state=states[estimate.estimate_id],
                    comparable=estimate.total.denomination == denomination,
                )
            )
    return tuple(rows)


def _outcome(check_id: str, subject: str) -> PlanningOutcome:
    definition: DiagnosticDefinition = diagnostic_for(check_id)
    return PlanningOutcome(
        code=definition.code,
        title=definition.title,
        next_action=definition.next_action,
        subject=subject,
        diagnostic=f"{definition.code}: {definition.title}",
    )


_REVIEWED = (
    "\n\nThe researcher reviews the call before it runs, and the result arrives later as a "
    "separate message."
)


def _parsed(model: type[BaseModel], value: object) -> BaseModel:
    try:
        return model.model_validate_json(json.dumps(value))
    except (ValidationError, ValueError, TypeError):
        raise PlanningRecordError("The arguments do not match the planning tool") from None


def _agent_tools() -> tuple[ClientToolSpec, ...]:
    from openhands.sdk.tool.client_tool import ClientToolSpec

    # The record's own schema, with its definitions where the references resolve.
    prediction = WorkloadPrediction.model_json_schema()
    reference = {
        "type": "object",
        "description": "The recorded prediction revision.",
        "properties": {
            "record_id": {"type": "string"},
            "revision": {"type": "integer", "minimum": 1},
        },
        "required": ["record_id", "revision"],
        "additionalProperties": False,
    }
    return (
        ClientToolSpec(
            name=OVERVIEW_AGENT_TOOL,
            description=(
                "Report which planning capabilities this platform offers, budgets and "
                "estimates, each as available, not-offered, unavailable or refused, or "
                "unchecked before the first discovery. Also "
                "return the latest budget observations with their observation times, the "
                "recorded prediction revisions with their platform estimates and element "
                "reviews, observation references for evidence, and planning outcomes. Call "
                "it before relying on a budget or an estimate: a platform may offer neither."
                + _REVIEWED
            ),
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
        ),
        ClientToolSpec(
            name=PREDICTION_AGENT_TOOL,
            description=(
                "Record one immutable workload prediction revision for this launch context. "
                "The prediction is a heartwood.workload-prediction.v1 record whose "
                "project_context_id is the one planning_overview reports; a later revision "
                "names the revision it supersedes." + _REVIEWED
            ),
            parameters={
                "type": "object",
                "properties": {
                    "prediction": {
                        key: value for key, value in prediction.items() if key != "$defs"
                    }
                },
                "required": ["prediction"],
                "additionalProperties": False,
                "$defs": prediction["$defs"],
            },
        ),
        ClientToolSpec(
            name=ESTIMATE_AGENT_TOOL,
            description=(
                "Ask the platform to estimate a recorded prediction revision, for the whole "
                "plan or for named elements. Call it only when planning_overview reports "
                "estimates as available. The platform reserves, charges and runs nothing."
                + _REVIEWED
            ),
            parameters={
                "type": "object",
                "properties": {
                    "prediction": reference,
                    "scope": {"type": "string", "enum": ["plan", "elements"]},
                    "element_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Elements to estimate; only for elements scope.",
                    },
                },
                "required": ["prediction"],
                "additionalProperties": False,
            },
        ),
    )


def _agent_view(
    projection: PlanningProjection, launch: PlatformLaunchContext
) -> dict[str, JsonValue]:
    """Keep what an agent plans with; proposals, decisions and launch details stay out."""
    plans: list[JsonValue] = []
    for plan in projection.plans:
        revisions: list[JsonValue] = []
        for revision in plan.revisions:
            prediction = revision.overview.prediction
            states = {item.estimate_id: item for item in revision.estimates}
            revisions.append(
                {
                    "prediction": prediction.prediction.model_dump(mode="json"),
                    "plan": prediction.plan.model_dump(mode="json"),
                    "elements": [item.model_dump(mode="json") for item in revision.elements],
                    "estimates": [
                        {
                            **item.model_dump(mode="json"),
                            "state": states[item.estimate_id].state,
                            "budget_changed": states[item.estimate_id].budget_changed,
                        }
                        for item in revision.overview.estimates
                    ],
                    "observation_evidence": [
                        item.model_dump(mode="json") for item in revision.observation_evidence
                    ],
                }
            )
        plans.append({"plan_id": plan.plan_id, "revisions": revisions})
    return {
        "project_context_id": launch.project_context_id,
        "services": None
        if projection.services is None
        else projection.services.model_dump(mode="json"),
        "budgets": [item.model_dump(mode="json") for item in projection.budgets],
        "plans": plans,
        "outcomes": [item.model_dump(mode="json") for item in projection.outcomes],
    }


def _agent_tool_availability(reason: str | None) -> PlanningAgentTools:
    if reason is None:
        return PlanningAgentTools(availability="available")
    return PlanningAgentTools(
        availability="unavailable",
        unavailable_reason=reason,
        unavailable_code=diagnostic_for("mcp-worker-required").code,
    )


def not_offered_projection() -> PlanningProjection:
    """Return the projection of a deployment without a planning service."""
    return PlanningProjection(offered=False)


__all__ = [
    "AGENT_RESULT_BYTES",
    "BUDGET_TOOL",
    "ESTIMATE_AGENT_TOOL",
    "ESTIMATE_TOOL",
    "OVERVIEW_AGENT_TOOL",
    "PLANNING_SERVICE_ID",
    "PREDICTION_AGENT_TOOL",
    "HostedPlanningClient",
    "McpPlanningClient",
    "PlanningDeployment",
    "PlanningDiscovery",
    "PlanningJournal",
    "PlanningNotOfferedError",
    "PlanningRecordError",
    "PlanningServiceClient",
    "PlanningServiceError",
    "PlanningStateError",
    "PlatformPlanning",
    "PlatformPlanningBinding",
    "not_offered_projection",
]
