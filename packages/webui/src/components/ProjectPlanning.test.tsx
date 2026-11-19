/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { HeartwoodClient } from "../client";
import type { PlanningProjection } from "../types";
import { admissionNotice, ProjectPlanning } from "./ProjectPlanning";

// The gateway tests check that this file is the projection the gateway produces.
// It is read at run time because the image build type-checks this package without it.
const projection = JSON.parse(
  readFileSync(
    resolve(
      import.meta.dirname,
      "../../../../fixtures/synthetic/platform-contract/planning-projection.json",
    ),
    "utf8",
  ),
) as PlanningProjection;

const client = (value: PlanningProjection = projection) => ({
  getPlanning: vi.fn<HeartwoodClient["getPlanning"]>().mockResolvedValue(value),
  refreshPlanning: vi
    .fn<HeartwoodClient["refreshPlanning"]>()
    .mockResolvedValue(value),
  requestPlanEstimate: vi
    .fn<HeartwoodClient["requestPlanEstimate"]>()
    .mockResolvedValue(value),
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("project planning", () => {
  it("stays hidden when the deployment offers no planning service", async () => {
    const api = client({ offered: false });
    const { container } = render(<ProjectPlanning client={api} />);

    await waitFor(() => expect(api.getPlanning).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it("shows budgets, revisions, estimates, the comparison and outcomes", async () => {
    render(<ProjectPlanning client={client()} />);
    fireEvent.click(await screen.findByText("Research Planning"));

    expect(
      screen.getByText(
        "HW-PLAN-004: A platform budget changed after the estimate",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("research-project-a")).toBeInTheDocument();
    expect(screen.getByText("binding-a")).toBeInTheDocument();
    expect(
      screen.getByText(/available 10 USD, allocated 50 USD, reserved 0 USD/),
    ).toBeInTheDocument();
    const comparison = screen.getByRole("region", {
      name: "Estimate comparison",
    });
    expect(comparison).toHaveTextContent(
      "Revision 1: 24.7 USD (from 18.54 USD to 37.05 USD), complete coverage",
    );
    expect(comparison).toHaveTextContent(
      "Revision 2: 22.5 USD (from 15.9 USD to 33.75 USD), complete coverage",
    );
    const revised = screen.getByRole("region", { name: "Revision 2" });
    expect(revised).toHaveTextContent(
      "batch (batch, synthetic-batch-gpu): requires review; platform authorization not requested",
    );
    expect(revised).toHaveTextContent("a budget changed since");
    expect(revised).toHaveTextContent(
      "Uncertainty: Batch duration depends on input sizes not yet measured at full scale.",
    );
    expect(revised).toHaveTextContent(
      "duration_per_task: expected 20 seconds (from 15 seconds to 30 seconds)",
    );
    expect(revised).toHaveTextContent("created 2026-07-01T00:00:00Z");
    expect(revised).toHaveTextContent(
      "Part: batch: 17.6 USD (from 13.2 USD to 26.4 USD), complete coverage",
    );
    expect(revised).toHaveTextContent(
      "Pricing assumption: Synthetic list prices; no discounts or commitments apply.",
    );
    expect(
      screen.getAllByText(
        "Constraint: Reported availability does not reserve funds or admit work.",
      ),
    ).toHaveLength(2);
    expect(screen.getByText(admissionNotice)).toBeInTheDocument();
  });

  it("reports a failed read instead of hiding the section", async () => {
    const api = client();
    api.getPlanning.mockRejectedValue(
      new Error("Persisted planning record is invalid"),
    );
    render(<ProjectPlanning client={api} />);
    fireEvent.click(await screen.findByText("Research Planning"));

    expect(screen.getByRole("alert")).toHaveTextContent(
      "Persisted planning record is invalid",
    );
    fireEvent.click(screen.getByRole("button", { name: /Refresh Budgets/ }));
    expect(await screen.findByText("research-project-a")).toBeInTheDocument();
    expect(
      screen.queryByText("Persisted planning record is invalid"),
    ).not.toBeInTheDocument();
  });

  it("refreshes budgets and requests an estimate for the latest revision", async () => {
    const api = client();
    render(<ProjectPlanning client={api} />);
    fireEvent.click(await screen.findByText("Research Planning"));

    fireEvent.click(screen.getByRole("button", { name: /Refresh Budgets/ }));
    await waitFor(() => expect(api.refreshPlanning).toHaveBeenCalledOnce());
    // Only the latest revision offers a new estimate; getByRole fails on several.
    fireEvent.click(
      await screen.findByRole("button", { name: "Request Plan Estimate" }),
    );

    await waitFor(() =>
      expect(api.requestPlanEstimate).toHaveBeenCalledWith({
        prediction: { record_id: "cohort-prediction", revision: 2 },
        scope: "plan",
      }),
    );
  });

  it("reports a failed request without dropping the shown projection", async () => {
    const api = client();
    api.refreshPlanning.mockRejectedValue(
      new Error("Platform planning is not offered by this deployment"),
    );
    render(<ProjectPlanning client={api} />);
    fireEvent.click(await screen.findByText("Research Planning"));
    fireEvent.click(screen.getByRole("button", { name: /Refresh Budgets/ }));

    expect(
      await screen.findByText(
        "Platform planning is not offered by this deployment",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("research-project-a")).toBeInTheDocument();
  });
});
