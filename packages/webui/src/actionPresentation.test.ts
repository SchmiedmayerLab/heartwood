/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

import { describe, expect, it } from "vitest";
import {
  actionRiskPresentation,
  actionStateLabel,
  actionToolLabel,
} from "./actionPresentation";
import type { ActionPresentation } from "./types";

const presentation: ActionPresentation = {
  other_tool_label_template: "{tool_name} Action",
  risk_labels: { low: "Low Risk" },
  state_labels: { "awaiting-review": "Awaiting Review" },
  tool_labels: { terminal: "Terminal Command" },
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
});
