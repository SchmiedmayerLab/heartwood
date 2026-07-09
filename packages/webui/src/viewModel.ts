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
  DatasetProposal,
  EventKind,
  JsonValue,
  PolicyStatus,
  SessionEvent,
  SessionViewModel,
  SkillProposal,
} from "./types";

export const emptyViewModel = (sessionId = ""): SessionViewModel => ({
  sessionId,
  eventCount: 0,
  activity: [],
  agentOutputs: [],
  conversation: [],
  datasetProposals: [],
  skillProposals: [],
  approvalControls: [],
  policyStatus: [],
  modelInvocations: [],
  exportActions: [],
  paused: false,
});

export const buildViewModel = (events: SessionEvent[]): SessionViewModel => {
  const viewModel = emptyViewModel(events.at(-1)?.session_id ?? "");
  for (const event of events) {
    viewModel.activity.push(activityItem(event));
    switch (event.kind) {
      case "agent_message.emitted": {
        const agentMessage = {
          role: "agent",
          content: stringValue(event.payload.content),
        } as const;
        viewModel.agentOutputs.push(agentMessage);
        addConversationMessage(viewModel, event, "agent", {
          content: agentMessage.content,
          label: "Agent",
        });
        break;
      }
      case "detection.proposed": {
        const proposal = datasetProposal(event.payload.dataset);
        viewModel.datasetProposals.push(proposal);
        break;
      }
      case "tool_call.proposed":
        addConversationMessage(viewModel, event, "trace", {
          content: `Proposed tool: ${stringValue(event.payload.tool_name)}`,
          detail: stringValue(event.payload.summary) || null,
          label: "Trace",
        });
        viewModel.skillProposals.push({
          targetId: stringValue(event.payload.tool_call_id),
          status: "proposed",
          detail: stringValue(event.payload.summary),
        });
        addApprovalControl(viewModel.approvalControls, {
          targetType: "tool-call",
          targetId: stringValue(event.payload.tool_call_id),
          label: `Approve ${stringValue(event.payload.tool_name)}`,
          decision: null,
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
      case "approval.recorded":
        recordApproval(
          viewModel.approvalControls,
          viewModel.skillProposals,
          event.payload.approval,
        );
        break;
      case "model_call.decision.recorded": {
        const status = policyStatus(event.payload);
        const invocation = modelInvocation(event.payload);
        viewModel.policyStatus.push(status);
        viewModel.modelInvocations.push(invocation);
        addConversationMessage(viewModel, event, "trace", {
          content: `${status.decision || "reviewed"} model route${
            status.routeId ? ` via ${status.routeId}` : ""
          }`,
          detail:
            [
              status.reason,
              invocation.responsePreview === null ?
                "response preview not persisted by default"
              : null,
            ]
              .filter(Boolean)
              .join(" · ") || null,
          label: "Trace",
          offset: 0.1,
        });
        if (invocation.responsePreview) {
          addConversationMessage(viewModel, event, "model", {
            content: invocation.responsePreview,
            detail:
              [invocation.model, invocation.provider, invocation.routeId]
                .filter(Boolean)
                .join(" · ") || null,
            label: "Model",
            offset: 0.2,
          });
        }
        addApprovalControl(
          viewModel.approvalControls,
          modelCallApproval(event.payload.decision),
        );
        break;
      }
      case "error.recorded":
        addConversationMessage(viewModel, event, "trace", {
          content: "Error recorded",
          detail: stringValue(event.payload.reason) || null,
          label: "Trace",
        });
        break;
      case "audit.export.recorded":
        viewModel.exportActions.push({
          label: "Scrubbed audit JSONL",
          path: stringValue(event.payload.path),
        });
        break;
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
  role: SessionViewModel["conversation"][number]["role"],
  message: {
    content: string;
    detail?: string | null;
    label: string;
    offset?: number;
  },
): void => {
  if (!message.content) {
    return;
  }
  const offset = message.offset ?? 0;
  viewModel.conversation.push({
    id: `${event.event_id}-${role}-${String(offset)}`,
    sequence: event.sequence + offset,
    role,
    label: message.label,
    content: message.content,
    detail: message.detail ?? null,
  });
};

const datasetProposal = (value: JsonValue | undefined): DatasetProposal => {
  const dataset = recordValue(value);
  return {
    sourceId: stringValue(dataset.source_id),
    datasetType: stringValue(dataset.dataset_type),
    confidence: numberValue(dataset.confidence),
    evidence: stringArrayValue(dataset.evidence),
  };
};

const confirmationApproval = (
  value: JsonValue | undefined,
): ApprovalControl => {
  const request = recordValue(value);
  return {
    targetType: "tool-call",
    targetId: stringValue(request.tool_call_id),
    label: `Review ${stringValue(request.tool_name)}`,
    decision: null,
  };
};

const recordApproval = (
  approvals: ApprovalControl[],
  skills: SkillProposal[],
  value: JsonValue | undefined,
): void => {
  const approval = recordValue(value);
  const targetType = stringValue(approval.target_type);
  const targetId = stringValue(approval.target_id);
  const decision = stringValue(approval.decision);
  if (targetType === "skill") {
    skills.push({
      targetId,
      status: decision === "denied" ? "denied" : "approved",
      detail: stringValue(approval.reason),
    });
  }
  const existing = approvals.find(
    (control) =>
      control.targetType === targetType && control.targetId === targetId,
  );
  if (existing) {
    existing.decision = decision;
    existing.label = `${decision} ${targetType}`;
  } else {
    approvals.push({
      targetType,
      targetId,
      label: `${decision} ${targetType}`,
      decision,
    });
  }
};

const recordConfirmation = (
  approvals: ApprovalControl[],
  payload: Record<string, JsonValue>,
): void => {
  const targetId = stringValue(payload.tool_call_id);
  if (!targetId) {
    return;
  }
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
      decision,
    });
  }
};

const policyStatus = (payload: Record<string, JsonValue>): PolicyStatus => {
  const decision = recordValue(payload.decision);
  const route = optionalRecordValue(payload.provider_route);
  return {
    decision: stringValue(decision.decision),
    endpoint: stringValue(decision.endpoint),
    reason: stringValue(decision.reason),
    routeId: route === null ? null : stringValue(route.route_id),
    provider: route === null ? null : stringValue(route.provider),
  };
};

const modelInvocation = (
  payload: Record<string, JsonValue>,
): SessionViewModel["modelInvocations"][number] => {
  const metadata = optionalRecordValue(payload.response_metadata);
  const route = optionalRecordValue(payload.provider_route);
  const usage = optionalRecordValue(metadata?.usage);
  return {
    status: metadata === null ? "pending" : stringValue(metadata.status),
    model: metadata === null ? null : stringValue(metadata.model) || null,
    routeId: route === null ? null : stringValue(route.route_id),
    provider: route === null ? null : stringValue(route.provider),
    responsePreview:
      metadata === null ? null : stringValue(metadata.response_preview) || null,
    choicesCount:
      metadata === null ? null : numberOrNull(metadata.choices_count),
    totalTokens: usage === null ? null : numberOrNull(usage.total_tokens),
  };
};

const modelCallApproval = (value: JsonValue | undefined): ApprovalControl => {
  const decision = recordValue(value);
  return {
    targetType: "model-call",
    targetId: stringValue(decision.decision_id),
    label: `Review model call to ${stringValue(decision.endpoint)}`,
    decision: null,
  };
};

const addApprovalControl = (
  approvals: ApprovalControl[],
  next: ApprovalControl,
): void => {
  const existing = approvals.find(
    (control) =>
      control.targetType === next.targetType &&
      control.targetId === next.targetId,
  );
  if (existing === undefined) {
    approvals.push(next);
  }
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
    "detection.proposed": "Detection proposed",
    "error.recorded": "Error",
    "model_call.decision.recorded": "Model-call decision",
    "policy.decision.recorded": "Policy decision",
    "session.paused": "Session paused",
    "session.resumed": "Session resumed",
    "tool.execution.recorded": "Tool execution",
    "tool_call.proposed": "Tool proposed",
  })[kind];

