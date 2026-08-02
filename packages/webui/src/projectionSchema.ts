/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

import { z } from "zod";
import { sessionProjectionJsonSchema } from "./sessionProjectionSchema.generated";
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

export const sessionProjectionSchema = z.fromJSONSchema(
  sessionProjectionJsonSchema as unknown as Parameters<
    typeof z.fromJSONSchema
  >[0],
) as z.ZodType<SessionProjection>;

export const sessionProjectionResponseSchema = z
  .object({
    events: z.array(sessionEventSchema),
    projection: sessionProjectionSchema,
  })
  .strict();
