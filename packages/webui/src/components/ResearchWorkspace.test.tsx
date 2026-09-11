/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { syntheticProjection } from "../test/fixtures";
import type { SessionProjection, WorkflowCatalog } from "../types";
import { ResearchWorkspace } from "./ResearchWorkspace";

const catalog: WorkflowCatalog = {
  workflows: [
    {
      available: true,
      unavailable_checks: [],
      definition: {
        schema_version: "heartwood.workflow-definition.v1",
        workflow_id: "synthetic-analysis",
        version: 1,
        label: "Synthetic Analysis",
        description: "Review a synthetic dataset.",
        inputs: [
          {
            input_id: "data",
            label: "Dataset",
            description: "Project-relative CSV path",
            kind: "file",
          },
          {
            input_id: "question",
            label: "Research Question",
            description: "The question to investigate",
            kind: "text",
          },
        ],
        artifacts: [
          {
            artifact_id: "report",
            label: "Report",
            relative_path: "report.md",
            media_type: "text/markdown",
          },
        ],
        stages: [
          {
            stage_id: "inspect",
            label: "Inspect Data",
            instruction: "Inspect the synthetic data",
            reads: ["data", "question"],
            writes: ["report"],
            checks: [
              {
                check_id: "report",
                evaluator_id: "artifact.nonempty",
                description: "Report exists",
                artifact_ids: ["report"],
              },
            ],
            reviewer_gate: "researcher",
            skill_ids: [],
            specialist_ids: [],
            budget: {
              maximum_seconds: 300,
              maximum_model_calls: 20,
              maximum_tokens: 100000,
              maximum_reported_cost_usd: 1,
              maximum_actions: 30,
            },
          },
        ],
        budget: {
          maximum_seconds: 600,
          maximum_model_calls: 40,
          maximum_tokens: 100000,
          maximum_reported_cost_usd: 1,
          maximum_actions: 50,
        },
      },
    },
  ],
};

const run = (): NonNullable<SessionProjection["workflow"]> => ({
  run_id: "run",
  revision: 3,
  binding: {
    workflow_id: "synthetic-analysis",
    workflow_fingerprint: "a".repeat(64),
    output_directory: "results",
    inputs: [
      {
        input_id: "data",
        value: "data.csv",
        kind: "file",
        sha256: "b".repeat(64),
      },
    ],
  },
  stage_id: "inspect",
  phase: "review",
  completed: [],
  evaluation: {
    artifacts: [],
    checks: [
      {
        check_id: "report",
        evaluator_id: "artifact.nonempty",
        status: "passed",
        inspected: [],
      },
    ],
    assessment: {
      workflow_fingerprint: "a".repeat(64),
      stage_id: "inspect",
      evidence_fingerprint: "c".repeat(64),
      evidence_satisfied: true,
      researcher_review_required: true,
      reasons: [],
    },
  },
  created_at: "2026-09-11T00:00:00Z",
  stage_started_at: null,
  stage_usage_baseline: null,
  started_sequence: 0,
});

const setup = (
  projection = syntheticProjection({ conversation: [], pendingApproval: null }),
) => {
  const props = {
    client: {
      getResearchWorkflows: vi.fn().mockResolvedValue(catalog),
      getWorkspaceFile: vi.fn(),
    },
    sessionId: "session-test",
    projection,
    busy: false,
    modelReady: true,
    onSubmit: vi.fn(),
    onConversation: vi.fn(),
    onNewSession: vi.fn(),
  };
  const rendered = render(<ResearchWorkspace {...props} />);
  return { ...props, ...rendered };
};

