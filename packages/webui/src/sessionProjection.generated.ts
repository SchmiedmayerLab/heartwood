/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

/* eslint-disable */
/**
 * Generated from the gateway-owned Pydantic session projection.
 * Run `npm run contracts:generate` after changing a shared request or response.
 */

export type JsonValue =
  | string
  | number
  | boolean
  | null
  | JsonValue[]
  | {
      [k: string]: JsonValue;
    };
export type ProjectionActionDetails =
  | ProjectionTerminalActionDetails
  | ProjectionFileEditorActionDetails
  | ProjectionTaskActionDetails
  | ProjectionOtherActionDetails;
/**
 * Events emitted by a Heartwood session.
 *
 * The stream translates OpenHands conversation events so every surface renders
 * the same turns. ``MODEL_CALL_DECISION_RECORDED`` records route authorization
 * before task submission or a continuation that may call the model;
 * ``POLICY_DECISION_RECORDED`` is reserved for other policy decisions.
 */
export type EventKind =
  | "command.received"
  | "approval.recorded"
  | "policy.decision.recorded"
  | "model_call.decision.recorded"
  | "user_message.recorded"
  | "agent_message.emitted"
  | "tool_call.proposed"
  | "confirmation.requested"
  | "confirmation.resolved"
  | "tool.execution.recorded"
  | "agent.lifecycle.updated"
  | "task.plan.updated"
  | "model.usage.updated"
  | "subagent.updated"
  | "session.paused"
  | "session.resumed"
  | "audit.export.recorded"
  | "error.recorded";
/**
 * Commands accepted by a Heartwood session.
 */
export type CommandKind =
  "approve" | "deny" | "chat" | "pause" | "resume" | "replay" | "audit.export";

/**
 * Complete session projection owned by the gateway.
 */
export interface SessionProjection {
  actions: ProjectionActionRecord[];
  activity: ProjectionActivity[];
  availableCommands: ("chat" | "pause" | "resume" | "approve" | "deny")[];
  context: ProjectionModelContext;
  conversation: ProjectionMessage[];
  eventCount: number;
  lastCommandOutcome: ProjectionCommandOutcome | null;
  lifecycle: ProjectionLifecycleState;
  /**
   * Return whether the projected session is paused.
   */
  paused: boolean;
  pendingApproval: ProjectionApprovalGroup | null;
  researcherNotice: ProjectionResearcherNotice | null;
  researcherStatus: ProjectionResearcherStatus;
  revision: number;
  schema_version: "heartwood.session-projection.v1";
  sessionId: string;
  streamEpoch: string;
  streamRevision: number;
  streamingText: string;
  subagents: ProjectionSubagent[];
  suggestions: ProjectionSuggestion[];
  taskPlan: ProjectionTask[];
  usage: ProjectionUsage | null;
  usageByPurpose: ProjectionUsage[];
  workspaceRevision: number;
}
/**
 * One versioned action record correlated across the OpenHands lifecycle.
 */
export interface ProjectionActionRecord {
  actionId: string | null;
  affectedPaths: ProjectionAffectedPath[];
  arguments: {
    [k: string]: JsonValue;
  };
  decision: ("approved" | "rejected") | null;
  details: ProjectionActionDetails;
  groupId: string | null;
  outcome: ProjectionActionOutcome | null;
  proposedSequence: number;
  risk: "high" | "low" | "medium" | "unknown";
  schema_version: "heartwood.action-record.v1";
  state:
    | "proposed"
    | "awaiting-review"
    | "approved"
    | "rejected"
    | "running"
    | "succeeded"
    | "failed"
    | "outcome-unknown";
  summary: string;
  toolCallId: string;
  toolName: string;
  updatedSequence: number;
}
/**
 * Project-relative path attributed to a typed mutating action.
 */
export interface ProjectionAffectedPath {
  effect: "created" | "modified" | "deleted" | "unknown";
  path: string;
  provenance: "file-editor-action";
}
/**
 * Typed terminal arguments from one OpenHands action.
 */
export interface ProjectionTerminalActionDetails {
  command: string;
  isInput: boolean;
  kind: "terminal";
  reset: boolean;
  timeout: number | null;
}
/**
 * Typed file-editor arguments from one OpenHands action.
 */
