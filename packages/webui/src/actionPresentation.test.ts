/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

import { describe, expect, it } from "vitest";
import {
  actionHeading,
  actionRiskPresentation,
  actionStateLabel,
  actionToolLabel,
  specialistModelFields,
} from "./actionPresentation";
import { syntheticAction } from "./test/fixtures";
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
});
