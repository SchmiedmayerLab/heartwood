/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

import { Button } from "@schmiedmayerlab/grove-design-system/components/Button";
import { LoaderCircle, RefreshCw } from "lucide-react";
import { useEffect, useState } from "react";
import type { HeartwoodClient } from "../client";
import type { EstimatedCost, PlanningProjection } from "../types";
import { displaySafeText } from "./SafeMarkdown";

type PlanningClient = Pick<
  HeartwoodClient,
  "getPlanning" | "refreshPlanning" | "requestPlanEstimate"
>;
type Revision = NonNullable<
  PlanningProjection["plans"]
>[number]["revisions"][number];
type Element = Revision["elements"][number];
type Quantity =
  Revision["overview"]["prediction"]["elements"][number]["quantities"][number];

// The terminal and notebook show the same notice from the gateway's shared lines.
export const admissionNotice =
  "A reported balance or estimate does not reserve funds or guarantee that the platform admits work.";

const amount = (value: string | null | undefined, unit: string) =>
  value === null || value === undefined ? "unknown" : `${value} ${unit}`;

const range = (
  lower: string | null | undefined,
  upper: string | null | undefined,
  unit: string,
) =>
  (lower ?? null) === null && (upper ?? null) === null ?
    ""
  : ` (from ${amount(lower, unit)} to ${amount(upper, unit)})`;

export const costLabel = (cost: EstimatedCost) => {
  const unit = cost.denomination.unit;
  if (cost.coverage === "unknown") return "unknown cost";
  return `${amount(cost.expected, unit)}${range(cost.lower, cost.upper, unit)}, ${cost.coverage} coverage`;
};

const quantityLabel = (item: Quantity) =>
  `${displaySafeText(item.metric)}: expected ${amount(item.expected, item.unit)}${range(item.lower, item.upper, item.unit)}; ${displaySafeText(item.uncertainty)}`;

const reviewLabel = (element: Element) => {
  switch (element.review) {
    case "approved":
      return "approved by the researcher";
    case "denied":
      return "denied by the researcher";
    case "unchanged-since-approval":
      return `unchanged since approval in prediction revision ${element.approved_in?.revision ?? "unknown"}`;
    default:
      return "requires review";
  }
};

const TextList = ({
  label,
  items,
}: {
  label: string;
  items: string[] | undefined;
}) =>
  // A repeated line adds nothing, so each distinct text is shown once.
  [...new Set(items ?? [])].map((item) => (
    <p key={item}>
      {label}: {displaySafeText(item)}
    </p>
  ));

