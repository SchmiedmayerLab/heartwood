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

import json
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast

from pydantic import BaseModel, ValidationError

from heartwood.gateway._diagnostics import DiagnosticDefinition, diagnostic_for
from heartwood.persistence import DurableFileError, LockedJsonlStore
from heartwood.schemas import (
    BudgetObservation,
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
    PlanningEstimateRequest,
    PlanningOutcome,
    PlanningProjection,
    PlanningServices,
    PlanningServiceStatus,
    PlanProjection,
    PlanRevision,
)

if TYPE_CHECKING:
    from mcp.types import CallToolResult
    from openhands.sdk.mcp import MCPClient

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
    """The local planning journal is damaged and needs integrity recovery."""


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

    def _session[T](self, request: Callable[[MCPClient], Awaitable[T]]) -> T:
        # The MCP client loads with the OpenHands SDK, so it is imported when first used.
        from fastmcp.client.transports import StreamableHttpTransport
        from mcp.shared.exceptions import MCPError as McpProtocolError
        from openhands.sdk.mcp import MCPClient
        from openhands.sdk.mcp.exceptions import MCPError

        client = MCPClient(  # type: ignore[no-untyped-call]
            StreamableHttpTransport(str(self.endpoint.profile.url), auth=self.endpoint.token),
            timeout=_TIMEOUT_SECONDS,
        )
        try:
            client.call_async_from_sync(client.connect, timeout=_TIMEOUT_SECONDS)
            return cast(T, client.call_async_from_sync(request(client), timeout=_TIMEOUT_SECONDS))
        except (MCPError, McpProtocolError, OSError, TimeoutError):
            # Transport and authentication failures carry server text; keep only the outcome.
            raise PlanningServiceError("unavailable") from None
        finally:
            client.sync_close()


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
                "schema_version": "heartwood.planning-entry.v1",
                "kind": kind,
                "record": record.model_dump(mode="json"),
            }

        try:
            self._store.append_derived(build)
        except _RepeatError:
            return
        except DurableFileError:
            raise PlanningStateError("Planning records require integrity recovery") from None


def _entries(objects: tuple[dict[str, Any], ...]) -> tuple[_Entry, ...]:
    entries: list[_Entry] = []
    for sequence, item in enumerate(objects, 1):
        model = _KINDS.get(str(item.get("kind")))
        if item.get("schema_version") != "heartwood.planning-entry.v1" or model is None:
            raise PlanningStateError("Persisted planning record is invalid")
        try:
            record = model.model_validate_json(json.dumps(item.get("record")))
        except ValidationError:
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

    def __init__(self, binding: PlatformPlanningBinding, *, journal_path: Path) -> None:
        """Keep planning records in ``journal_path``; the binding's clock judges validity."""
        self.launch = binding.launch
        self.client = binding.client
        self.journal = PlanningJournal(journal_path)
        self.clock = binding.clock
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
                return True
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
        projected: list[PlanProjection] = []
        for plan_id in sorted(plans):
            revisions = sorted(
                plans[plan_id],
                key=lambda item: (item.plan.revision, _key(item.prediction)),
            )
            views: list[PlanRevision] = []
            for prediction in revisions:
                views.append(_revision(entries, prediction, views, now))
            outcomes.extend(_revision_outcomes(views[-1]))
            projected.append(
                PlanProjection(
                    plan_id=plan_id, revisions=tuple(views), comparison=_comparison(views)
                )
            )
        return PlanningProjection(
            offered=True,
            launch=self.launch,
            services=services,
            budgets=tuple(budgets[key] for key in sorted(budgets)),
            plans=tuple(projected),
            outcomes=tuple(outcomes),
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
            if any(_identity(item.record) == _identity(record) for item in entries):
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


def _revision(
    entries: tuple[_Entry, ...],
    prediction: WorkloadPrediction,
    earlier: Sequence[PlanRevision],
    now: datetime,
) -> PlanRevision:
    """Project one revision; an unchanged element keeps the approval of its last revision."""
    overview = _overview(entries, prediction)
    reviews = _element_reviews(overview)
    previous = earlier[-1] if earlier else None
    elements: list[ElementReview] = []
    for element in prediction.elements:
        review = reviews[element.element_id]
        if review.review == "requires-review" and previous is not None:
            before = next(
                (
                    (item, state)
                    for item, state in zip(
                        previous.overview.prediction.elements, previous.elements, strict=True
                    )
                    if item.element_id == element.element_id
                ),
                None,
            )
            if (
                before is not None
                and before[0] == element
                and before[1].review in {"approved", "unchanged-since-approval"}
            ):
                review = review.model_copy(
                    update={
                        "review": "unchanged-since-approval",
                        "approved_in": before[1].approved_in,
                    }
                )
        elements.append(review)
    return PlanRevision(
        overview=overview,
        elements=tuple(elements),
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
        # Each observation of an operation is kept; the latest one is shown.
        return ("operation", record.operation_id, record.observed_at.isoformat(), record.status)
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
                if any(op.operation_id == record.operation_id for op in item.proposal.operations)
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
        if observation.operation_id in operations:
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


def _element_reviews(overview: PlanningOverview) -> dict[str, ElementReview]:
    """Derive each element's researcher review and platform authorization state."""
    decisions: dict[tuple[str, int], ResearcherDecision] = {}
    for decision in sorted(overview.researcher_decisions, key=lambda item: item.decided_at):
        decisions[_key(decision.proposal)] = decision
    authorizations: dict[tuple[str, int], PlatformAuthorization] = {}
    for authorization in sorted(overview.authorizations, key=lambda item: item.observed_at):
        authorizations[_key(authorization.proposal)] = authorization
    reviews: dict[str, ElementReview] = {}
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
        reviews[element.element_id] = ElementReview(
            element_id=element.element_id,
            kind=element.kind,
            runtime_profile_ref=element.runtime_profile_ref,
            review=(
                "approved"
                if latest == "approved"
                else "denied"
                if latest == "denied"
                else "requires-review"
            ),
            approved_in=overview.prediction.prediction if latest == "approved" else None,
            platform=authorized[-1].status if authorized else "not-requested",
        )
    return reviews


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


def not_offered_projection() -> PlanningProjection:
    """Return the projection of a deployment without a planning service."""
    return PlanningProjection(offered=False)


__all__ = [
    "BUDGET_TOOL",
    "ESTIMATE_TOOL",
    "McpPlanningClient",
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
