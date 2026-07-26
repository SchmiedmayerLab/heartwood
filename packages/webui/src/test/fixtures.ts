/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

import type { SessionEvent, SessionProjection } from "../types";

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
  event(0, "command.received", { command_id: "session-test-chat-000000" }),
  event(1, "user_message.recorded", {
    actor_id: "human",
    command_id: "session-test-chat-000002",
    content: "Build the synthetic target-condition cohort",
  }),
  event(2, "model_call.decision.recorded", {
    decision: {
      capability_tier: "supervised",
      decision: "allow",
      decision_id: "decision-synthetic-model-call",
      endpoint: "http://127.0.0.1:8765/v1/chat/completions",
      policy_profile_id: "generic-default",
      reason: "model route policy allows the configured profile",
    },
    model_profile: {
      backend_id: "openhands-sdk",
      profile_id: "local-smoke",
      capability_tier: "supervised",
      action_confirmation_mode: "always-confirm",
    },
  }),
  event(3, "agent_message.emitted", {
    content: "I will run the repository-verified cohort Skill.",
  }),
  event(4, "tool_call.proposed", {
    risk: "low",
    summary: "build the aggregate synthetic target-condition cohort",
    tool_call_id: "session-test-toolcall-0",
    tool_name: "terminal",
  }),
  event(5, "confirmation.requested", {
    request: {
      request_id: "session-test-toolcall-0-confirm",
      risk: "low",
      summary: "build the aggregate synthetic target-condition cohort",
      arguments: {
        command: "python run.py --output /project/cohort-summary.json",
      },
      tool_call_id: "session-test-toolcall-0",
      tool_name: "terminal",
    },
  }),
];

export const emptyProjection = (
  sessionId = "session-test",
): SessionProjection => ({
  schema_version: "heartwood.session-projection.v1",
  sessionId,
  eventCount: 0,
  revision: -1,
  streamEpoch: "synthetic-epoch",
  streamRevision: 0,
  activity: [],
  conversation: [],
  pendingApproval: null,
  context: {
    modelEndpoint: null,
    modelDecision: null,
    modelReason: null,
  },
  lifecycle: {
    status: "idle",
    canPause: false,
    canResume: false,
    canSteer: true,
  },
  lastCommandOutcome: null,
  taskPlan: [],
  usage: null,
  usageByPurpose: [],
  subagents: [],
  streamingText: "",
  availableCommands: ["chat"],
  paused: false,
});

export const syntheticProjection = (
  overrides: Partial<SessionProjection> = {},
): SessionProjection => ({
  ...emptyProjection(),
  eventCount: 6,
  revision: 5,
  streamRevision: 0,
  activity: syntheticEvents().map((item) => ({
    sequence: item.sequence,
    kind: item.kind,
    label: item.kind,
    detail: "",
  })),
  conversation: [
    {
      id: "local-session-test-chat-000002",
      sequence: 1,
      role: "user",
      label: "You",
      content: "Build the synthetic target-condition cohort",
      detail: null,
    },
    {
      id: "session-test-event-000003-agent",
      sequence: 3,
      role: "agent",
      label: "Agent",
      content: "I will run the repository-verified cohort Skill.",
      detail: null,
    },
    {
      id: "session-test-event-000004-trace",
      sequence: 4,
      role: "trace",
      label: "Trace",
      content: "Proposed terminal command",
      detail: "build the aggregate synthetic target-condition cohort",
      technicalDetail: JSON.stringify(
        { command: "python run.py --output /project/cohort-summary.json" },
        null,
        2,
      ),
    },
  ],
  pendingApproval: {
    groupId: "action-set-session-test",
    actions: [
      {
        targetId: "session-test-toolcall-0",
        toolName: "terminal",
        risk: "low",
        summary: "build the aggregate synthetic target-condition cohort",
        arguments: {
          command: "python run.py --output /project/cohort-summary.json",
        },
      },
    ],
    decision: null,
    decisionScope: "all",
  },
  context: {
    modelEndpoint: "http://127.0.0.1:8765/v1/chat/completions",
    modelDecision: "allow",
    modelReason: "model route policy allows the configured profile",
  },
  lifecycle: {
    status: "waiting-for-confirmation",
    canPause: false,
    canResume: false,
    canSteer: false,
  },
  availableCommands: ["approve", "deny"],
  ...overrides,
});
