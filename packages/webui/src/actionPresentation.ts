/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

import type {
  ActionModeOption,
  ActionPresentation,
  ActionRisk,
  ActionSettings,
  ProjectionActionRecord,
} from "./types";

export const actionCountLabel = (count: number): string =>
  `${count} ${count === 1 ? "action" : "actions"}`;

export const actionRiskPresentation = (
  risk: ActionRisk | null,
  presentation: ActionPresentation | null,
): { className: string; label: string } => {
  const normalizedRisk = risk ?? "unknown";
  return {
    className: (
      {
        high: "risk-high",
        low: "risk-low",
        medium: "risk-medium",
        unknown: "risk-unknown",
      } as const
    )[normalizedRisk],
    label:
      presentation?.risk_labels[normalizedRisk] ??
      presentation?.unknown_risk_label ??
      "Not Classified",
  };
};

export const actionToolLabel = (
  toolName: string,
  presentation: ActionPresentation | null,
): string => {
  const configured = presentation?.tool_labels[toolName];
  if (configured) return configured;
  if (!toolName) return presentation?.unknown_tool_label ?? "Tool Action";
  return (
    presentation?.other_tool_label_template.replace("{tool_name}", toolName) ??
    `${toolName} Action`
  );
};

export const actionStateLabel = (
  state: ProjectionActionRecord["state"],
  presentation: ActionPresentation | null,
): string =>
  presentation?.state_labels[state] ??
  state
    .split("-")
    .map((part) => `${part[0]?.toUpperCase() ?? ""}${part.slice(1)}`)
    .join(" ");

export const selectedActionMode = (
  settings: ActionSettings | null,
): ActionModeOption | null =>
  settings?.modes.find(
    (option) => option.mode === settings.confirmation_mode,
  ) ?? null;
