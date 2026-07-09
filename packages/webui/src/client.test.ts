/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { GatewayClient, createCommand } from "./client";
import { syntheticEvents } from "./test/fixtures";
import type { SessionEvent } from "./types";

class FakeWebSocket {
  static instances: FakeWebSocket[] = [];

  onerror: (() => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  onmessage: ((message: MessageEvent<string>) => void) | null = null;
  readonly close = vi.fn();

  constructor(readonly url: string) {
    FakeWebSocket.instances.push(this);
  }

  fail(): void {
    this.onerror?.();
  }

  closeWith(code: number): void {
    this.onclose?.(new CloseEvent("close", { code }));
  }

  emit(events: SessionEvent[]): void {
    this.onmessage?.(
      new MessageEvent("message", {
        data: JSON.stringify({ events }),
      }),
    );
  }
}

class FakeEventSource {
  static instances: FakeEventSource[] = [];

  private listener: ((message: MessageEvent<string>) => void) | null = null;
  readonly close = vi.fn();

  constructor(readonly url: string) {
    FakeEventSource.instances.push(this);
  }

  addEventListener(
    eventName: string,
    listener: (message: MessageEvent<string>) => void,
  ): void {
    if (eventName === "heartwood-session-events") {
      this.listener = listener;
    }
  }

  emit(events: SessionEvent[]): void {
    this.listener?.(
      new MessageEvent("heartwood-session-events", {
        data: JSON.stringify({ events }),
      }),
    );
  }
}

afterEach(() => {
  vi.unstubAllGlobals();
  FakeWebSocket.instances = [];
  FakeEventSource.instances = [];
  window.history.pushState({}, "", "/");
});

describe("createCommand", () => {
  it("builds the shared session command envelope", () => {
    const command = createCommand("session-test", "run", 7, {
      prompt: "synthetic",
    });

    expect(command).toMatchObject({
      actor_id: "human",
      command_id: "session-test-run-000007",
      kind: "run",
      payload: { prompt: "synthetic" },
      schema_version: "heartwood.session-command.v1",
      session_id: "session-test",
    });
  });
});

describe("GatewayClient", () => {
  it("posts commands through the configured base path", async () => {
    const fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ events: syntheticEvents().slice(0, 1) }), {
        status: 200,
      }),
    );
    vi.stubGlobal("fetch", fetch);

    const client = new GatewayClient("/proxy/8767");
    const response = await client.postCommand(
      createCommand("session-test", "detect", 0),
    );

    expect(response.events).toHaveLength(1);
    expect(fetch).toHaveBeenCalledWith(
      "/proxy/8767/sessions/session-test/commands",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("reports gateway errors", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          new Response(JSON.stringify({ error: "denied" }), { status: 403 }),
        ),
    );

    const client = new GatewayClient();

    await expect(
      client.replayEvents("session-test"),
    ).rejects.toThrowErrorMatchingInlineSnapshot(`[Error: denied]`);
  });

  it("preserves gateway status for non-JSON error responses", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          new Response("<html>bad gateway</html>", { status: 502 }),
        ),
    );

    const client = new GatewayClient();

    await expect(
      client.replayEvents("session-test"),
    ).rejects.toThrowErrorMatchingInlineSnapshot(
      `[Error: Gateway request failed with status 502]`,
    );
  });

  it("infers the Jupyter proxy base path from the browser location", async () => {
    window.history.pushState({}, "", "/user/synthetic/proxy/8767/");
    const fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ events: syntheticEvents().slice(0, 1) }), {
        status: 200,
      }),
    );
    vi.stubGlobal("fetch", fetch);

    const client = new GatewayClient();
    await client.replayEvents("session-test", 0);

    expect(fetch).toHaveBeenCalledWith(
      "/user/synthetic/proxy/8767/sessions/session-test/events?after=0",
    );
  });

  it("falls back to server-sent events after a WebSocket error", () => {
    vi.stubGlobal("WebSocket", FakeWebSocket);
    vi.stubGlobal("EventSource", FakeEventSource);
    const received: SessionEvent[][] = [];
    const client = new GatewayClient("/proxy/8767");

    const cleanup = client.streamEvents("session-test", 2, (events) => {
      received.push(events);
    });
    FakeWebSocket.instances[0]?.fail();
    FakeEventSource.instances[0]?.emit(syntheticEvents().slice(2, 3));
    cleanup();

    expect(FakeWebSocket.instances[0]?.url).toContain(
      "/proxy/8767/sessions/session-test/events?after=2",
    );
    expect(FakeEventSource.instances[0]?.url).toBe(
      "/proxy/8767/sessions/session-test/events/stream?after=2",
    );
    expect(received[0]?.[0]?.kind).toBe("model_call.decision.recorded");
    expect(FakeEventSource.instances[0]?.close).toHaveBeenCalled();
  });

  it("falls back to server-sent events after an abnormal WebSocket close", () => {
    vi.stubGlobal("WebSocket", FakeWebSocket);
    vi.stubGlobal("EventSource", FakeEventSource);
    const received: SessionEvent[][] = [];
    const client = new GatewayClient("/proxy/8767");

    const cleanup = client.streamEvents("session-test", 2, (events) => {
      received.push(events);
    });
    FakeWebSocket.instances[0]?.closeWith(1011);
    FakeEventSource.instances[0]?.emit(syntheticEvents().slice(2, 3));
    cleanup();

    expect(FakeEventSource.instances[0]?.url).toBe(
      "/proxy/8767/sessions/session-test/events/stream?after=2",
    );
    expect(received[0]?.[0]?.kind).toBe("model_call.decision.recorded");
  });

  it("streams events over WebSocket when the upgrade succeeds", () => {
    vi.stubGlobal("WebSocket", FakeWebSocket);
    const received: SessionEvent[][] = [];
    const client = new GatewayClient();

    const cleanup = client.streamEvents("session-test", undefined, (events) => {
      received.push(events);
    });
    FakeWebSocket.instances[0]?.emit(syntheticEvents().slice(3, 4));
    cleanup();

    expect(received[0]?.[0]?.kind).toBe("agent_message.emitted");
    expect(FakeWebSocket.instances[0]?.close).toHaveBeenCalled();
  });
});
