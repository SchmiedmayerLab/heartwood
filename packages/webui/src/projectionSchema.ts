/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

import { z } from "zod";
import type { JsonValue, SessionEvent, SessionProjection } from "./types";

const jsonValueSchema: z.ZodType<JsonValue> = z.lazy(() =>
  z.union([
    z.string(),
    z.number(),
    z.boolean(),
    z.null(),
    z.array(jsonValueSchema),
    z.record(z.string(), jsonValueSchema),
  ]),
);

const eventKindSchema = z.enum([
  "command.received",
  "approval.recorded",
  "policy.decision.recorded",
  "model_call.decision.recorded",
  "user_message.recorded",
  "agent_message.emitted",
  "tool_call.proposed",
  "confirmation.requested",
  "confirmation.resolved",
  "tool.execution.recorded",
  "session.paused",
  "session.resumed",
  "agent.lifecycle.updated",
  "task.plan.updated",
  "model.usage.updated",
  "subagent.updated",
  "audit.export.recorded",
  "error.recorded",
]);

const commandKindSchema = z.enum([
  "approve",
  "deny",
  "chat",
  "pause",
  "resume",
  "replay",
  "audit.export",
]);

const sessionEventSchema: z.ZodType<SessionEvent> = z
  .object({
    schema_version: z.literal("heartwood.session-event.v1"),
    event_id: z.string(),
    session_id: z.string(),
    sequence: z.number().int().nonnegative(),
    kind: eventKindSchema,
    occurred_at: z.string(),
    payload: z.record(z.string(), jsonValueSchema),
    previous_event_hash: z.string().nullable(),
  })
  .strict();

const activitySchema = z
  .object({
    sequence: z.number().int().nonnegative(),
    kind: eventKindSchema,
    label: z.string(),
    detail: z.string(),
  })
  .strict();

const messageSchema = z
  .object({
    id: z.string(),
    sequence: z.number().int().nonnegative(),
    role: z.enum(["user", "agent", "trace"]),
    label: z.string(),
    content: z.string(),
    detail: z.string().nullable(),
    technicalDetail: z.string().nullable(),
  })
  .strict();

const actionDetailsSchema = z.discriminatedUnion("kind", [
  z
    .object({
      kind: z.literal("terminal"),
      command: z.string(),
      isInput: z.boolean(),
      reset: z.boolean(),
      timeout: z.number().nonnegative().nullable(),
    })
    .strict(),
  z
    .object({
      kind: z.literal("file-editor"),
      operation: z.enum([
        "view",
        "create",
        "str_replace",
        "insert",
        "undo_edit",
        "unknown",
      ]),
      path: z.string().nullable(),
    })
    .strict(),
  z
    .object({
      kind: z.literal("task"),
      description: z.string().nullable(),
      prompt: z.string().nullable(),
      subagentType: z.string().nullable(),
      resume: z.string().nullable(),
    })
    .strict(),
  z.object({ kind: z.literal("other") }).strict(),
]);

const actionRecordSchema = z
  .object({
    schema_version: z.literal("heartwood.action-record.v1"),
    toolCallId: z.string(),
    actionId: z.string().nullable(),
    groupId: z.string().nullable(),
    toolName: z.string(),
    risk: z.enum(["high", "low", "medium", "unknown"]),
    summary: z.string(),
    arguments: z.record(z.string(), jsonValueSchema),
    details: actionDetailsSchema,
    affectedPaths: z.array(
      z
        .object({
          path: z.string(),
          effect: z.enum(["created", "modified", "deleted", "unknown"]),
          provenance: z.literal("file-editor-action"),
        })
        .strict(),
    ),
    state: z.enum([
      "proposed",
      "awaiting-review",
      "approved",
      "rejected",
      "running",
      "succeeded",
      "failed",
      "outcome-unknown",
    ]),
    decision: z.enum(["approved", "rejected"]).nullable(),
    outcome: z
      .object({
        exitCode: z.number().int(),
        summary: z.string(),
        result: z.string().nullable(),
        resultTruncated: z.boolean(),
      })
      .strict()
      .nullable(),
    proposedSequence: z.number().int().nonnegative(),
    updatedSequence: z.number().int().nonnegative(),
  })
  .strict();

