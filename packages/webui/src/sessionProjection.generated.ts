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
export type JsonValue =
  | string
  | number
  | boolean
  | null
  | JsonValue[]
  | {
      [k: string]: JsonValue;
    };

/**
 * Complete session projection owned by the gateway.
 */
export interface SessionProjection {
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
  revision: number;
  schema_version: "heartwood.session-projection.v1";
  sessionId: string;
  streamEpoch: string;
  streamRevision: number;
  streamingText: string;
  subagents: ProjectionSubagent[];
  taskPlan: ProjectionTask[];
  usage: ProjectionUsage | null;
  usageByPurpose: ProjectionUsage[];
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
  actions: ProjectionApprovalAction[];
  decision: ("approved" | "denied") | null;
  decisionScope: "all";
  groupId: string;
}
/**
 * One member of an atomic OpenHands action group.
 */
export interface ProjectionApprovalAction {
  arguments: {
    [k: string]: JsonValue;
  };
  risk: ("high" | "low" | "medium" | "unknown") | null;
  summary: string | null;
  targetId: string;
  toolName: string;
}
export interface ProjectionSubagent {
  agentName: string;
  invocationId: string;
  parentActionId: string;
  parentSessionId: string;
  status: "proposed" | "running" | "completed" | "error";
  taskId: string | null;
}
export interface ProjectionTask {
  status: "todo" | "in-progress" | "done";
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
  reasoningTokens: number;
  usageId: string;
}
