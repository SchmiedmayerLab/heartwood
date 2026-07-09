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
  it("projects gateway events into the researcher UI state", () => {
    const viewModel = buildViewModel(syntheticEvents());

    expect(viewModel.sessionId).toBe("session-test");
    expect(viewModel.datasetProposals).toEqual([
      {
        confidence: 0.95,
        datasetType: "omop-cdm",
        evidence: ["found synthetic person table"],
        sourceId: "synthetic-omop",
      },
    ]);
    expect(viewModel.policyStatus[0]).toMatchObject({
      decision: "allow",
      provider: "openai-compatible",
      routeId: "local-loopback",
    });
    expect(viewModel.modelInvocations[0]).toMatchObject({
      choicesCount: 1,
      model: "heartwood-local-runtime",
      responsePreview: "Synthetic local model response.",
      routeId: "local-loopback",
      status: "ok",
      totalTokens: 2,
    });
    expect(
      viewModel.approvalControls.some(
        (control) => control.targetType === "tool-call",
      ),
    ).toBe(true);
  });

  it("projects approvals, lifecycle, exports, and errors", () => {
    const viewModel = buildViewModel([
      ...syntheticEvents(),
      event(5, "approval.recorded", {
        approval: {
          decision: "approved",
          reason: "synthetic approval",
          target_id: "heartwood.aggregate-export",
          target_type: "skill",
        },
      }),
      event(6, "confirmation.requested", {
        request: {
          tool_call_id: "session-test-toolcall-1",
          tool_name: "heartwood.local.write_summary",
        },
      }),
      event(7, "confirmation.resolved", {
        command_id: "session-test-approve-000007",
        decision: "approved",
        tool_call_id: "session-test-toolcall-1",
      }),
      event(8, "tool.execution.recorded", { exit_code: 0 }),
      event(9, "audit.export.recorded", {
        path: "/workspace/.heartwood/audit/export.jsonl",
      }),
      event(10, "session.paused", { command_id: "session-test-pause-000010" }),
      event(11, "session.resumed", {
        command_id: "session-test-resume-000011",
      }),
      event(12, "error.recorded", { reason: "synthetic error" }),
    ]);

    expect(viewModel.skillProposals).toContainEqual({
      detail: "synthetic approval",
      status: "approved",
      targetId: "heartwood.aggregate-export",
    });
    expect(viewModel.approvalControls).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          decision: "approved",
          targetId: "session-test-toolcall-1",
        }),
        expect.objectContaining({ targetType: "skill" }),
      ]),
    );
    expect(viewModel.exportActions).toEqual([
      {
        label: "Scrubbed audit JSONL",
        path: "/workspace/.heartwood/audit/export.jsonl",
      },
    ]);
    expect(viewModel.paused).toBe(false);
    expect(viewModel.activity.at(-1)?.detail).toBe("synthetic error");
  });

  it("uses empty defaults for partial payloads", () => {
    const viewModel = buildViewModel([
      event(0, "detection.proposed", {
        dataset: {
          confidence: "unknown",
          dataset_type: 12,
          evidence: "none",
          source_id: null,
        },
      }),
      event(1, "model_call.decision.recorded", {
        decision: {
          decision: true,
        },
      }),
    ]);

    expect(viewModel.datasetProposals[0]).toEqual({
      confidence: 0,
      datasetType: "12",
      evidence: [],
      sourceId: "",
    });
    expect(viewModel.policyStatus[0]).toMatchObject({
      decision: "true",
      provider: null,
      routeId: null,
    });
    expect(viewModel.modelInvocations[0]).toMatchObject({
      responsePreview: null,
      status: "pending",
    });
  });

  it("updates existing approval controls for recorded decisions", () => {
    const viewModel = buildViewModel([
      event(0, "tool_call.proposed", {
        summary: "write synthetic output",
        tool_call_id: "toolcall-1",
        tool_name: "heartwood.local.write_summary",
      }),
      event(1, "approval.recorded", {
        approval: {
          decision: "approved",
          reason: "synthetic approval",
          target_id: "toolcall-1",
          target_type: "tool-call",
        },
      }),
    ]);

    const controls = viewModel.approvalControls.filter(
      (control) => control.targetId === "toolcall-1",
    );
    expect(controls).toHaveLength(1);
    expect(controls[0]).toMatchObject({
      decision: "approved",
      label: "approved tool-call",
    });
  });

  it("resolves tool-call controls from confirmation events without duplicates", () => {
    const viewModel = buildViewModel([
      event(0, "tool_call.proposed", {
        summary: "write synthetic output",
        tool_call_id: "toolcall-1",
        tool_name: "heartwood.local.write_summary",
      }),
      event(1, "confirmation.requested", {
        request: {
          tool_call_id: "toolcall-1",
          tool_name: "heartwood.local.write_summary",
        },
      }),
      event(2, "confirmation.resolved", {
        decision: "approved",
        tool_call_id: "toolcall-1",
      }),
    ]);

    const controls = viewModel.approvalControls.filter(
      (control) => control.targetId === "toolcall-1",
    );
    expect(controls).toHaveLength(1);
    expect(controls[0]).toMatchObject({
      decision: "approved",
      label: "approved tool-call",
    });
  });
});
