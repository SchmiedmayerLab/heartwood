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
  research_review: null,
  parallel_review_plan: null,
  corrections: [],
  run_id: "run",
  revision: 3,
  binding: {
    python_executable: null,
    workflow_id: "synthetic-analysis",
    workflow_fingerprint: "a".repeat(64),
    output_directory: "results",
    artifacts: [{ artifact_id: "report", path: "results/report.md" }],
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
      getExperimentRecords: vi.fn().mockResolvedValue({
        schema_version: "heartwood.experiment-collection.v1",
        retention: "project-local",
        runs: [],
      }),
      getExperimentExport: vi.fn(),
      getVerificationEnvironment: vi.fn(),
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
  it("submits bounded correction consent and inspects recorded attempt outputs", async () => {
    const state = run();
    const snapshot = {
      schema_version: "heartwood.review-snapshot.v1" as const,
      artifacts: [
        {
          artifact_id: "report",
          file: {
            path: "results/report.md",
            sha256: "a".repeat(64),
            size_bytes: 20,
          },
        },
      ],
    };
    state.corrections = [
      {
        correction_id: "correction-one",
        stage_id: "inspect",
        maximum_attempts: 2,
        stop_reason: "unavailable",
        review: {
          review_id: "review-one",
          parallel_plan: null,
          parallel_dispatch: [],
          reviewer_ids: ["statistical-reviewer"],
          started_sequence: 8,
          status: "assessed",
          submissions: [],
          unavailable_reason: null,
          snapshot,
          assessment: {
            schema_version: "heartwood.review-assessment.v1",
            snapshot_sha256: "b".repeat(64),
            findings: [],
          },
        },
        attempts: [
          {
            attempt_id: "attempt-one",
            started_sequence: 12,
            status: "assessed",
            unavailable_reason: null,
            plan: {
              review_id: "review-one",
              snapshot_sha256: "b".repeat(64),
              finding_ids: ["c".repeat(64)],
              output_directory: "correction-one",
              outputs: [
                { artifact_id: "report", path: "correction-one/report.md" },
              ],
            },
            assessment: {
              plan_sha256: "d".repeat(64),
              snapshot,
              checks: [
                {
                  finding_id: "c".repeat(64),
                  status: "still_observed",
                  reason: "artifact-byte-mismatch",
                },
              ],
            },
          },
          {
            attempt_id: "attempt-two",
            started_sequence: 20,
            status: "unavailable",
            unavailable_reason: "changed-context",
            plan: {
              review_id: "review-one",
              snapshot_sha256: "b".repeat(64),
              finding_ids: ["c".repeat(64)],
              output_directory: "correction-two",
              outputs: [
                { artifact_id: "report", path: "correction-two/report.md" },
              ],
            },
            assessment: null,
          },
        ],
      },
    ];
    const view = setup(
      syntheticProjection({ workflow: state, workflowControls: [] }),
    );
    fireEvent.click(await screen.findByText("Corrections: unavailable"));
    expect(screen.getByText("Attempt 1/2: assessed")).toBeVisible();
    expect(screen.getByText("still observed")).toBeVisible();
    expect(screen.getByText("changed context")).toBeVisible();
    view.client.getWorkspaceFile.mockResolvedValue({
      status: "available",
      content: "# Preserved Attempt",
      message: null,
    });
    fireEvent.click(
      screen.getByRole("button", { name: "correction-one/report.md" }),
    );
    await screen.findByRole("heading", { name: "Preserved Attempt" });
    expect(view.client.getWorkspaceFile).toHaveBeenCalledWith(
      "session-test",
      "correction-one/report.md",
    );
    expect(view.onSubmit).not.toHaveBeenCalled();

    const request = {
      action: "correct" as const,
      run_id: "run",
      revision: 4,
      maximum_attempts: 2,
    };
    view.rerender(
      <ResearchWorkspace
        {...view}
        projection={{
          ...view.projection,
          workflow: run(),
          workflowControls: [
            {
              control_id: "correct",
              label: "Correct Findings (Up to 2 Attempts)",
              request,
            },
          ],
        }}
      />,
    );
    fireEvent.click(
      await screen.findByRole("button", {
        name: "Correct Findings (Up to 2 Attempts)",
      }),
    );
    expect(view.onSubmit).toHaveBeenCalledExactlyOnceWith(request);
  });

  it("does not reload project provenance for unrelated session events", async () => {
    const view = setup();
    fireEvent.click(screen.getByText("Project Experiment Records"));
    await screen.findByText("No experiments recorded in this project.");
    view.rerender(
      <ResearchWorkspace
        {...view}
        projection={{
          ...view.projection,
          revision: view.projection.revision + 1,
        }}
      />,
    );
    expect(view.client.getExperimentRecords).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() =>
      expect(view.client.getExperimentRecords).toHaveBeenCalledTimes(2),
    );
  });

  it("shows recorded stage evidence without inferring successful execution", async () => {
    setup(
      syntheticProjection({
        workflow: run(),
        experiments: [
          {
            schema_version: "heartwood.experiment-run.v1",
            run_id: "3064c9dc-826f-4793-9203-e381dbf26303",
            definition: {
              actor_ref: "researcher",
              source: "heartwood",
              entry_point: null,
              code: [],
              inputs: [
                { path: "data.csv", sha256: "a".repeat(64), size_bytes: 42 },
              ],
              output_paths: ["results/report.md"],
              code_output_paths: [],
              environment: {
                kind: "python",
                source: "observed",
                sha256: "b".repeat(64),
              },
              parameters_sha256: "c".repeat(64),
              invocation_sha256: "d".repeat(64),
              git_revision: null,
              git_dirty: null,
              stage: {
                session_id: "session-test",
                workflow_run_id: "run",
                stage_id: "inspect",
                workflow_sha256: "a".repeat(64),
                tool_call_id: null,
                correction_id: null,
              },
            },
            started_at: "2026-09-11T00:00:00Z",
            updated_at: "2026-09-11T00:00:00Z",
            status: "started",
            attempt: 1,
            outputs: [],
            exit_code: null,
            evidence: [],
          },
        ],
      }),
    );
    fireEvent.click(await screen.findByText("Experiment Records"));
    const record = screen.getByRole("region", {
      name: "Experiment Inspect Data",
    });
    expect(record).toHaveTextContent("Outcome not recorded");
    expect(record).toHaveTextContent("3064c9dc-826f-4793-9203-e381dbf26303");
    expect(record).toHaveTextContent("b".repeat(64));
    expect(record).not.toHaveTextContent("succeeded");
  });

  it.each(["pending", "unavailable", "cancelled"] as const)(
    "renders %s review without requiring another assessment request",
    async (status) => {
      const state = run();
      state.research_review = {
        review_id: "review-one",
        parallel_plan: null,
        parallel_dispatch: [],
        reviewer_ids: ["statistical-reviewer"],
        started_sequence: 8,
        status,
        submissions: [],
        assessment: null,
        unavailable_reason:
          status === "unavailable" ? "no-structured-outcome" : null,
        snapshot: {
          schema_version: "heartwood.review-snapshot.v1",
          artifacts: [
            {
              artifact_id: "program",
              file: {
                path: "analysis.py",
                sha256: "a".repeat(64),
                size_bytes: 20,
              },
            },
          ],
        },
      };
      const view = setup(
        syntheticProjection({
          workflow: state,
          workflowControls: [],
        }),
      );
      const review = await screen.findByText(`Research Review: ${status}`);
      expect(review).toBeVisible();
      fireEvent.click(review);
      if (status === "unavailable") {
        expect(screen.getByText("no structured outcome")).toBeVisible();
      }
      expect(
        screen.queryByRole("button", { name: "Check Review Findings" }),
      ).not.toBeInTheDocument();
      expect(view.onSubmit).not.toHaveBeenCalled();
    },
  );

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

  it.each(["results/report.md", "correction-one/report.md"])(
    "submits the displayed review and inspects the bound artifact %s",
    async (path) => {
      const request = {
        action: "review" as const,
        run_id: "run",
        revision: 3,
        approved: true,
        evidence_fingerprint: "c".repeat(64),
      };
      const state = run();
      state.binding.artifacts = [{ artifact_id: "report", path }];
      const view = setup(
        syntheticProjection({
          workflow: state,
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
        path,
      );
      fireEvent.click(screen.getByRole("button", { name: "Accept Stage" }));
      expect(view.onSubmit).toHaveBeenCalledWith(request);
      fireEvent.click(
        screen.getByRole("button", { name: "Open Conversation" }),
      );
      expect(view.onConversation).toHaveBeenCalledOnce();
    },
  );

  it("does not infer stage controls from model messages or lifecycle", async () => {
    setup(syntheticProjection({ workflow: run(), workflowControls: [] }));
    await screen.findByRole("heading", { name: "Synthetic Analysis" });
    expect(
      screen.queryByRole("button", { name: "Accept Stage" }),
    ).not.toBeInTheDocument();
  });

  it("shows gateway review limits and submits the exact parallel consent", async () => {
    const request = {
      action: "request-review" as const,
      run_id: "run",
      revision: 3,
      parallel_review_fingerprint: "b".repeat(64),
    };
    const summary =
      "Parallel review (preview): 2 workers; up to 300s. Action confirmation still applies.";
    const view = setup(
      syntheticProjection({
        workflow: run(),
        reviewExecution: {
          status: "preview",
          purpose: "qualified-review",
          workers: 2,
          reviewers: ["research-planner", "statistical-reviewer"],
          budget: {
            maximum_seconds: 300,
            maximum_model_calls: 20,
            maximum_tokens: 100000,
            maximum_actions: 30,
            maximum_reported_cost_usd: 1,
          },
          summary,
        },
        workflowControls: [
          {
            control_id: "request-parallel-review",
            label: "Review with 2 Parallel Specialists",
            request,
          },
        ],
      }),
    );
    expect(await screen.findByText(summary)).toBeVisible();
    fireEvent.click(
      screen.getByRole("button", {
        name: "Review with 2 Parallel Specialists",
      }),
    );
    expect(view.onSubmit).toHaveBeenCalledWith(request);
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
