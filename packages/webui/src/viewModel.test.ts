/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

import { describe, expect, it } from "vitest";
import { event, syntheticEvents } from "./test/fixtures";
import { buildViewModel } from "./viewModel";

describe("buildViewModel", () => {
  it("projects the conversation and actual pending action", () => {
    const viewModel = buildViewModel(syntheticEvents());

    expect(viewModel.sessionId).toBe("session-test");
    expect(viewModel.conversation).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          content: "Build the synthetic target-condition cohort",
          label: "You",
          role: "user",
        }),
        expect.objectContaining({
          content: "I will run the repository-verified cohort Skill.",
          label: "Agent",
          role: "agent",
        }),
        expect.objectContaining({
          content: "Proposed tool: terminal",
          label: "Trace",
          role: "trace",
        }),
        expect.objectContaining({ content: "allow model route" }),
      ]),
    );
    expect(viewModel.approvalControls).toEqual([
      expect.objectContaining({
        decision: null,
        risk: "low",
        summary: "build the aggregate synthetic target-condition cohort",
        targetId: "session-test-toolcall-0",
        targetType: "tool-call",
        toolName: "terminal",
      }),
    ]);
    expect(viewModel.context).toEqual({
      platform: "generic",
      dataset: "omop-cdm",
      modelEndpoint: "http://127.0.0.1:8765/v1/chat/completions",
      modelDecision: "allow",
      modelReason: "model route policy allows the configured profile",
    });
  });

  it("projects lifecycle, exports, errors, and confirmation results", () => {
    const viewModel = buildViewModel([
      event(0, "confirmation.resolved", {
        decision: "approved",
        tool_call_id: "toolcall-1",
      }),
      event(1, "tool.execution.recorded", {
        exit_code: 0,
        summary: "Wrote summary",
        tool_name: "terminal",
      }),
      event(2, "audit.export.recorded", {
        event_count: 3,
        path: "/audit.jsonl",
      }),
      event(3, "session.paused", {}),
      event(4, "session.resumed", {}),
      event(5, "error.recorded", { reason: "synthetic error" }),
    ]);

    expect(viewModel.approvalControls).toEqual([
      expect.objectContaining({ decision: "approved", targetId: "toolcall-1" }),
    ]);
    expect(viewModel.paused).toBe(false);
    expect(viewModel.activity[1]?.detail).toBe("exit=0");
    expect(viewModel.conversation[0]).toMatchObject({
      content: "Ran terminal",
      detail: "Wrote summary",
    });
    expect(viewModel.activity[2]?.detail).toBe("3 events, scrubbed JSONL");
    expect(viewModel.activity.at(-1)?.detail).toBe("synthetic error");
  });

  it("does not invent an approval before OpenHands requests confirmation", () => {
    const viewModel = buildViewModel([
      event(0, "tool_call.proposed", {
        summary: "inspect",
        tool_call_id: "toolcall-1",
        tool_name: "terminal",
      }),
    ]);

    expect(viewModel.approvalControls).toEqual([]);
  });

  it("coalesces repeated confirmation state", () => {
    const viewModel = buildViewModel([
      event(0, "confirmation.requested", {
        request: { tool_call_id: "toolcall-1", tool_name: "terminal" },
      }),
      event(1, "confirmation.requested", {
        request: { tool_call_id: "toolcall-1", tool_name: "terminal" },
      }),
      event(2, "confirmation.resolved", {
        decision: "denied",
        tool_call_id: "toolcall-1",
      }),
    ]);

    expect(viewModel.approvalControls).toEqual([
      expect.objectContaining({ decision: "denied", targetId: "toolcall-1" }),
    ]);
  });

  it("uses safe defaults for malformed optional values", () => {
    const viewModel = buildViewModel([
      event(0, "model_call.decision.recorded", {
        decision: { decision: true, reason: [] },
      }),
      event(1, "agent_message.emitted", { content: null }),
      event(2, "confirmation.resolved", {}),
      event(3, "command.received", { command_id: 7 }),
    ]);

    expect(viewModel.conversation[0]).toMatchObject({
      content: "true model route",
      detail: null,
    });
    expect(viewModel.approvalControls).toEqual([]);
    expect(viewModel.activity.at(-1)?.detail).toBe("7");
  });
});
