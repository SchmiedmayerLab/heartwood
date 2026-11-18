/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import example from "../../../../fixtures/synthetic/platform-contract/planning-projection.json";
import type { HeartwoodClient } from "../client";
import type { PlanningProjection } from "../types";
import { ProjectPlanning } from "./ProjectPlanning";

// The gateway tests check that this file is the projection the gateway produces.
const projection = example as PlanningProjection;

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
