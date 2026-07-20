/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

import type {
  ActivityItem,
  ApprovalControl,
  EventKind,
  JsonValue,
  SessionEvent,
  SessionViewModel,
} from "./types";

export const emptyViewModel = (sessionId = ""): SessionViewModel => ({
  sessionId,
  eventCount: 0,
  activity: [],
  conversation: [],
  approvalControls: [],
  context: {
    modelEndpoint: null,
    modelDecision: null,
    modelReason: null,
  },
  paused: false,
});

export const buildViewModel = (events: SessionEvent[]): SessionViewModel => {
  const viewModel = emptyViewModel(events.at(-1)?.session_id ?? "");
  for (const event of events) {
    viewModel.activity.push(activityItem(event));
    switch (event.kind) {
      case "user_message.recorded":
        addConversationMessage(viewModel, event, {
          content: stringValue(event.payload.content),
          id: `local-${stringValue(event.payload.command_id)}`,
          label: "You",
          role: "user",
        });
        break;
      case "agent_message.emitted":
        addConversationMessage(viewModel, event, {
          content: stringValue(event.payload.content),
          label: "Agent",
          role: "agent",
        });
        break;
      case "tool_call.proposed": {
        const action = actionPresentation(event.payload);
        addConversationMessage(viewModel, event, {
          content: `Proposed ${toolLabel(event.payload.tool_name)}`,
          detail: action.summary,
          label: "Trace",
          role: "trace",
          technicalDetail: action.arguments,
        });
        break;
      }
      case "tool.execution.recorded":
        addConversationMessage(viewModel, event, {
          content: `Ran ${toolLabel(event.payload.tool_name)}`,
          detail:
            stringValue(event.payload.summary) ||
            `Exit ${stringValue(event.payload.exit_code) || "unknown"}`,
          label: "Tool",
          role: "trace",
        });
        break;
      case "confirmation.requested":
        addApprovalControl(
          viewModel.approvalControls,
          confirmationApproval(event.payload.request),
        );
        break;
      case "confirmation.resolved":
        recordConfirmation(viewModel.approvalControls, event.payload);
        break;
      case "model_call.decision.recorded": {
        const decision = recordValue(event.payload.decision);
        viewModel.context.modelEndpoint =
          stringValue(decision.endpoint) || null;
        viewModel.context.modelDecision =
          stringValue(decision.decision) || null;
        viewModel.context.modelReason = stringValue(decision.reason) || null;
        break;
      }
      case "error.recorded": {
        const reason = stringValue(event.payload.reason);
        addConversationMessage(viewModel, event, {
          content: "The task could not be completed",
          detail:
            reason.startsWith("OpenHands conversation failed:") ?
              "Check Model setup and Activity & audit, then try again."
            : reason || "Review Activity & audit, then try again.",
          label: "System",
          role: "trace",
        });
        break;
      }
      case "session.paused":
        viewModel.paused = true;
        break;
      case "session.resumed":
        viewModel.paused = false;
        break;
      default:
        break;
    }
  }
  viewModel.eventCount = events.length;
  return viewModel;
};

const addConversationMessage = (
  viewModel: SessionViewModel,
  event: SessionEvent,
  message: {
    content: string;
    detail?: string | null;
    id?: string;
    label: string;
    role: SessionViewModel["conversation"][number]["role"];
    technicalDetail?: string | null;
  },
): void => {
  if (!message.content) return;
  viewModel.conversation.push({
    id: message.id ?? `${event.event_id}-${message.role}`,
    sequence: event.sequence,
    role: message.role,
    label: message.label,
    content: message.content,
    detail: message.detail ?? null,
    technicalDetail: message.technicalDetail ?? null,
  });
};

const confirmationApproval = (
  value: JsonValue | undefined,
): ApprovalControl => {
  const request = recordValue(value);
  const toolName = stringValue(request.tool_name);
  return {
    targetType: "tool-call",
    targetId: stringValue(request.tool_call_id),
    label: `Review ${toolName || "tool action"}`,
    toolName,
    risk: stringValue(request.risk) || null,
    summary: stringValue(request.summary) || null,
    arguments: recordValue(request.arguments),
    decision: null,
  };
};

