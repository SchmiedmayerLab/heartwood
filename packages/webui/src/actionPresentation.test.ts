/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

import { describe, expect, it } from "vitest";
import {
  actionArgumentsLabel,
  actionHeading,
  actionRiskPresentation,
  actionStateLabel,
  actionToolLabel,
  modelProposedOperation,
  platformOperationLines,
  platformStatusLines,
  specialistModelFields,
} from "./actionPresentation";
import {
  syntheticAction,
  syntheticOutsideOperation,
  syntheticPlatformJob,
} from "./test/fixtures";
import type { ActionPresentation } from "./types";

const presentation: ActionPresentation = {
  other_tool_label_template: "{tool_name} Action",
  risk_labels: { low: "Low Risk" },
  state_labels: { "awaiting-review": "Awaiting Review" },
  tool_labels: {
    task: "Specialist Review",
    terminal: "Terminal Command",
  },
  unknown_risk_label: "Not Classified",
  unknown_tool_label: "Tool Action",
};

describe("action presentation", () => {
  it("uses gateway-owned terminology with deterministic fallbacks", () => {
    expect(actionStateLabel("awaiting-review", presentation)).toBe(
      "Awaiting Review",
    );
    expect(actionStateLabel("outcome-unknown", null)).toBe("Outcome Unknown");
    expect(actionToolLabel("terminal", presentation)).toBe("Terminal Command");
    expect(actionToolLabel("task", presentation)).toBe("Specialist Review");
    expect(actionToolLabel("custom", presentation)).toBe("custom Action");
    expect(actionToolLabel("", null)).toBe("Tool Action");
    expect(actionRiskPresentation("low", presentation)).toEqual({
      className: "risk-low",
      label: "Low Risk",
    });
    expect(actionRiskPresentation(null, presentation)).toEqual({
      className: "risk-unknown",
      label: "Not Classified",
    });
  });

  it("labels every model-written specialist field and heading fallback", () => {
    const task = (
      roleLabel: string | null,
      description: string | null,
      subagentType: string | null,
    ) =>
      syntheticAction({
        toolName: "task",
        summary: "Delegate the review",
        details: {
          kind: "task",
          capability: "advisory",
          description,
          prompt: "Review the synthetic plan.",
          roleLabel,
          subagentType,
          resume: null,
        },
      });

    expect(actionHeading(task("Research Planner", "Check", "planner"))).toBe(
      "Research Planner",
    );
    expect(actionHeading(task(null, "Check the plan", "planner"))).toBe(
      "Model-provided task description: Check the plan",
    );
    expect(actionHeading(task(null, null, "planner"))).toBe(
      "Model-provided specialist name: planner",
    );
    expect(actionHeading(task(null, null, null))).toBe(
      "Model-provided summary: Delegate the review",
    );
    expect(actionHeading(syntheticAction({ details: { kind: "other" } }))).toBe(
      "Model-provided summary: build the aggregate synthetic target-condition cohort",
    );
    expect(specialistModelFields(task(null, "Check", "planner"))).toEqual([
      { label: "Model-provided specialist name", value: "planner" },
      { label: "Model-provided task description", value: "Check" },
      {
        label: "Model-provided task prompt",
        value: "Review the synthetic plan.",
      },
    ]);
    expect(
      specialistModelFields(task("Research Planner", null, "planner")),
    ).toEqual([
      {
        label: "Model-provided task prompt",
        value: "Review the synthetic plan.",
      },
    ]);
  });

  it("labels model-proposed commands, file operations and arguments", () => {
    const command = syntheticAction();
    const edit = syntheticAction({
      toolName: "file_editor",
      details: {
        kind: "file-editor",
        operation: "str_replace",
        path: "analysis/plan.md",
      },
    });

    expect(actionHeading(command)).toBe(
      "Model-proposed command: $ python run.py --output /project/cohort-summary.json",
    );
    expect(modelProposedOperation(edit)).toEqual({
      label: "Model-proposed file operation",
      value: "str_replace analysis/plan.md",
    });
    expect(actionHeading(edit)).toBe(
      "Model-proposed file operation: str_replace analysis/plan.md",
    );
    expect(actionArgumentsLabel(command)).toBe(
      "Exact arguments proposed by the model",
    );
    // An outside review's content is prepared by the gateway, not by the model.
    expect(modelProposedOperation(syntheticOutsideOperation())).toBeNull();
    expect(actionArgumentsLabel(syntheticOutsideOperation())).toBe(
      "Complete reviewed proposal prepared by the gateway",
    );
  });

  it("describes a platform job with the gateway's wording", () => {
    expect(platformOperationLines(syntheticOutsideOperation())).toEqual([]);
    expect(platformOperationLines(syntheticPlatformJob())).toEqual([
      "Platform proposal: Run task synthetic-analysis: python analysis.py",
      "Runtime profile: synthetic-batch-small",
      "Resource limits: cpu 2 core, duration 1800 s, gpu 1 synthetic-gpu",
      "Data scope: Project objects drs://data.synthetic.invalid/obj-cohort",
      "Results destination: Project results prefix results/synthetic/",
      "Estimate est-synthetic for element batch of plan cohort-plan revision 1: 19.8 USD, complete coverage, current when proposed, valid until 2026-07-01T01:00:00+00:00",
      "Platform project context: launch-synthetic-1",
      "Approval is not admission. A reported balance or estimate does not reserve funds or guarantee that the platform admits work.",
    ]);
    const denied = syntheticPlatformJob({
      status: "denied",
      backendOperationRef: "op-1",
      platformRecordRef: "synthetic-audit:op-1",
      observedLimitations: ["Budget budget-compute has no availability"],
      diagnostic: "HW-PLAN-006: The platform denied an operation",
    });
    expect(platformStatusLines(denied)).toEqual([
      "Platform status: Denied; platform reference op-1; platform record synthetic-audit:op-1",
      "Platform report: Budget budget-compute has no availability",
      "Diagnostic: HW-PLAN-006: The platform denied an operation",
    ]);
    const cancel = syntheticPlatformJob({
      kind: "cancel",
      proposal: null,
      estimate: null,
      targetOperationId: "op-1",
      targetSummary: "Call create_task on Synthetic jobs",
    });
    expect(platformOperationLines(cancel).slice(0, 2)).toEqual([
      "Cancels operation op-1: Call create_task on Synthetic jobs",
      "Cancellation does not undo completed work or incurred charges.",
    ]);
    expect(
      platformOperationLines(
        syntheticPlatformJob({
          proposal: null,
          estimate: {
            state: "missing",
            elementId: null,
            planId: null,
            planRevision: null,
            estimateId: null,
            coverage: null,
            expected: null,
            lower: null,
            upper: null,
            unit: null,
            validUntil: null,
            limitation: "No estimate covers this plan element.",
          },
        }),
      ),
    ).toEqual(
      expect.arrayContaining([
        "Platform proposal: none returned; this review cannot be approved",
        "Estimate: none recorded",
        "Estimate note: No estimate covers this plan element.",
      ]),
    );
  });
});