export interface ProjectionFileEditorActionDetails {
  kind: "file-editor";
  operation:
    "view" | "create" | "str_replace" | "insert" | "undo_edit" | "unknown";
  path: string | null;
}
/**
 * Typed sequential-specialist arguments from one OpenHands action.
 */
export interface ProjectionTaskActionDetails {
  capability: ("advisory" | "project-actions") | null;
  description: string | null;
  kind: "task";
  prompt: string | null;
  resume: string | null;
  roleLabel: string | null;
  subagentType: string | null;
}
/**
 * Typed fallback for an OpenHands tool without a specialized renderer.
 */
export interface ProjectionOtherActionDetails {
  kind: "other";
}
/**
 * Bounded private result of an executed action.
 */
export interface ProjectionActionOutcome {
  exitCode: number;
  result: string | null;
  resultTruncated: boolean;
  summary: string;
}
export interface ProjectionActivity {
  detail: string;
  kind: EventKind;
  label: string;
  sequence: number;
}
export interface ProjectionModelContext {
  modelDecision: string | null;
  modelEndpoint: string | null;
  modelReason: string | null;
}
export interface ProjectionMessage {
  content: string;
  detail: string | null;
  id: string;
  label: string;
  role: "user" | "agent" | "trace";
  sequence: number;
  technicalDetail: string | null;
}
/**
 * Gateway-owned outcome of the most recently accepted command.
 */
export interface ProjectionCommandOutcome {
  commandId: string;
  commandKind: CommandKind;
  errorCode: string | null;
  message: string | null;
  status: "accepted" | "rejected";
}
export interface ProjectionLifecycleState {
  canPause: boolean;
  canResume: boolean;
  canSteer: boolean;
  /**
   * User-visible state of the OpenHands conversation.
   */
  status:
    | "idle"
    | "running"
    | "paused"
    | "waiting-for-confirmation"
    | "finished"
    | "error";
}
/**
 * One decision that applies to every listed OpenHands action.
 */
export interface ProjectionApprovalGroup {
  actions: ProjectionActionRecord[];
  decision: ("approved" | "denied") | null;
  decisionScope: "all";
  groupId: string;
}
/**
 * A non-lifecycle outcome that every interface must present.
 */
export interface ProjectionResearcherNotice {
  code: "request-not-applied";
  detail: string;
  label: string;
  noticeId: string;
  tone: "attention" | "danger";
}
/**
 * Stable researcher-facing state derived from the session lifecycle.
 */
export interface ProjectionResearcherStatus {
  code:
    | "ready"
    | "working"
    | "waiting-for-review"
    | "paused"
    | "complete"
    | "denied"
    | "recoverable-failure"
    | "terminal-failure";
  detail: string;
  label: string;
  recoverable: boolean;
  tone: "neutral" | "progress" | "attention" | "success" | "danger";
}
export interface ProjectionSubagent {
  agentName: string;
  invocationId: string;
  parentActionId: string;
  parentSessionId: string;
  resultSummary: string | null;
  roleLabel: string;
  status: "proposed" | "running" | "completed" | "error" | "rejected";
  statusLabel: string;
  taskId: string | null;
  taskSummary: string | null;
}
/**
 * One bounded task suggestion derived from the authoritative session state.
 */
export interface ProjectionSuggestion {
  kind: "task" | "follow-up" | "recovery";
  label: string;
  prompt: string;
  suggestionId:
    | "inspect-project"
    | "plan-project"
    | "continue-plan"
    | "review-changes"
    | "verify-work"
    | "recover-task"
    | "identify-next-step";
}
export interface ProjectionTask {
  status: "todo" | "in-progress" | "done";
  statusLabel: string;
  title: string;
}
export interface ProjectionUsage {
  accumulatedCost: number;
  cacheReadTokens: number;
  cacheWriteTokens: number;
  callCount: number;
  completionTokens: number;
  contextWindow: number | null;
  modelName: string;
  promptTokens: number;
  purposeLabel: string;
  reasoningTokens: number;
  usageId: string;
}