const RevisionView = ({
  revision,
  latest,
  canEstimate,
  busy,
  onEstimate,
}: {
  revision: Revision;
  latest: boolean;
  canEstimate: boolean;
  busy: boolean;
  onEstimate: (prediction: Revision["overview"]["prediction"]) => void;
}) => {
  const overview = revision.overview;
  const prediction = overview.prediction;
  const states = new Map(
    revision.estimates.map((item) => [item.estimate_id, item]),
  );
  return (
    <section aria-label={`Revision ${prediction.plan.revision}`}>
      <h5>Revision {prediction.plan.revision}</h5>
      <p>
        Prediction {displaySafeText(prediction.prediction.record_id)} revision{" "}
        {prediction.prediction.revision}
      </p>
      {(prediction.evidence ?? []).map((item) => (
        <p key={item.evidence_id}>
          Evidence {displaySafeText(item.evidence_id)} ({item.kind}):{" "}
          {displaySafeText(item.reference)}; {displaySafeText(item.description)}
        </p>
      ))}
      <p>Uncertainty: {displaySafeText(prediction.uncertainty)}</p>
      <TextList label="Assumption" items={prediction.assumptions} />
      <ul>
        {prediction.elements.map((element, index) => {
          const review = revision.elements[index];
          return (
            <li key={element.element_id}>
              {displaySafeText(element.element_id)} ({element.kind},{" "}
              {displaySafeText(element.runtime_profile_ref)})
              {review ?
                `: ${reviewLabel(review)}; platform authorization ${review.platform.replace("-", " ")}`
              : null}
              {element.quantities.map((item) => (
                <p key={item.metric}>{quantityLabel(item)}</p>
              ))}
              <TextList label="Evidence" items={element.evidence_ids} />
              <TextList label="Assumption" items={element.assumptions} />
            </li>
          );
        })}
      </ul>
      <ul>
        {(overview.researcher_decisions ?? []).map((item) => (
          <li key={item.decision_id}>
            Researcher decision {displaySafeText(item.decision_id)} on proposal{" "}
            {displaySafeText(item.proposal.record_id)} revision{" "}
            {item.proposal.revision}: {item.decision}, {item.decided_at}; audit{" "}
            {displaySafeText(item.audit_ref)}
          </li>
        ))}
        {(overview.authorizations ?? []).map((item) => (
          <li key={item.authorization_id}>
            Platform authorization {displaySafeText(item.authorization_id)} on
            proposal {displaySafeText(item.proposal.record_id)} revision{" "}
            {item.proposal.revision}: {item.status}; budget{" "}
            {displaySafeText(item.budget_id ?? "not reported")}, reservation{" "}
            {displaySafeText(item.reservation_ref ?? "none reported")}; observed{" "}
            {item.observed_at}
          </li>
        ))}
        {(revision.observation_evidence ?? []).map((item) => (
          <li key={item.reference}>
            Operation {displaySafeText(item.operation_id)}:{" "}
            {displaySafeText(item.status)}; observed {item.observed_at};{" "}
            {displaySafeText(item.service_id)} run{" "}
            {displaySafeText(item.backend_operation_ref)};{" "}
            {displaySafeText(item.platform_record_ref)};{" "}
            {displaySafeText(item.reference)}
          </li>
        ))}
      </ul>
      <ul>
        {(overview.estimates ?? []).map((estimate) => {
          const state = states.get(estimate.estimate_id);
          return (
            <li key={estimate.estimate_id}>
              Estimate <code>{displaySafeText(estimate.estimate_id)}</code> for{" "}
              {estimate.scope === "plan" ?
                "whole plan"
              : `elements ${estimate.covered_element_ids.map(displaySafeText).join(", ")}`
              }
              : {costLabel(estimate.total)}; created {estimate.created_at},
              valid until {estimate.valid_until}; {state?.state ?? "unknown"}
              {state?.budget_changed ? "; a budget changed since" : ""}
              {(estimate.element_estimates ?? []).map((item) => (
                <p key={item.element_ids.join(" ")}>
                  {item.included_in_total ? "Part" : "In addition"}:{" "}
                  {item.element_ids.map(displaySafeText).join(", ")}:{" "}
                  {costLabel(item.cost)}
                </p>
              ))}
              <TextList label="Limitation" items={estimate.total.limitations} />
              <TextList label="Excludes" items={estimate.exclusions} />
              <TextList
                label="Pricing assumption"
                items={estimate.pricing_assumptions}
              />
            </li>
          );
        })}
      </ul>
      {latest && canEstimate ?
        <Button
          variant="outline"
          disabled={busy}
          onClick={() => onEstimate(prediction)}
        >
          Request Plan Estimate
        </Button>
      : null}
    </section>
  );
};

