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
  SessionProjection,
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
  settings?.modes.find((option) => option.mode === settings.review_mode) ??
  null;

export const summarySourceLabel = (action: ProjectionActionRecord): string =>
  action.summarySource === "gateway" ?
    "Gateway-provided summary"
  : "Model-provided summary";

/** Whether the exact retry of a retained approval can still send it, as the gateway decides. */
export const retainedApprovalSendable = (
  projection: SessionProjection,
): boolean =>
  projection.lifecycle.status !== "error" || projection.lifecycle.canSteer;

/** Model-written fields of a specialist task review, each named as model-provided. */
export const specialistModelFields = (
  action: ProjectionActionRecord,
): Array<{ label: string; value: string }> => {
  const details = action.details;
  if (details.kind !== "task") return [];
  return [
    ...(details.roleLabel === null && details.subagentType !== null ?
      [{ label: "Model-provided specialist name", value: details.subagentType }]
    : []),
    ...(details.description !== null ?
      [{ label: "Model-provided task description", value: details.description }]
    : []),
    ...(details.prompt !== null ?
      [{ label: "Model-provided task prompt", value: details.prompt }]
    : []),
  ];
};

/** The command or file operation exactly as the model proposed it, labelled as such. */
export const modelProposedOperation = (
  action: ProjectionActionRecord,
): { label: string; value: string } | null => {
  const details = action.details;
  if (details.kind === "terminal") {
    return { label: "Model-proposed command", value: `$ ${details.command}` };
  }
  if (details.kind === "file-editor") {
    return {
      label: "Model-proposed file operation",
      value: `${details.operation} ${details.path ?? "path unavailable"}`,
    };
  }
  return null;
};

/** Name who wrote an action's exact arguments: the model, or the gateway for an outside review. */
export const actionArgumentsLabel = (action: ProjectionActionRecord): string =>
  (
    action.details.kind === "outside-operation" ||
    action.details.kind === "outside-result"
  ) ?
    "Complete reviewed proposal prepared by the gateway"
  : "Exact arguments proposed by the model";

/** Head an action with its labelled command or path, or its trusted name; model text is labelled. */
export const actionHeading = (action: ProjectionActionRecord): string => {
  const details = action.details;
  const operation = modelProposedOperation(action);
  if (operation !== null) return `${operation.label}: ${operation.value}`;
  if (details.kind === "task") {
    if (details.roleLabel !== null) return details.roleLabel;
    if (details.description !== null) {
      return `Model-provided task description: ${details.description}`;
    }
    if (details.subagentType !== null) {
      return `Model-provided specialist name: ${details.subagentType}`;
    }
  }
  if (details.kind === "outside-operation") {
    return `Outside operation to ${details.destination}: ${action.summary}`;
  }
  if (details.kind === "outside-result") {
    return `Outside result release: ${action.summary}`;
  }
  return action.summary ?
      `${summarySourceLabel(action)}: ${action.summary}`
    : "";
};
