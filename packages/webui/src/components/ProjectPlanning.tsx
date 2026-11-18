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

const platformLabels: Record<Element["platform"], string> = {
  authorized: "authorized",
  denied: "denied",
  pending: "pending",
  unknown: "unknown",
  revoked: "revoked",
  "not-requested": "not requested",
};

const amount = (value: string | null | undefined, unit: string) =>
  value === null || value === undefined ? "unknown" : `${value} ${unit}`;

export const costLabel = (cost: EstimatedCost) => {
  const unit = cost.denomination.unit;
  if (cost.coverage === "unknown") return "unknown cost";
  const bounds =
    (cost.lower ?? null) === null && (cost.upper ?? null) === null ?
      ""
    : ` (from ${amount(cost.lower, unit)} to ${amount(cost.upper, unit)})`;
  return `${amount(cost.expected, unit)}${bounds}, ${cost.coverage} coverage`;
};

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
                {(budget.limitations ?? []).map((item) => (
                  <p key={item}>Limitation: {displaySafeText(item)}</p>
                ))}
              </li>
            );
          })}
        </ul>
      }
    </section>
    {(projection.plans ?? []).map((plan) => (
      <section key={plan.plan_id} aria-label={`Plan ${plan.plan_id}`}>
        <h4>Plan {displaySafeText(plan.plan_id)}</h4>
        {plan.revisions.map((revision, index) => {
          const prediction = revision.overview.prediction;
          const states = new Map(
            revision.estimates.map((item) => [item.estimate_id, item]),
          );
          const latest = index === plan.revisions.length - 1;
          return (
            <section
              key={`${prediction.prediction.record_id} ${prediction.prediction.revision}`}
              aria-label={`Revision ${prediction.plan.revision}`}
            >
              <h5>Revision {prediction.plan.revision}</h5>
              <ul>
                {revision.elements.map((element) => (
                  <li key={element.element_id}>
                    {displaySafeText(element.element_id)} ({element.kind},{" "}
                    {displaySafeText(element.runtime_profile_ref)}):{" "}
                    {reviewLabel(element)}; platform authorization{" "}
                    {platformLabels[element.platform]}
                  </li>
                ))}
              </ul>
              <ul>
                {(revision.overview.estimates ?? []).map((estimate) => {
                  const state = states.get(estimate.estimate_id);
                  return (
                    <li key={estimate.estimate_id}>
                      Estimate <code>{estimate.estimate_id}</code> for{" "}
                      {estimate.scope === "plan" ?
                        "whole plan"
                      : `elements ${estimate.covered_element_ids.join(", ")}`}
                      : {costLabel(estimate.total)}; valid until{" "}
                      {estimate.valid_until}; {state?.state ?? "unknown"}
                      {state?.budget_changed ? "; a budget changed since" : ""}
                      {(estimate.total.limitations ?? []).map((item) => (
                        <p key={item}>Limitation: {displaySafeText(item)}</p>
                      ))}
                    </li>
                  );
                })}
              </ul>
              {latest && projection.services?.estimates !== "not-offered" ?
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
        })}
        {plan.comparison.length > 0 ?
          <section aria-label="Estimate comparison">
            <h5>Comparison</h5>
            <ul>
              {plan.comparison.map((row) => (
                <li key={row.estimate_id}>
                  Revision {row.prediction.revision}: {costLabel(row.total)}{" "}
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
      .catch(() => {
        // Without a planning projection the section stays hidden.
      });
    return () => {
      active = false;
    };
  }, [client]);
  if (!projection?.offered) return null;
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
      <p>
        Platform estimates and budgets. A reported balance or estimate does not
        reserve funds or guarantee that the platform admits work.
      </p>
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
    </details>
  );
};
