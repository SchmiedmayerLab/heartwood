/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

import type { SessionEvent } from "../types";

export const event = (
  sequence: number,
  kind: SessionEvent["kind"],
  payload: SessionEvent["payload"],
): SessionEvent => ({
  schema_version: "heartwood.session-event.v1",
  event_id: `session-test-event-${String(sequence).padStart(6, "0")}`,
  session_id: "session-test",
  sequence,
  kind,
  occurred_at: "2026-01-01T00:00:00Z",
  payload,
  previous_event_hash: null,
});

export const syntheticEvents = (): SessionEvent[] => [
  event(0, "command.received", { command_id: "session-test-detect-000000" }),
  event(1, "detection.proposed", {
    dataset: {
      confidence: 0.95,
      dataset_type: "omop-cdm",
      evidence: ["found synthetic person table"],
      source_id: "synthetic-omop",
    },
    platform: {
      adapter_id: "generic",
      confidence: 1,
      evidence: ["generic fallback"],
    },
  }),
  event(2, "model_call.decision.recorded", {
    decision: {
      capability_tier: "supervised",
      decision: "allow",
      decision_id: "decision-synthetic-model-call",
      endpoint: "http://127.0.0.1:8765/v1/chat/completions",
      policy_profile_id: "generic-default",
      reason: "endpoint and capability tier are allowed",
    },
    provider_route: {
      auth: "none",
      capability_tier: "supervised",
      endpoint: "http://127.0.0.1:8765/v1/chat/completions",
      model: "heartwood-local-runtime",
      provider: "openai-compatible",
      route_id: "local-loopback",
    },
    response_metadata: {
      choices_count: 1,
      model: "heartwood-local-runtime",
      response_preview: "Synthetic local model response.",
      status: "ok",
      usage: {
        total_tokens: 2,
      },
    },
  }),
  event(3, "agent_message.emitted", {
    content:
      "Prepared a local workspace action over the detected synthetic dataset.",
  }),
  event(4, "tool_call.proposed", {
    risk: "low",
    summary: "write a synthetic workspace summary artifact",
    tool_call_id: "session-test-toolcall-0",
    tool_name: "heartwood.local.write_summary",
  }),
];
