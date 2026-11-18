# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Text lines for the planning projection, shared by the terminal and notebook."""

from __future__ import annotations

from heartwood.gateway._action_presentation import display_safe_text
from heartwood.schemas import EstimatedCost, WorkloadQuantity
from heartwood.schemas.planning import ElementReview, PlanningProjection

ADMISSION_NOTICE = (
    "A reported balance or estimate does not reserve funds or guarantee "
    "that the platform admits work."
)


def planning_lines(projection: PlanningProjection) -> tuple[str, ...]:
    """Describe plans, estimates, budgets and outcomes; unknown values stay unknown."""
    if not projection.offered:
        return ("Platform planning is not offered by this deployment.",)
    lines: list[str] = [ADMISSION_NOTICE]
    if projection.launch is not None:
        launch = projection.launch
        lines.extend(
            (
                f"Platform project: {launch.project_id}",
                f"Billing attribution: {launch.billing_attribution_ref or 'not reported'}",
                f"Launch context: {launch.project_context_id}",
            )
        )
    services = projection.services
    if services is not None:
        checked = "" if services.checked_at is None else f", checked {services.checked_at}"
        lines.append(
            f"Budget service: {services.budgets}; estimate service: {services.estimates}{checked}"
        )
    if not projection.budgets:
        lines.append("Budgets: none observed")
    for budget in projection.budgets:
        unit = budget.denomination.unit
        lines.append(
            f"Budget {budget.budget_id}: available {_amount(budget.available, unit)}, "
            f"allocated {_amount(budget.allocated, unit)}, "
            f"reserved {_amount(budget.reserved, unit)}, spent {_amount(budget.spent, unit)}; "
            f"observed {budget.observed_at}"
        )
        lines.extend(f"  Constraint: {item}" for item in budget.constraints)
        lines.extend(f"  Limitation: {item}" for item in budget.limitations)
    for plan in projection.plans:
        lines.append(f"Plan {plan.plan_id}")
        for revision in plan.revisions:
            prediction = revision.overview.prediction
            lines.append(
                f"  Revision {prediction.plan.revision}: prediction "
                f"{prediction.prediction.record_id} revision {prediction.prediction.revision}"
            )
            lines.append(f"    Uncertainty: {prediction.uncertainty}")
            lines.extend(f"    Assumption: {item}" for item in prediction.assumptions)
            for element, review in zip(prediction.elements, revision.elements, strict=True):
                lines.append(f"    {_review(review)}")
                lines.extend(
                    f"      {item.metric}: {_quantity(item)}; {item.uncertainty}"
                    for item in element.quantities
                )
                lines.extend(f"      Assumption: {item}" for item in element.assumptions)
            overview = revision.overview
            lines.extend(
                f"    Researcher decision {item.decision_id} on proposal "
                f"{item.proposal.record_id} revision {item.proposal.revision}: {item.decision}, "
                f"{item.decided_at}; audit {item.audit_ref}"
                for item in overview.researcher_decisions
            )
            lines.extend(
                f"    Platform authorization {item.authorization_id} on proposal "
                f"{item.proposal.record_id} revision {item.proposal.revision}: {item.status}; "
                f"budget {item.budget_id or 'not reported'}, "
                f"reservation {item.reservation_ref or 'none reported'}; "
                f"observed {item.observed_at}"
                for item in overview.authorizations
            )
            lines.extend(
                f"    Operation {item.operation_id}: {item.status}; observed {item.observed_at}"
                for item in overview.operations
            )
            states = {item.estimate_id: item for item in revision.estimates}
            for estimate in revision.overview.estimates:
                state = states[estimate.estimate_id]
                scope = (
                    "whole plan"
                    if estimate.scope == "plan"
                    else "elements " + ", ".join(estimate.covered_element_ids)
                )
                changed = "; a budget changed since" if state.budget_changed else ""
                lines.append(
                    f"    Estimate {estimate.estimate_id} for {scope}: {_cost(estimate.total)}; "
                    f"created {estimate.created_at}, valid until {estimate.valid_until}; "
                    f"{state.state}{changed}"
                )
                lines.extend(
                    f"      {'Part' if item.included_in_total else 'In addition'}: "
                    f"{', '.join(item.element_ids)}: {_cost(item.cost)}"
                    for item in estimate.element_estimates
                )
                lines.extend(f"      Limitation: {item}" for item in estimate.total.limitations)
                lines.extend(f"      Excludes: {item}" for item in estimate.exclusions)
                lines.extend(
                    f"      Pricing assumption: {item}" for item in estimate.pricing_assumptions
                )
        for row in plan.comparison:
            denomination = "" if row.comparable else "; another denomination, not comparable"
            lines.append(
                f"  Compare revision {row.prediction.revision}: {_cost(row.total)} with "
                f"{', '.join(row.runtime_profile_refs)}; {row.state}{denomination}"
            )
    lines.extend(
        f"{item.diagnostic} ({item.subject}). {item.next_action}" for item in projection.outcomes
    )
    # A repeated line adds nothing; the browser shows each distinct text once as well.
    return tuple(display_safe_text(line) for line in dict.fromkeys(lines))


def _amount(value: str | None, unit: str) -> str:
    return "unknown" if value is None else f"{value} {unit}"


def _quantity(item: WorkloadQuantity) -> str:
    bounds = (
        ""
        if item.lower is None and item.upper is None
        else f" (from {_amount(item.lower, item.unit)} to {_amount(item.upper, item.unit)})"
    )
    return f"expected {_amount(item.expected, item.unit)}{bounds}"


def _cost(cost: EstimatedCost) -> str:
    unit = cost.denomination.unit
    if cost.coverage == "unknown":
        return "unknown cost"
    bounds = (
        ""
        if cost.lower is None and cost.upper is None
        else f" (from {_amount(cost.lower, unit)} to {_amount(cost.upper, unit)})"
    )
    return f"{_amount(cost.expected, unit)}{bounds}, {cost.coverage} coverage"


def _review(element: ElementReview) -> str:
    review = {
        "approved": "approved by the researcher",
        "denied": "denied by the researcher",
        "requires-review": "requires review",
        "unchanged-since-approval": (
            "unchanged since approval in prediction revision "
            f"{element.approved_in.revision if element.approved_in else 'unknown'}"
        ),
    }[element.review]
    return (
        f"{element.element_id} ({element.kind}, {element.runtime_profile_ref}): {review}; "
        f"platform authorization {element.platform.replace('-', ' ')}"
    )


__all__ = ["ADMISSION_NOTICE", "planning_lines"]
