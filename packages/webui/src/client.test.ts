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
  it("manages persisted session lifecycle routes", async () => {
    const session = {
      session_id: "session-test",
      title: "Synthetic analysis",
      status: "empty",
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z",
      event_count: 0,
    };
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ sessions: [session] })),
      )
      .mockResolvedValueOnce(new Response(JSON.stringify(session)))
      .mockResolvedValueOnce(new Response(JSON.stringify(session)))
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ ...session, title: "Renamed" })),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            filename: "session test-audit.jsonl",
            content: '{"kind":"audit.export.recorded"}\n',
          }),
        ),
      );
    vi.stubGlobal("fetch", fetch);
    const client = new GatewayClient("/proxy/8767");

    await client.listSessions();
    await client.createSession("Synthetic analysis");
    await client.getSession("session test");
    await client.renameSession("session test", "Renamed");
    const exported = await client.getAuditExport("session test");

    expect(fetch).toHaveBeenNthCalledWith(1, "/proxy/8767/sessions");
    expect(fetch).toHaveBeenNthCalledWith(
      2,
      "/proxy/8767/sessions",
      expect.objectContaining({
        body: JSON.stringify({ title: "Synthetic analysis" }),
        method: "POST",
      }),
    );
    expect(fetch).toHaveBeenNthCalledWith(
      3,
      "/proxy/8767/sessions/session%20test",
    );
    expect(fetch).toHaveBeenNthCalledWith(
      4,
      "/proxy/8767/sessions/session%20test",
      expect.objectContaining({
        body: JSON.stringify({ title: "Renamed" }),
        method: "PATCH",
      }),
    );
    expect(fetch).toHaveBeenNthCalledWith(
      5,
      "/proxy/8767/sessions/session%20test/audit-export",
    );
    expect(exported.filename).toBe("session test-audit.jsonl");
  });

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

  it("manages non-secret model profiles through settings routes", async () => {
    const settings = {
      schema_version: "heartwood.model-settings.v1",
      active_profile: "local",
      profiles: [],
      presets: [],
    };
    const validation = {
      profile: {},
      credential_status: "configured",
      policy_decision: { decision: "allow" },
    };
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(new Response(JSON.stringify(settings)))
      .mockResolvedValueOnce(new Response(JSON.stringify(settings)))
      .mockResolvedValueOnce(new Response(JSON.stringify(settings)))
      .mockResolvedValueOnce(new Response(JSON.stringify(settings)))
      .mockResolvedValueOnce(new Response(JSON.stringify(validation)));
    vi.stubGlobal("fetch", fetch);
    const client = new GatewayClient("/proxy/8767");
    const profile = {
      profile_id: "local",
      model: "openai/local-model",
      policy_endpoint: "http://127.0.0.1:8765/v1/chat/completions",
      capability_tier: "supervised" as const,
      base_url: "http://127.0.0.1:8765/v1",
      credential_kind: "none" as const,
      api_key_env: null,
      api_key_file: null,
      api_version: null,
      aws_region_name: null,
      aws_profile_name: null,
      description: null,
    };

    await client.getModelSettings();
    await client.saveModelProfile(profile);
    await client.selectModelProfile("local");
    await client.removeModelProfile("local");
    await client.validateModelProfile("local profile");

    expect(fetch).toHaveBeenNthCalledWith(1, "/proxy/8767/settings/models");
    expect(fetch).toHaveBeenNthCalledWith(
      2,
      "/proxy/8767/settings/models/profiles",
      expect.objectContaining({ method: "POST" }),
    );
    expect(fetch).toHaveBeenNthCalledWith(
      3,
      "/proxy/8767/settings/models/active",
      expect.objectContaining({ method: "PUT" }),
    );
    expect(fetch).toHaveBeenNthCalledWith(
      4,
      "/proxy/8767/settings/models/profiles/local",
      { method: "DELETE" },
    );
    expect(fetch).toHaveBeenNthCalledWith(
      5,
      "/proxy/8767/settings/models/validation?profile_id=local%20profile",
    );
  });

  it("configures a provider through the simplified settings route", async () => {
    const settings = {
      schema_version: "heartwood.model-settings.v1",
      active_profile: "openai",
      profiles: [],
      presets: [],
    };
    const fetch = vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify(settings)));
    vi.stubGlobal("fetch", fetch);
    const client = new GatewayClient("/proxy/8767");

    await client.connectModelProvider("openai", "configured-model");

    expect(fetch).toHaveBeenCalledWith(
      "/proxy/8767/settings/models/connect",
      expect.objectContaining({
        body: JSON.stringify({
          model_name: "configured-model",
          preset_id: "openai",
        }),
        method: "POST",
      }),
    );
  });

  it("lists and starts reviewed model downloads", async () => {
    const artifacts = {
      schema_version: "heartwood.local-model-catalog.v1",
      artifacts: [],
      downloads: [],
    };
    const download = {
      artifact_id: "reviewed-model",
      status: "downloading",
      bytes_downloaded: 0,
      bytes_total: 1024,
      path: null,
      error: null,
    };
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(new Response(JSON.stringify(artifacts)))
      .mockResolvedValueOnce(new Response(JSON.stringify(download)));
    vi.stubGlobal("fetch", fetch);
    const client = new GatewayClient("/proxy/8767");

    await client.getModelArtifacts();
    await client.downloadModelArtifact("reviewed-model");

    expect(fetch).toHaveBeenNthCalledWith(
      1,
      "/proxy/8767/settings/models/artifacts",
    );
    expect(fetch).toHaveBeenNthCalledWith(
      2,
      "/proxy/8767/settings/models/downloads",
      expect.objectContaining({
        body: JSON.stringify({ artifact_id: "reviewed-model" }),
        method: "POST",
      }),
    );
  });

  it("selects the shared action-confirmation mode", async () => {
    const actions = {
      schema_version: "heartwood.action-settings.v1",
      confirmation_mode: "always-confirm",
      modes: [],
    };
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(new Response(JSON.stringify(actions)))
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({ ...actions, confirmation_mode: "confirm-risky" }),
        ),
      );
    vi.stubGlobal("fetch", fetch);
    const client = new GatewayClient("/proxy/8767");

    await client.getActionSettings();
    await client.selectActionConfirmationMode("confirm-risky");

    expect(fetch).toHaveBeenNthCalledWith(1, "/proxy/8767/settings/actions");
    expect(fetch).toHaveBeenNthCalledWith(
      2,
      "/proxy/8767/settings/actions/confirmation",
      expect.objectContaining({
        body: JSON.stringify({ mode: "confirm-risky" }),
        method: "PUT",
      }),
    );
  });

  it("reports model settings errors", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ error: "invalid profile" }), {
          status: 422,
        }),
      ),
    );

    await expect(new GatewayClient().getModelSettings()).rejects.toThrow(
      "invalid profile",
    );
  });

  it("manages Skill inspection and installation through settings routes", async () => {
    const skill = {
      name: "community-summary",
      skill_id: "example.community-summary",
      source: "candidate",
    };
    const settings = { skills: [{ ...skill, source: "installed" }] };
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ skills: [] })))
      .mockResolvedValueOnce(new Response(JSON.stringify(skill)))
      .mockResolvedValueOnce(new Response(JSON.stringify(settings)))
      .mockResolvedValueOnce(new Response(JSON.stringify({ skills: [] })));
    vi.stubGlobal("fetch", fetch);
    const client = new GatewayClient("/proxy/8767");

    await client.getSkillSettings();
    await client.inspectSkill("/mnt/community-summary");
    await client.installSkill("/mnt/community-summary");
    await client.removeSkill("community summary");

    expect(fetch).toHaveBeenNthCalledWith(1, "/proxy/8767/settings/skills");
    expect(fetch).toHaveBeenNthCalledWith(
      2,
      "/proxy/8767/settings/skills/inspect",
      expect.objectContaining({ method: "POST" }),
    );
    expect(fetch).toHaveBeenNthCalledWith(
      3,
      "/proxy/8767/settings/skills/install",
      expect.objectContaining({
        body: JSON.stringify({
          approved: true,
          source: "/mnt/community-summary",
        }),
      }),
    );
    expect(fetch).toHaveBeenNthCalledWith(
      4,
      "/proxy/8767/settings/skills/community%20summary",
      { method: "DELETE" },
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
    FakeEventSource.instances[0]?.emit(syntheticEvents().slice(3, 4));
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
    FakeEventSource.instances[0]?.emit(syntheticEvents().slice(3, 4));
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
    FakeWebSocket.instances[0]?.emit(syntheticEvents().slice(4, 5));
    cleanup();

    expect(received[0]?.[0]?.kind).toBe("agent_message.emitted");
    expect(FakeWebSocket.instances[0]?.close).toHaveBeenCalled();
  });
});