const activityDetail = (event: SessionEvent): string => {
  if (event.kind === "model_call.decision.recorded") {
    const decision = recordValue(event.payload.decision);
    return `${stringValue(decision.decision)} ${stringValue(decision.endpoint)}`;
  }
  if (event.kind === "tool_call.proposed") {
    return stringValue(event.payload.tool_name);
  }
  if (event.kind === "tool.execution.recorded") {
    return `exit=${stringValue(event.payload.exit_code)}`;
  }
  if (event.kind === "audit.export.recorded") {
    return stringValue(event.payload.path);
  }
  if (event.kind === "error.recorded") {
    return stringValue(event.payload.reason);
  }
  return stringValue(event.payload.command_id);
};

export const recordValue = (
  value: JsonValue | undefined,
): Record<string, JsonValue> => {
  if (value !== null && typeof value === "object" && !Array.isArray(value)) {
    return value;
  }
  return {};
};

export const optionalRecordValue = (
  value: JsonValue | undefined,
): Record<string, JsonValue> | null => {
  if (value === undefined || value === null) {
    return null;
  }
  return recordValue(value);
};

export const stringValue = (value: JsonValue | undefined): string => {
  if (typeof value === "string") {
    return value;
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  return "";
};

export const numberValue = (value: JsonValue | undefined): number => {
  if (typeof value === "number") {
    return value;
  }
  return 0;
};

export const numberOrNull = (value: JsonValue | undefined): number | null => {
  if (typeof value === "number") {
    return value;
  }
  return null;
};

export const stringArrayValue = (value: JsonValue | undefined): string[] => {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.filter((item): item is string => typeof item === "string");
};
