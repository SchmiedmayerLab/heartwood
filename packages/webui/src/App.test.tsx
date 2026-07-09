/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { App } from "./App";
import type { HeartwoodClient, SessionEventResponse } from "./client";
import { event, syntheticEvents } from "./test/fixtures";
import type { SessionCommand, SessionEvent } from "./types";

class FakeClient implements HeartwoodClient {
  commands: SessionCommand[] = [];
  replayCalls = 0;

  postCommand(command: SessionCommand): Promise<SessionEventResponse> {
    this.commands.push(command);
    return Promise.resolve({
      events: command.kind === "detect" ? syntheticEvents().slice(0, 2) : [],
    });
  }

  replayEvents(): Promise<SessionEventResponse> {
    this.replayCalls += 1;
    return Promise.resolve({
      events:
        this.replayCalls === 1 ?
          []
        : [
            ...syntheticEvents(),
            event(5, "approval.recorded", {
              approval: {
                decision: "denied",
                reason: "synthetic denial",
                target_id: "session-test-toolcall-0",
                target_type: "tool-call",
              },
            }),
            event(6, "audit.export.recorded", {
              path: "/workspace/.heartwood/audit/export.jsonl",
            }),
          ],
    });
  }

  streamEvents(
    _sessionId: string,
    _afterSequence: number | undefined,
    _onEvents: (events: SessionEvent[]) => void,
  ): () => void {
    return vi.fn();
  }
}

describe("App", () => {
  it("renders session state and sends gateway commands", async () => {
    const client = new FakeClient();
    render(<App client={client} initialSessionId="session-test" />);

    await waitFor(() =>
      expect(screen.getByLabelText("Session ID")).toHaveValue("session-test"),
    );
    fireEvent.click(screen.getByLabelText("Detect"));

    await waitFor(() => expect(client.commands).toHaveLength(1));
    expect(client.commands[0]?.kind).toBe("detect");
    expect(await screen.findByText("omop-cdm")).toBeInTheDocument();
  });

  it("sends provider route run settings and rehydrates replayed events", async () => {
    const client = new FakeClient();
    render(<App client={client} initialSessionId="session-test" />);

    await waitFor(() => expect(client.replayCalls).toBe(1));
    expect(screen.getByLabelText("Provider Config")).toHaveValue(
      "/opt/heartwood/images/generic/providers/provider-routes.example.toml",
    );
    expect(screen.getByLabelText("Route")).toHaveValue("local-loopback");
    expect(screen.getByLabelText("Invoke Provider")).toBeChecked();
    fireEvent.click(screen.getByRole("button", { name: "Run Local Model" }));

    await waitFor(() => expect(client.commands.at(-1)?.kind).toBe("run"));
    expect(client.commands.at(-1)?.payload).toMatchObject({
      invoke_provider: true,
      provider_config_path:
        "/opt/heartwood/images/generic/providers/provider-routes.example.toml",
      provider_route_id: "local-loopback",
    });

    fireEvent.click(screen.getByLabelText("Replay"));

    expect(await screen.findAllByText(/local-loopback/u)).toHaveLength(2);
    expect(
      await screen.findByText("Synthetic local model response."),
    ).toBeInTheDocument();
    await waitFor(() =>
      expect(
        screen.getAllByText("/workspace/.heartwood/audit/export.jsonl"),
      ).toHaveLength(2),
    );
    expect(client.replayCalls).toBe(2);
  });

  it("sends approval decisions from rendered controls", async () => {
    const client = new PendingApprovalClient();
    render(<App client={client} initialSessionId="session-test" />);

    await waitFor(() => expect(client.replayCalls).toBe(1));
    fireEvent.click(screen.getByLabelText("Replay"));
    fireEvent.click(
      await screen.findByLabelText("Approve session-test-toolcall-0"),
    );

    await waitFor(() => expect(client.commands.at(-1)?.kind).toBe("approve"));
    expect(client.commands.at(-1)?.payload).toMatchObject({
      reason: "web UI demo decision",
      target_id: "session-test-toolcall-0",
      target_type: "tool-call",
    });
  });
});

class PendingApprovalClient extends FakeClient {
  override replayEvents(): Promise<SessionEventResponse> {
    this.replayCalls += 1;
    return Promise.resolve({
      events: this.replayCalls === 1 ? [] : syntheticEvents(),
    });
  }
}

class RejectingClient extends FakeClient {
  override postCommand(): Promise<SessionEventResponse> {
    return Promise.reject(new Error("synthetic gateway failure"));
  }
}

describe("App error handling", () => {
  it("renders gateway command errors", async () => {
    render(
      <App client={new RejectingClient()} initialSessionId="session-test" />,
    );

    await waitFor(() =>
      expect(screen.getByLabelText("Session ID")).toHaveValue("session-test"),
    );
    fireEvent.click(screen.getByLabelText("Export Audit"));

    expect(await screen.findByText("synthetic gateway failure")).toBeVisible();
  });
});
