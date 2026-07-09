/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

export type JsonPrimitive = string | number | boolean | null;
export type JsonValue =
  JsonPrimitive | JsonValue[] | { [key: string]: JsonValue };

export type CommandKind =
  | "detect"
  | "approve"
  | "deny"
  | "chat"
  | "run"
  | "pause"
  | "resume"
  | "replay"
  | "audit.export";

export type EventKind =
  | "command.received"
  | "detection.proposed"
  | "approval.recorded"
  | "policy.decision.recorded"
  | "model_call.decision.recorded"
  | "agent_message.emitted"
  | "tool_call.proposed"
  | "confirmation.requested"
  | "confirmation.resolved"
  | "tool.execution.recorded"
  | "session.paused"
  | "session.resumed"
  | "audit.export.recorded"
  | "error.recorded";

export interface SessionCommand {
  schema_version: "heartwood.session-command.v1";
  command_id: string;
  session_id: string;
  kind: CommandKind;
  actor_id: string;
  created_at: string;
  payload: Record<string, JsonValue>;
}

export interface SessionEvent {
  schema_version: "heartwood.session-event.v1";
  event_id: string;
  session_id: string;
  sequence: number;
  kind: EventKind;
  occurred_at: string;
  payload: Record<string, JsonValue>;
  previous_event_hash: string | null;
}

export interface ActivityItem {
  sequence: number;
  kind: EventKind;
  label: string;
  detail: string;
}

export interface ChatMessage {
  role: "assistant" | "system";
  content: string;
}

export interface DatasetProposal {
  sourceId: string;
  datasetType: string;
  confidence: number;
  evidence: string[];
}

export interface SkillProposal {
  targetId: string;
  status: "proposed" | "approved" | "denied";
  detail: string;
}

export interface ApprovalControl {
  targetType: string;
  targetId: string;
  label: string;
  decision: string | null;
}

export interface PolicyStatus {
  decision: string;
  endpoint: string;
  reason: string;
  routeId: string | null;
  provider: string | null;
}

export interface ExportAction {
  label: string;
  path: string;
}

export interface SessionViewModel {
  sessionId: string;
  eventCount: number;
  activity: ActivityItem[];
  chat: ChatMessage[];
  datasetProposals: DatasetProposal[];
  skillProposals: SkillProposal[];
  approvalControls: ApprovalControl[];
  policyStatus: PolicyStatus[];
  exportActions: ExportAction[];
  paused: boolean;
}