const recordConfirmation = (
  approvals: ApprovalControl[],
  payload: Record<string, JsonValue>,
): void => {
  const targetId = stringValue(payload.tool_call_id);
  if (!targetId) return;
  const decision = stringValue(payload.decision) || "approved";
  const existing = approvals.find(
    (control) =>
      control.targetType === "tool-call" && control.targetId === targetId,
  );
  if (existing) {
    existing.decision = decision;
    existing.label = `${decision} tool-call`;
  } else {
    approvals.push({
      targetType: "tool-call",
      targetId,
      label: `${decision} tool-call`,
      toolName: "",
      risk: null,
      summary: null,
      arguments: {},
      decision,
    });
  }
};

const addApprovalControl = (
  approvals: ApprovalControl[],
  next: ApprovalControl,
): void => {
  const exists = approvals.some(
    (control) =>
      control.targetType === next.targetType &&
      control.targetId === next.targetId,
  );
  if (!exists) approvals.push(next);
};

const activityItem = (event: SessionEvent): ActivityItem => ({
  sequence: event.sequence,
  kind: event.kind,
  label: activityLabel(event.kind),
  detail: activityDetail(event),
});

const activityLabel = (kind: EventKind): string =>
  ({
    "agent_message.emitted": "Agent message",
    "approval.recorded": "Approval recorded",
    "audit.export.recorded": "Audit export",
    "command.received": "Command received",
    "confirmation.requested": "Confirmation requested",
    "confirmation.resolved": "Confirmation resolved",
    "error.recorded": "Error",
    "model_call.decision.recorded": "Model route decision",
    "policy.decision.recorded": "Policy decision",
    "session.paused": "Session paused",
    "session.resumed": "Session resumed",
    "tool.execution.recorded": "Tool execution",
    "tool_call.proposed": "Tool proposed",
    "user_message.recorded": "Researcher message",
  })[kind];

const activityDetail = (event: SessionEvent): string => {
  if (event.kind === "model_call.decision.recorded") {
    const decision = recordValue(event.payload.decision);
    return `${stringValue(decision.decision)} ${stringValue(decision.endpoint)}`;
  }
  if (event.kind === "tool_call.proposed")
    return stringValue(event.payload.tool_name);
  if (event.kind === "tool.execution.recorded")
    return `exit=${stringValue(event.payload.exit_code)}`;
  if (event.kind === "audit.export.recorded") {
    const eventCount = event.payload.event_count;
    return typeof eventCount === "number" ?
        `${eventCount} events, scrubbed JSONL`
      : "Scrubbed JSONL ready";
  }
  if (event.kind === "error.recorded") return stringValue(event.payload.reason);
  return stringValue(event.payload.command_id);
};

export const recordValue = (
  value: JsonValue | undefined,
): Record<string, JsonValue> =>
  value !== null && typeof value === "object" && !Array.isArray(value) ?
    value
  : {};

export const stringValue = (value: JsonValue | undefined): string => {
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean")
    return String(value);
  return "";
};

const actionPresentation = (
  payload: Record<string, JsonValue>,
): { arguments: string | null; summary: string | null } => {
  const summary = stringValue(payload.summary);
  const argumentsValue = recordValue(payload.arguments);
  const argumentsText =
    Object.keys(argumentsValue).length > 0 ?
      JSON.stringify(argumentsValue, null, 2)
    : "";
  return {
    arguments: argumentsText || null,
    summary: summary || null,
  };
};

const toolLabel = (value: JsonValue | undefined): string => {
  const toolName = stringValue(value);
  return (
    {
      file_editor: "file change",
      terminal: "terminal command",
    }[toolName] ?? (toolName ? `${toolName} action` : "tool action")
  );
};