describe("research workflow workspace", () => {
  it("collects catalog-declared inputs without starting an agent implicitly", async () => {
    const view = setup();
    expect(screen.getByRole("status")).toHaveTextContent(
      "Loading research workflows",
    );
    fireEvent.change(await screen.findByLabelText("Dataset"), {
      target: { value: "data.csv" },
    });
    fireEvent.change(screen.getByLabelText("Research Question"), {
      target: { value: "Which measurements matter?" },
    });
    expect(view.onSubmit).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Start Workflow" }));
    expect(view.onSubmit).toHaveBeenCalledWith({
      action: "start",
      workflow_id: "synthetic-analysis",
      inputs: { data: "data.csv", question: "Which measurements matter?" },
      output_directory: "results",
    });
  });

  it("submits the exact displayed review request and inspects actual artifacts", async () => {
    const request = {
      action: "review" as const,
      run_id: "run",
      revision: 3,
      approved: true,
      evidence_fingerprint: "c".repeat(64),
    };
    const view = setup(
      syntheticProjection({
        workflow: run(),
        pendingApproval: null,
        workflowControls: [
          { control_id: "accept", label: "Accept Stage", request },
        ],
      }),
    );
    view.client.getWorkspaceFile.mockResolvedValue({
      status: "available",
      content: "# Synthetic Report\nMeasured findings",
      message: null,
    });
    fireEvent.click(await screen.findByRole("button", { name: "Report" }));
    expect(
      await screen.findByRole("heading", { name: "Synthetic Report" }),
    ).toBeVisible();
    expect(view.client.getWorkspaceFile).toHaveBeenCalledWith(
      "session-test",
      "results/report.md",
    );
    fireEvent.click(screen.getByRole("button", { name: "Accept Stage" }));
    expect(view.onSubmit).toHaveBeenCalledWith(request);
    fireEvent.click(screen.getByRole("button", { name: "Open Conversation" }));
    expect(view.onConversation).toHaveBeenCalledOnce();
  });

  it("does not infer stage controls from model messages or lifecycle", async () => {
    setup(syntheticProjection({ workflow: run(), workflowControls: [] }));
    await screen.findByRole("heading", { name: "Synthetic Analysis" });
    expect(
      screen.queryByRole("button", { name: "Accept Stage" }),
    ).not.toBeInTheDocument();
  });

  it("reports catalog failure and permits an explicit retry", async () => {
    const view = setup();
    await screen.findByLabelText("Dataset");
    const client = {
      ...view.client,
      getResearchWorkflows: vi
        .fn()
        .mockRejectedValueOnce(new Error("Offline catalog"))
        .mockResolvedValue(catalog),
    };
    view.rerender(<ResearchWorkspace {...view} client={client} />);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Offline catalog",
    );
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() =>
      expect(screen.queryByRole("alert")).not.toBeInTheDocument(),
    );
    expect(client.getResearchWorkflows).toHaveBeenCalledTimes(2);
  });

  it("requires a configured model and disables work while a request is pending", async () => {
    const view = setup();
    await screen.findByLabelText("Dataset");
    view.rerender(<ResearchWorkspace {...view} modelReady={false} />);
    expect(
      screen.getByRole("button", { name: "Start Workflow" }),
    ).toBeDisabled();
    view.rerender(<ResearchWorkspace {...view} busy />);
    expect(
      screen.getByRole("button", { name: "Start Workflow" }),
    ).toBeDisabled();
  });

  it("requires a new session instead of starting inside existing conversation evidence", async () => {
    const view = setup(syntheticProjection());
    fireEvent.click(await screen.findByRole("button", { name: "New Session" }));
    expect(view.onNewSession).toHaveBeenCalledOnce();
    expect(view.onSubmit).not.toHaveBeenCalled();
  });

  it("shows an empty catalog without offering invented workflows", async () => {
    const view = setup();
    await screen.findByLabelText("Dataset");
    view.rerender(
      <ResearchWorkspace
        {...view}
        client={{
          ...view.client,
          getResearchWorkflows: vi.fn().mockResolvedValue({ workflows: [] }),
        }}
      />,
    );
    expect(
      await screen.findByText("No research workflows are available."),
    ).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "Start Workflow" }),
    ).not.toBeInTheDocument();
  });

  it("does not display old artifact content while a newer workspace revision loads", async () => {
    const projection = syntheticProjection({
      workflow: run(),
      pendingApproval: null,
    });
    const view = setup(projection);
    view.client.getWorkspaceFile.mockResolvedValue({
      status: "available",
      content: "# Earlier Report",
      message: null,
    });
    fireEvent.click(await screen.findByRole("button", { name: "Report" }));
    await screen.findByRole("heading", { name: "Earlier Report" });
    const pending = Promise.withResolvers<{
      status: string;
      content: string;
      message: null;
    }>();
    view.client.getWorkspaceFile.mockReturnValue(pending.promise);
    view.rerender(
      <ResearchWorkspace
        {...view}
        projection={{
          ...projection,
          workspaceRevision: projection.workspaceRevision + 1,
        }}
      />,
    );
    expect(
      screen.queryByRole("heading", { name: "Earlier Report" }),
    ).not.toBeInTheDocument();
    expect(screen.getByText("Loading artifact...")).toBeVisible();
    pending.resolve({
      status: "available",
      content: "# Updated Report",
      message: null,
    });
    await screen.findByRole("heading", { name: "Updated Report" });
  });
});
