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
      ]),
    );
    expect(viewModel.approvalControls).toEqual([
      expect.objectContaining({
        decision: null,
        arguments: {
          command: "python run.py --output /project/cohort-summary.json",
        },
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
    expect(viewModel.conversation.at(-1)).toMatchObject({
      content: "The task could not be completed",
      detail: "synthetic error",
      label: "System",
    });
  });

  it("keeps provider implementation errors out of the researcher conversation", () => {
    const viewModel = buildViewModel([
      event(0, "error.recorded", {
        reason: "OpenHands conversation failed: ConversationRunError",
      }),
    ]);

    expect(viewModel.conversation).toEqual([
      expect.objectContaining({
        content: "The task could not be completed",
        detail: "Check Model setup and Activity & audit, then try again.",
        label: "System",
      }),
    ]);
    expect(viewModel.activity[0]?.detail).toBe(
      "OpenHands conversation failed: ConversationRunError",
    );
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

  it("keeps exact action arguments in conversation replay after resolution", () => {
    const viewModel = buildViewModel([
      event(0, "tool_call.proposed", {
        arguments: {
          command: "create",
          file_text: "heartwood-corrected-review-ok\n",
          path: "/project/cohort-summary.txt",
        },
        risk: "medium",
        summary: "Write the reviewed aggregate",
        tool_call_id: "toolcall-1",
        tool_name: "file_editor",
      }),
      event(1, "confirmation.requested", {
        request: {
          arguments: {
            command: "create",
            file_text: "heartwood-corrected-review-ok\n",
            path: "/project/cohort-summary.txt",
          },
          tool_call_id: "toolcall-1",
          tool_name: "file_editor",
        },
      }),
      event(2, "confirmation.resolved", {
        decision: "denied",
        tool_call_id: "toolcall-1",
      }),
    ]);

    expect(viewModel.approvalControls[0]?.decision).toBe("denied");
    expect(viewModel.conversation[0]?.detail).toContain("Arguments:");
    expect(viewModel.conversation[0]?.detail).toContain('"command": "create"');
    expect(viewModel.conversation[0]?.detail).toContain(
      '"path": "/project/cohort-summary.txt"',
    );
    expect(viewModel.conversation[0]?.detail).toContain(
      '"file_text": "heartwood-corrected-review-ok\\n"',
    );
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

    expect(viewModel.context.modelDecision).toBe("true");
    expect(viewModel.conversation).toEqual([]);
    expect(viewModel.approvalControls).toEqual([]);
    expect(viewModel.activity.at(-1)?.detail).toBe("7");
  });

  it("projects missing optional values into stable researcher-facing defaults", () => {
    const viewModel = buildViewModel([
      event(0, "tool_call.proposed", { tool_name: "terminal" }),
      event(1, "tool.execution.recorded", {}),
      event(2, "confirmation.requested", {
        request: { tool_call_id: "toolcall-default" },
      }),
      event(3, "confirmation.resolved", {
        tool_call_id: "toolcall-default",
      }),
      event(4, "model_call.decision.recorded", { decision: {} }),
      event(5, "detection.proposed", { dataset: null, platform: [] }),
      event(6, "audit.export.recorded", { event_count: "unknown" }),
      event(7, "error.recorded", {}),
    ]);

    expect(viewModel.conversation).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          content: "Proposed tool: terminal",
          detail: null,
        }),
        expect.objectContaining({
          content: "Ran tool",
          detail: "Exit unknown",
        }),
        expect.objectContaining({
          content: "The task could not be completed",
          detail: "Review Activity & audit, then try again.",
        }),
      ]),
    );
    expect(viewModel.approvalControls).toEqual([
      {
        arguments: {},
        decision: "approved",
        label: "approved tool-call",
        risk: null,
        summary: null,
        targetId: "toolcall-default",
        targetType: "tool-call",
        toolName: "",
      },
    ]);
    expect(viewModel.context).toEqual({
      dataset: null,
      modelDecision: null,
      modelEndpoint: null,
      modelReason: null,
      platform: null,
    });
    expect(viewModel.activity[6]?.detail).toBe("Scrubbed JSONL ready");
  });
});