export const PlanningOverview = ({
  projection,
  busy,
  onEstimate,
}: {
  projection: PlanningProjection;
  busy: boolean;
  onEstimate: (prediction: Revision["overview"]["prediction"]) => void;
}) => (
  <>
    <p>{admissionNotice}</p>
    {(projection.outcomes ?? []).map((outcome) => (
      <div key={`${outcome.code} ${outcome.subject}`}>
        <p role="alert">{outcome.diagnostic}</p>
        <p>
          {displaySafeText(outcome.subject)}. {outcome.next_action}
        </p>
      </div>
    ))}
    {projection.launch ?
      <dl>
        <dt>Platform Project</dt>
        <dd>
          <code>{displaySafeText(projection.launch.project_id)}</code>
        </dd>
        <dt>Billing Attribution</dt>
        <dd>
          {projection.launch.billing_attribution_ref ?
            <code>
              {displaySafeText(projection.launch.billing_attribution_ref)}
            </code>
          : "not reported"}
        </dd>
        <dt>Launch Context</dt>
        <dd>
          <code>{displaySafeText(projection.launch.project_context_id)}</code>
        </dd>
        {projection.services ?
          <>
            <dt>Services</dt>
            <dd>
              Budgets {projection.services.budgets}, estimates{" "}
              {projection.services.estimates}
              {projection.services.checked_at ?
                `, checked ${projection.services.checked_at}`
              : ""}
            </dd>
          </>
        : null}
      </dl>
    : null}
    <section aria-label="Platform budgets">
      <h4>Budgets</h4>
      {(projection.budgets ?? []).length === 0 ?
        <p>None observed.</p>
      : <ul>
          {(projection.budgets ?? []).map((budget) => {
            const unit = budget.denomination.unit;
            return (
              <li key={budget.budget_id}>
                <code>{displaySafeText(budget.budget_id)}</code>: available{" "}
                {amount(budget.available, unit)}, allocated{" "}
                {amount(budget.allocated, unit)}, reserved{" "}
                {amount(budget.reserved, unit)}, spent{" "}
                {amount(budget.spent, unit)}; observed {budget.observed_at}
                <TextList label="Constraint" items={budget.constraints} />
                <TextList label="Limitation" items={budget.limitations} />
              </li>
            );
          })}
        </ul>
      }
    </section>
    {(projection.plans ?? []).map((plan) => (
      <section key={plan.plan_id} aria-label={`Plan ${plan.plan_id}`}>
        <h4>Plan {displaySafeText(plan.plan_id)}</h4>
        {plan.revisions.map((revision, index) => (
          <RevisionView
            key={`${revision.overview.prediction.prediction.record_id} ${revision.overview.prediction.prediction.revision}`}
            revision={revision}
            latest={index === plan.revisions.length - 1}
            canEstimate={projection.services?.estimates !== "not-offered"}
            busy={busy}
            onEstimate={onEstimate}
          />
        ))}
        {plan.comparison.length > 0 ?
          <section aria-label="Estimate comparison">
            <h5>Comparison</h5>
            <ul>
              {plan.comparison.map((row) => (
                <li key={row.estimate_id}>
                  Prediction {displaySafeText(row.prediction.record_id)},
                  revision {row.prediction.revision}, estimate{" "}
                  {displaySafeText(row.estimate_id)}: {costLabel(row.total)}{" "}
                  with{" "}
                  {row.runtime_profile_refs.map(displaySafeText).join(", ")};{" "}
                  {row.state}
                  {row.comparable ?
                    ""
                  : "; another denomination, not comparable"}
                </li>
              ))}
            </ul>
          </section>
        : null}
      </section>
    ))}
  </>
);

export const ProjectPlanning = ({ client }: { client: PlanningClient }) => {
  const [projection, setProjection] = useState<PlanningProjection | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let active = true;
    void client
      .getPlanning()
      .then((value) => {
        if (active) setProjection(value);
      })
      .catch((caught: unknown) => {
        if (active)
          setError(
            caught instanceof Error ? caught.message : "Planning unavailable",
          );
      });
    return () => {
      active = false;
    };
  }, [client]);
  // Only a deployment without a planning service hides the section.
  if (projection === null && error === null) return null;
  if (projection !== null && !projection.offered) return null;
  const run = (request: () => Promise<PlanningProjection>) => {
    setBusy(true);
    setError(null);
    void request()
      .then(setProjection)
      .catch((caught: unknown) => {
        setError(
          caught instanceof Error ? caught.message : "Planning unavailable",
        );
      })
      .finally(() => setBusy(false));
  };
  return (
    <details className="research-provenance project-planning">
      <summary>Research Planning</summary>
      <div className="research-controls">
        <Button
          variant="outline"
          disabled={busy}
          onClick={() => run(() => client.refreshPlanning())}
        >
          {busy ?
            <LoaderCircle className="animate-spin" size={16} />
          : <RefreshCw size={16} />}
          Refresh Budgets
        </Button>
      </div>
      {error !== null ?
        <p role="alert">{error}</p>
      : null}
      {projection !== null ?
        <PlanningOverview
          projection={projection}
          busy={busy}
          onEstimate={(prediction) =>
            run(() =>
              client.requestPlanEstimate({
                prediction: prediction.prediction,
                scope: "plan",
              }),
            )
          }
        />
      : null}
    </details>
  );
};
