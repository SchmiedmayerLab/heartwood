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
    technicalDetail: z.string().nullable().optional(),
  })
  .strict();

const approvalActionSchema = z
  .object({
    targetId: z.string(),
    toolName: z.string(),
    risk: z.string().nullable(),
    summary: z.string().nullable(),
    arguments: z.record(z.string(), jsonValueSchema),
  })
  .strict();

const approvalGroupSchema = z
  .object({
    groupId: z.string(),
    actions: z.array(approvalActionSchema),
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

const taskSchema = z
  .object({
    title: z.string(),
    status: z.enum(["todo", "in-progress", "done"]),
  })
  .strict();

const usageSchema = z
  .object({
    usageId: z.string(),
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
    status: z.enum(["proposed", "running", "completed", "error"]),
    parentSessionId: z.string(),
    parentActionId: z.string(),
  })
  .strict();

const commandOutcomeSchema = z
  .object({
    commandId: z.string(),
    commandKind: z.string(),
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
    streamEpoch: z.string(),
    streamRevision: z.number().int().nonnegative(),
    activity: z.array(activitySchema),
    conversation: z.array(messageSchema),
    pendingApproval: approvalGroupSchema.nullable(),
    context: modelContextSchema,
    lifecycle: lifecycleSchema,
    lastCommandOutcome: commandOutcomeSchema.nullable(),
    taskPlan: z.array(taskSchema),
    usage: usageSchema.nullable(),
    usageByPurpose: z.array(usageSchema),
    subagents: z.array(subagentSchema),
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
