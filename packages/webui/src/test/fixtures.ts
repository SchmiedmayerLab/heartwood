/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

import type {
  CredentialIsolation,
  PlatformCapabilities,
  SessionEvent,
  SessionProjection,
} from "../types";

export const credentialIsolation = (): CredentialIsolation => ({
  status: "not-required",
  boundary: "credential-free",
  unattended_actions_allowed: true,
  summary: "The selected model route does not place a credential in Heartwood.",
});

export const genericCapabilities = (
  overrides: Partial<PlatformCapabilities> = {},
): PlatformCapabilities => ({
  platform_id: "generic",
  display_name: "Workstation or container",
  interfaces: ["terminal", "web", "notebook"],
  browser_route: "direct",
  ingress_modes: ["direct-loopback", "jupyter-proxy", "trusted-proxy"],
  default_ingress_mode: "direct-loopback",
  managed_runtimes: ["llama-cpp", "vllm"],
  scheduler: "none",
  persistent_storage: "The project directory",
  credential_backends: ["process", "keyring", "mounted-file"],
  model_sources: ["heartwood", "openai", "anthropic", "custom"],
  platform_isolated_model_sources: [],
  managed_model_connections: [],
  validation_level: "ci",
  ...overrides,
});

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
    content:
      "## Plan\n\nI will run the repository-verified cohort Skill.\n\n- Validate the synthetic inputs\n- Create the cohort summary",
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

export const syntheticAction = (
  overrides: Partial<SessionProjection["actions"][number]> = {},
): SessionProjection["actions"][number] => ({
  schema_version: "heartwood.action-record.v1",
  toolCallId: "session-test-toolcall-0",
  actionId: "session-test-action-0",
  groupId: "action-set-session-test",
  toolName: "terminal",
  risk: "low",
  summary: "build the aggregate synthetic target-condition cohort",
  arguments: {
    command: "python run.py --output /project/cohort-summary.json",
  },
  details: {
    kind: "terminal",
    command: "python run.py --output /project/cohort-summary.json",
    isInput: false,
    reset: false,
    timeout: null,
  },
  affectedPaths: [],
  state: "awaiting-review",
  decision: null,
  outcome: null,
  proposedSequence: 4,
  updatedSequence: 5,
  ...overrides,
});

export const emptyProjection = (
  sessionId = "session-test",
): SessionProjection => ({
  schema_version: "heartwood.session-projection.v1",
  sessionId,
  eventCount: 0,
  revision: -1,
  workspaceRevision: -1,
  streamEpoch: "synthetic-epoch",
  streamRevision: 0,
  activity: [],
  conversation: [],
  actions: [],
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
  researcherStatus: {
    code: "ready",
    label: "Ready",
    detail: "Heartwood is ready for the next task.",
    tone: "neutral",
    recoverable: true,
  },
  researcherNotice: null,
  lastCommandOutcome: null,
  taskPlan: [],
  usage: null,
  usageByPurpose: [],
  subagents: [],
  suggestions: [
    {
      suggestionId: "inspect-project",
      label: "Inspect the Project",
      prompt:
        "Inspect this project and summarize its structure, relevant files, and likely entry points without changing files.",
      kind: "task",
    },
    {
      suggestionId: "plan-project",
      label: "Plan the Work",
      prompt:
        "Review this project and propose a concise, verifiable plan before making changes.",
      kind: "task",
    },
  ],
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
      technicalDetail: null,
    },
    {
      id: "session-test-event-000003-agent",
      sequence: 3,
      role: "agent",
      label: "Agent",
      content:
        "## Plan\n\nI will run the repository-verified cohort Skill.\n\n- Validate the synthetic inputs\n- Create the cohort summary",
      detail: null,
      technicalDetail: null,
    },
  ],
  pendingApproval: {
    groupId: "action-set-session-test",
    actions: [syntheticAction()],
    decision: null,
    decisionScope: "all",
  },
  actions: [syntheticAction()],
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
  researcherStatus: {
    code: "waiting-for-review",
    label: "Waiting for Action Review",
    detail: "Review the complete proposed action set to continue.",
    tone: "attention",
    recoverable: true,
  },
  suggestions: [],
  availableCommands: ["approve", "deny"],
  ...overrides,
});