const approvalGroupSchema = z
  .object({
    groupId: z.string(),
    actions: z.array(actionRecordSchema),
    decision: z.enum(["approved", "denied"]).nullable(),
    decisionScope: z.literal("all"),
  })
  .strict();

const modelContextSchema = z
  .object({
    modelEndpoint: z.string().nullable(),
    modelDecision: z.string().nullable(),
    modelReason: z.string().nullable(),
  })
  .strict();

const lifecycleSchema = z
  .object({
    status: z.enum([
      "idle",
      "running",
      "paused",
      "waiting-for-confirmation",
      "finished",
      "error",
    ]),
    canPause: z.boolean(),
    canResume: z.boolean(),
    canSteer: z.boolean(),
  })
  .strict();

const researcherStatusSchema = z
  .object({
    code: z.enum([
      "ready",
      "working",
      "waiting-for-review",
      "paused",
      "complete",
      "denied",
      "recoverable-failure",
      "terminal-failure",
    ]),
    label: z.string(),
    detail: z.string(),
    tone: z.enum(["neutral", "progress", "attention", "success", "danger"]),
    recoverable: z.boolean(),
  })
  .strict();

const taskSchema = z
  .object({
    title: z.string(),
    status: z.enum(["todo", "in-progress", "done"]),
    statusLabel: z.string(),
  })
  .strict();

const usageSchema = z
  .object({
    usageId: z.string(),
    purposeLabel: z.string(),
    modelName: z.string(),
    callCount: z.number().int().nonnegative(),
    promptTokens: z.number().int().nonnegative(),
    completionTokens: z.number().int().nonnegative(),
    cacheReadTokens: z.number().int().nonnegative(),
    cacheWriteTokens: z.number().int().nonnegative(),
    reasoningTokens: z.number().int().nonnegative(),
    contextWindow: z.number().int().nonnegative().nullable(),
    accumulatedCost: z.number().nonnegative(),
  })
  .strict();

const subagentSchema = z
  .object({
    invocationId: z.string(),
    taskId: z.string().nullable(),
    agentName: z.string(),
    roleLabel: z.string(),
    status: z.enum(["proposed", "running", "completed", "error"]),
    statusLabel: z.string(),
    taskSummary: z.string().nullable(),
    resultSummary: z.string().nullable(),
    parentSessionId: z.string(),
    parentActionId: z.string(),
  })
  .strict();

const suggestionSchema = z
  .object({
    suggestionId: z.enum([
      "inspect-project",
      "plan-project",
      "continue-plan",
      "review-changes",
      "verify-work",
      "recover-task",
      "identify-next-step",
    ]),
    label: z.string(),
    prompt: z.string(),
    kind: z.enum(["task", "follow-up", "recovery"]),
  })
  .strict();

const commandOutcomeSchema = z
  .object({
    commandId: z.string(),
    commandKind: commandKindSchema,
    status: z.enum(["accepted", "rejected"]),
    errorCode: z.string().nullable(),
    message: z.string().nullable(),
  })
  .strict();

export const sessionProjectionSchema: z.ZodType<SessionProjection> = z
  .object({
    schema_version: z.literal("heartwood.session-projection.v1"),
    sessionId: z.string(),
    eventCount: z.number().int().nonnegative(),
    revision: z.number().int().min(-1),
    workspaceRevision: z.number().int().min(-1),
    streamEpoch: z.string(),
    streamRevision: z.number().int().nonnegative(),
    activity: z.array(activitySchema),
    conversation: z.array(messageSchema),
    actions: z.array(actionRecordSchema),
    pendingApproval: approvalGroupSchema.nullable(),
    context: modelContextSchema,
    lifecycle: lifecycleSchema,
    researcherStatus: researcherStatusSchema,
    lastCommandOutcome: commandOutcomeSchema.nullable(),
    taskPlan: z.array(taskSchema),
    usage: usageSchema.nullable(),
    usageByPurpose: z.array(usageSchema),
    subagents: z.array(subagentSchema),
    suggestions: z.array(suggestionSchema),
    streamingText: z.string(),
    availableCommands: z.array(
      z.enum(["approve", "chat", "deny", "pause", "resume"]),
    ),
    paused: z.boolean(),
  })
  .strict();

export const sessionProjectionResponseSchema = z
  .object({
    events: z.array(sessionEventSchema),
    projection: sessionProjectionSchema,
  })
  .strict();
