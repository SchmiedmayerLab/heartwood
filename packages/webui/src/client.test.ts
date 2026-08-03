/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { GatewayClient, createCommand } from "./client";
import {
  emptyProjection,
  syntheticAction,
  syntheticEvents,
  syntheticProjection,
} from "./test/fixtures";
import type { SessionEvent, SessionProjection } from "./types";

const projectionResponse = (
  events: SessionEvent[] = [],
  projection: SessionProjection = syntheticProjection(),
) => ({ events, projection });

const setGatewayBase = (value: string): void => {
  const metadata = document.createElement("meta");
  metadata.name = "heartwood-gateway-base";
  metadata.content = value;
  document.head.append(metadata);
};

class FakeWebSocket {
  static instances: FakeWebSocket[] = [];

  onerror: (() => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  onmessage: ((message: MessageEvent<string>) => void) | null = null;
  onopen: (() => void) | null = null;
  readonly close = vi.fn();

  constructor(readonly url: string) {
    FakeWebSocket.instances.push(this);
  }

  fail(): void {
    this.onerror?.();
  }

  open(): void {
    this.onopen?.();
  }

  closeWith(code: number): void {
    this.onclose?.(new CloseEvent("close", { code }));
  }

  emit(projection: SessionProjection): void {
    this.emitRaw(JSON.stringify(projectionResponse([], projection)));
  }

  emitRaw(data: string): void {
    this.onmessage?.(
      new MessageEvent("message", {
        data,
      }),
    );
  }
}

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  static readonly CLOSED = 2;
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;

  private listener: ((message: MessageEvent<string>) => void) | null = null;
  onerror: (() => void) | null = null;
  onopen: (() => void) | null = null;
  readyState = FakeEventSource.CONNECTING;
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

  emit(projection: SessionProjection): void {
    this.emitRaw(JSON.stringify(projectionResponse([], projection)));
  }

  fail(closed = false): void {
    this.readyState =
      closed ? FakeEventSource.CLOSED : FakeEventSource.CONNECTING;
    this.onerror?.();
  }

  open(): void {
    this.readyState = FakeEventSource.OPEN;
    this.onopen?.();
  }

  emitRaw(data: string): void {
    this.listener?.(
      new MessageEvent("heartwood-session-events", {
        data,
      }),
    );
  }
}

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
  FakeWebSocket.instances = [];
  FakeEventSource.instances = [];
  document
    .querySelectorAll('meta[name="heartwood-gateway-base"]')
    .forEach((element) => element.remove());
  window.history.pushState({}, "", "/");
});

describe("createCommand", () => {
  it("builds the shared session command envelope", () => {
    const command = createCommand("session-test", "chat", {
      prompt: "synthetic",
    });

    expect(command).toMatchObject({
      actor_id: "human",
      kind: "chat",
      payload: { prompt: "synthetic" },
      schema_version: "heartwood.session-command.v1",
      session_id: "session-test",
    });
    expect(command.command_id).toMatch(/^session-test-chat-[0-9a-f]{32}$/);
  });
});

describe("GatewayClient", () => {
  it("ensures the shared first session through an idempotent operation", async () => {
    const session = {
      session_id: "session-main",
      title: "Main session",
      status: "empty",
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z",
      event_count: 0,
    };
    const fetch = vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify(session)));
    vi.stubGlobal("fetch", fetch);

    const ensured = await new GatewayClient(
      "/proxy/8767",
    ).ensureDefaultSession();

    expect(ensured).toEqual(session);
    expect(fetch).toHaveBeenCalledWith("/proxy/8767/sessions/default", {
      method: "POST",
    });
  });

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
    const fetch = vi
      .fn()
      .mockResolvedValue(
        new Response(
          JSON.stringify(projectionResponse(syntheticEvents().slice(0, 1))),
          { status: 200 },
        ),
      );
    vi.stubGlobal("fetch", fetch);

    const client = new GatewayClient("/proxy/8767");
    const response = await client.postCommand(
      createCommand("session-test", "pause"),
    );

    expect(response.events).toHaveLength(1);
    expect(fetch).toHaveBeenCalledWith(
      "/proxy/8767/sessions/session-test/commands",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("uses bounded workspace routes with encoded session and project paths", async () => {
    const limits = {
      max_tree_entries: 2_000,
      max_tree_depth: 8,
      max_file_bytes: 524_288,
      max_file_lines: 10_000,
      max_change_entries: 500,
      max_diff_bytes: 1_048_576,
    };
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            schema_version: "heartwood.workspace-tree.v1",
            path: ".",
            status: "available",
            entries: [],
            truncated: false,
            limits,
          }),
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            schema_version: "heartwood.workspace-file.v1",
            path: "results/cohort summary.py",
            status: "available",
            content: "print('synthetic')\n",
            size_bytes: 19,
            bytes_read: 19,
            line_count: 1,
            truncated: false,
            message: null,
          }),
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            schema_version: "heartwood.workspace-changes.v1",
            status: "available",
            source: "git",
            changes: [],
            truncated: false,
            message: null,
            limits,
          }),
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            schema_version: "heartwood.workspace-diff.v1",
            path: "results/cohort summary.py",
            status: "available",
            source: "git",
            original: "",
            modified: "print('synthetic')\n",
            truncated: false,
            message: null,
          }),
        ),
      );
    vi.stubGlobal("fetch", fetch);
    const client = new GatewayClient("/proxy/8767");

    await client.getWorkspaceTree("session test", ".", 4);
    await client.getWorkspaceFile("session test", "results/cohort summary.py");
    await client.getWorkspaceChanges("session test");
    await client.getWorkspaceDiff("session test", "results/cohort summary.py");

    expect(fetch).toHaveBeenNthCalledWith(
      1,
      "/proxy/8767/sessions/session%20test/workspace/tree?path=.&depth=4",
    );
    expect(fetch).toHaveBeenNthCalledWith(
      2,
      "/proxy/8767/sessions/session%20test/workspace/file?path=results%2Fcohort+summary.py",
    );
    expect(fetch).toHaveBeenNthCalledWith(
      3,
      "/proxy/8767/sessions/session%20test/workspace/changes",
    );
    expect(fetch).toHaveBeenNthCalledWith(
      4,
      "/proxy/8767/sessions/session%20test/workspace/diff?path=results%2Fcohort+summary.py",
    );
  });

  it("rejects session payloads that omit the gateway projection", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          new Response(JSON.stringify({ events: syntheticEvents() })),
        ),
    );

    await expect(
      new GatewayClient("/proxy/8767").replayEvents("session-test"),
    ).rejects.toThrow(
      "Gateway response included an invalid session projection",
    );
  });

  it("rejects malformed fields in a versioned gateway projection", async () => {
    const malformed = {
      ...syntheticProjection(),
      lifecycle: { status: "running" },
    };
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          new Response(
            JSON.stringify(projectionResponse([], malformed as never)),
          ),
        ),
    );

    await expect(
      new GatewayClient("/proxy/8767").replayEvents("session-test"),
    ).rejects.toThrow(
      "Gateway response included an invalid session projection",
    );
  });

  it("accepts fractional OpenHands terminal timeouts from the typed projection", async () => {
    const action = syntheticAction({
      details: {
        kind: "terminal",
        command: "python analysis.py",
        isInput: false,
        reset: false,
        timeout: 0.25,
      },
    });
    const projection = syntheticProjection({
      actions: [action],
      pendingApproval: {
        groupId: "action-set-session-test",
        actions: [action],
        decision: null,
        decisionScope: "all",
      },
    });
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          new Response(JSON.stringify(projectionResponse([], projection))),
        ),
    );

    await expect(
      new GatewayClient("/proxy/8767").replayEvents("session-test"),
    ).resolves.toEqual({ events: [], projection });
  });

  it("accepts projection fields named like JSON Schema annotations", async () => {
    const action = syntheticAction({
      details: {
        kind: "task",
        capability: "advisory",
        description: "Review the cohort summary",
        prompt: "Check the generated result.",
        roleLabel: "Research Reviewer",
        subagentType: "research-reviewer",
        resume: null,
      },
    });
    const projection = syntheticProjection({ actions: [action] });
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          new Response(JSON.stringify(projectionResponse([], projection))),
        ),
    );

    await expect(
      new GatewayClient("/proxy/8767").replayEvents("session-test"),
    ).resolves.toEqual({ events: [], projection });
  });

  it("enforces canonical numeric projection constraints at runtime", async () => {
    const malformed = {
      ...syntheticProjection(),
      usage: {
        usageId: "total",
        purposeLabel: "Total Model Activity",
        modelName: "synthetic-model",
        callCount: 1,
        promptTokens: 1,
        completionTokens: 1,
        cacheReadTokens: 0,
        cacheWriteTokens: 0,
        reasoningTokens: 0,
        contextWindow: -1,
        accumulatedCost: 0,
      },
    };
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          new Response(JSON.stringify(projectionResponse([], malformed))),
        ),
    );

    await expect(
      new GatewayClient("/proxy/8767").replayEvents("session-test"),
    ).rejects.toThrow(
      "Gateway response included an invalid session projection",
    );
  });

  it("reports malformed projection JSON with recovery guidance", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response("{not-json", { status: 200 })),
    );

    await expect(
      new GatewayClient("/proxy/8767").replayEvents("session-test"),
    ).rejects.toThrow(
      "Heartwood could not read the session update. Refresh the page to reconnect.",
    );
  });

  it("uses optional request defaults and an empty event response", async () => {
    const session = {
      session_id: "session-test",
      title: "Untitled session",
      status: "empty",
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z",
      event_count: 0,
    };
    const validation = {
      profile: {},
      credential_status: "configured",
      policy_decision: { decision: "allow" },
    };
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(new Response(JSON.stringify(session)))
      .mockResolvedValueOnce(new Response(JSON.stringify(validation)))
      .mockResolvedValueOnce(
        new Response(JSON.stringify(projectionResponse([], emptyProjection()))),
      );
    vi.stubGlobal("fetch", fetch);
    const client = new GatewayClient("/proxy/8767/");

    await client.createSession();
    await client.validateModelProfile();
    const response = await client.replayEvents("session-test");

    expect(fetch).toHaveBeenNthCalledWith(
      1,
      "/proxy/8767/sessions",
      expect.objectContaining({ body: "{}" }),
    );
    expect(fetch).toHaveBeenNthCalledWith(
      2,
      "/proxy/8767/settings/models/validation",
    );
    expect(response.events).toEqual([]);
  });

  it("manages non-secret model profiles through settings routes", async () => {
    const settings = {
      schema_version: "heartwood.model-settings.v1",
      active_profile: "heartwood",
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
      profile_id: "custom-loopback",
      model: "openai/custom-model",
      policy_endpoint: "http://127.0.0.1:8765/v1/chat/completions",
      capability_tier: "supervised" as const,
      base_url: "http://127.0.0.1:8765/v1",
      credential_kind: "none" as const,
      auth_type: "api_key" as const,
      subscription_vendor: null,
      api_key_env: null,
      api_key_file: null,
      api_version: null,
      aws_region_name: null,
      aws_profile_name: null,
      description: null,
    };

    await client.getModelSettings();
    await client.saveModelProfile(profile);
    await client.selectModelProfile("custom-loopback");
    await client.removeModelProfile("custom-loopback");
    await client.validateModelProfile("custom profile");

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
      "/proxy/8767/settings/models/profiles/custom-loopback",
      { method: "DELETE" },
    );
    expect(fetch).toHaveBeenNthCalledWith(
      5,
      "/proxy/8767/settings/models/validation?profile_id=custom%20profile",
    );
  });

  it("discovers and selects a model through the shared connection routes", async () => {
    const connection = {
      connection_id: "openai",
      label: "OpenAI",
      protocol: "openai",
      model_prefix: "openai/",
      source: "built-in",
      credential_kind: "environment",
      policy_endpoint: "https://api.openai.com/v1/chat/completions",
      catalog_endpoint: "https://api.openai.com/v1/models",
      base_url: null,
      api_key_env: "OPENAI_API_KEY",
      api_key_file: null,
      api_version: null,
      aws_region_name: null,
      aws_profile_name: null,
      description: "OpenAI models",
      static_models: [],
      subscription_vendor: null,
      group: "hosted-provider",
      group_label: "Hosted providers",
      accepts_token: true,
      supports_login: false,
      auth_type: "api_key",
      credential_status: "missing",
    };
    const catalog = {
      schema_version: "heartwood.model-catalog.v1",
      connection,
      models: [
        {
          model_id: "provider-coder",
          display_name: "provider-coder",
          execution_model: "openai/provider-coder",
          availability: "available",
          reason: "Verified by the pinned OpenHands SDK",
          context_window: 128_000,
          supports_tools: true,
        },
      ],
      refreshed_at: 1_783_683_200,
    };
    const settings = {
      schema_version: "heartwood.model-settings.v1",
      active_profile: "openai",
      profiles: [],
      connections: [connection],
      presets: [],
    };
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(new Response(JSON.stringify(catalog)))
      .mockResolvedValueOnce(new Response(JSON.stringify(settings)));
    vi.stubGlobal("fetch", fetch);
    const client = new GatewayClient("/proxy/8767");

    await client.discoverModels({
      connection_id: "openai",
      token: "runtime-only-token",
      refresh: true,
    });
    await client.connectModel({
      connection_id: "openai",
      model_id: "provider-coder",
      token: "runtime-only-token",
    });

    expect(fetch).toHaveBeenNthCalledWith(
      1,
      "/proxy/8767/settings/models/catalog",
      expect.objectContaining({
        body: JSON.stringify({
          connection_id: "openai",
          token: "runtime-only-token",
          refresh: true,
        }),
        method: "POST",
      }),
    );
    expect(fetch).toHaveBeenNthCalledWith(
      2,
      "/proxy/8767/settings/models/connect",
      expect.objectContaining({
        body: JSON.stringify({
          connection_id: "openai",
          model_id: "provider-coder",
          token: "runtime-only-token",
        }),
        method: "POST",
      }),
    );
  });

  it("starts and polls OpenHands device login without exposing credentials", async () => {
    const pending = {
      schema_version: "heartwood.subscription-login.v1",
      login_id: "login-1",
      connection_id: "openai-subscription",
      verification_url: "https://auth.openai.test/device",
      user_code: "TEST-CODE",
      poll_interval_seconds: 5,
      status: "pending",
    };
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(new Response(JSON.stringify(pending)))
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ ...pending, status: "complete" })),
      );
    vi.stubGlobal("fetch", fetch);
    const client = new GatewayClient("/proxy/8767");

    await client.startSubscriptionDeviceLogin("openai-subscription");
    await client.pollSubscriptionDeviceLogin("openai-subscription", "login-1");

    expect(fetch).toHaveBeenNthCalledWith(
      1,
      "/proxy/8767/settings/models/subscription/device",
      expect.objectContaining({
        body: JSON.stringify({
          connection_id: "openai-subscription",
          terms_accepted: true,
        }),
        method: "POST",
      }),
    );
    expect(fetch).toHaveBeenNthCalledWith(
      2,
      "/proxy/8767/settings/models/subscription/device/poll",
      expect.objectContaining({
        body: JSON.stringify({
          connection_id: "openai-subscription",
          login_id: "login-1",
        }),
        method: "POST",
      }),
    );
  });

  it("lists and starts recommended model downloads", async () => {
    const artifacts = {
      schema_version: "heartwood.local-model-catalog.v2",
      snapshot_schema_version: "heartwood.model-snapshot-catalog.v3",
      artifacts: [],
      snapshots: [],
      models: [],
      downloads: [],
    };
    const download = {
      model_id: "reviewed-model",
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
    await client.downloadLocalModel("reviewed-model");

    expect(fetch).toHaveBeenNthCalledWith(
      1,
      "/proxy/8767/settings/models/artifacts",
    );
    expect(fetch).toHaveBeenNthCalledWith(
      2,
      "/proxy/8767/settings/models/downloads",
      expect.objectContaining({
        body: JSON.stringify({ model_id: "reviewed-model" }),
        method: "POST",
      }),
    );
  });

  it("plans and starts a gateway-selected Hugging Face model download", async () => {
    const plan = {
      model: {
        model_id: "hf-model-123456789abc",
        label: "Model Q4_K_M",
        purpose: "User-selected model",
        runtime: "llama-cpp",
        source_repository: "example/model-gguf",
        source_revision: "1".repeat(40),
        source_path: "model-q4_k_m.gguf",
        size_bytes: 1024,
        minimum_free_bytes: 1024,
        license_posture: "Review source terms",
        catalog_source: "user-selected",
        context_window: 32_768,
        artifact_sha256: "a".repeat(64),
        minimum_resource_envelope: "Estimated minimum",
        recommended_resource_envelope: "Recommended resources",
        active: false,
        available: true,
        selected: false,
        availability_reason: "Available on this deployment",
      },
      selection_reason: "Selected a balanced GGUF model.",
    };
    const download = {
      model_id: plan.model.model_id,
      status: "downloading",
      bytes_downloaded: 0,
      bytes_total: 1024,
      path: null,
      error: null,
    };
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(new Response(JSON.stringify(plan)))
      .mockResolvedValueOnce(new Response(JSON.stringify(download)));
    vi.stubGlobal("fetch", fetch);
    const client = new GatewayClient("/proxy/8767");

    await client.inspectModelRepository({ repository: "example/model-gguf" });
    await client.downloadCustomLocalModel({
      repository: "example/model-gguf",
      revision: "1".repeat(40),
    });

    expect(fetch).toHaveBeenNthCalledWith(
      1,
      "/proxy/8767/settings/models/repository",
      expect.objectContaining({
        body: JSON.stringify({ repository: "example/model-gguf" }),
        method: "POST",
      }),
    );
    expect(fetch).toHaveBeenNthCalledWith(
      2,
      "/proxy/8767/settings/models/downloads/custom",
      expect.objectContaining({
        body: JSON.stringify({
          repository: "example/model-gguf",
          revision: "1".repeat(40),
        }),
        method: "POST",
      }),
    );
  });

  it("reads project readiness and prepares a shared model source", async () => {
    const readiness = {
      state: "setup-required",
      platform_id: "generic",
      project_root: "/projects/analysis",
      state_root: "/projects/analysis/.heartwood",
      evidence: [],
      checks: [],
    };
    const settings = {
      schema_version: "heartwood.model-settings.v1",
      active_profile: null,
      model_source: "stanford-ai-api-gateway",
      profiles: [],
      connections: [],
      presets: [],
      source_options: [],
    };
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(new Response(JSON.stringify(readiness)))
      .mockResolvedValueOnce(new Response(JSON.stringify(settings)));
    vi.stubGlobal("fetch", fetch);
    const client = new GatewayClient("/proxy/8767");

    await client.getProjectReadiness();
    await client.configureModelSource("stanford-ai-api-gateway");

    expect(fetch).toHaveBeenNthCalledWith(1, "/proxy/8767/project/readiness");
    expect(fetch).toHaveBeenNthCalledWith(
      2,
      "/proxy/8767/settings/models/source",
      expect.objectContaining({
        body: JSON.stringify({ source_id: "stanford-ai-api-gateway" }),
        method: "PUT",
      }),
    );
  });

  it("selects the shared action-confirmation mode", async () => {
    const actions = {
      schema_version: "heartwood.action-settings.v1",
      confirmation_mode: "always-confirm",
      scope_description:
        "Shared by every Heartwood interface in this project and applied to future action sets.",
      presentation: {
        risk_labels: {},
        state_labels: {},
        tool_labels: {},
        other_tool_label_template: "{tool_name} Action",
        unknown_risk_label: "Not Classified",
        unknown_tool_label: "Tool Action",
      },
      change_allowed: true,
      change_blocked_reason: null,
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

  it("loads the shared research-specialist catalog", async () => {
    const settings = { specialists: [] };
    const fetch = vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify(settings)));
    vi.stubGlobal("fetch", fetch);

    await expect(
      new GatewayClient("/proxy/8767").getSpecialistSettings(),
    ).resolves.toEqual(settings);
    expect(fetch).toHaveBeenCalledWith("/proxy/8767/settings/specialists");
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

  it("preserves gateway status when JSON errors omit a message", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({}), { status: 409 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({}), { status: 503 }));
    vi.stubGlobal("fetch", fetch);
    const client = new GatewayClient();

    await expect(client.replayEvents("session-test")).rejects.toThrow(
      "Gateway request failed with status 409",
    );
    await expect(client.getModelSettings()).rejects.toThrow(
      "Gateway request failed with status 503",
    );
  });

  it("uses the gateway-owned Jupyter proxy base path", async () => {
    setGatewayBase("/user/synthetic/proxy/8767");
    const fetch = vi
      .fn()
      .mockResolvedValue(
        new Response(
          JSON.stringify(projectionResponse(syntheticEvents().slice(0, 1))),
          { status: 200 },
        ),
      );
    vi.stubGlobal("fetch", fetch);

    const client = new GatewayClient();
    await client.replayEvents("session-test", 0);

    expect(fetch).toHaveBeenCalledWith(
      "/user/synthetic/proxy/8767/sessions/session-test/events?after=0",
    );
  });

  it("preserves the complete gateway-owned Terra proxy base path", async () => {
    setGatewayBase("/proxy/terra-project/saturn-runtime/jupyter/proxy/8767");
    const fetch = vi
      .fn()
      .mockResolvedValue(
        new Response(
          JSON.stringify(projectionResponse(syntheticEvents().slice(0, 1))),
          { status: 200 },
        ),
      );
    vi.stubGlobal("fetch", fetch);

    const client = new GatewayClient();
    await client.replayEvents("session-test", 0);

    expect(fetch).toHaveBeenCalledWith(
      "/proxy/terra-project/saturn-runtime/jupyter/proxy/8767/sessions/session-test/events?after=0",
    );
  });

  it("uses the configured gateway base path when one is provided", async () => {
    vi.stubEnv("VITE_HEARTWOOD_GATEWAY_BASE", "/configured-gateway");
    const fetch = vi
      .fn()
      .mockResolvedValue(
        new Response(
          JSON.stringify(projectionResponse([], emptyProjection())),
          { status: 200 },
        ),
      );
    vi.stubGlobal("fetch", fetch);

    await new GatewayClient().replayEvents("session-test");

    expect(fetch).toHaveBeenCalledWith(
      "/configured-gateway/sessions/session-test/events",
    );
  });

  it("falls back to server-sent events after a WebSocket error", () => {
    vi.stubGlobal("WebSocket", FakeWebSocket);
    vi.stubGlobal("EventSource", FakeEventSource);
    const received: SessionProjection[] = [];
    const client = new GatewayClient("/proxy/8767");

    const cleanup = client.streamSession("session-test", 2, {
      onProjection: (projection) => received.push(projection),
    });
    FakeWebSocket.instances[0]?.fail();
    FakeEventSource.instances[0]?.emit(
      syntheticProjection({ streamingText: "First streamed response" }),
    );
    cleanup();

    expect(FakeWebSocket.instances[0]?.url).toContain(
      "/proxy/8767/sessions/session-test/events?after=2",
    );
    expect(FakeEventSource.instances[0]?.url).toBe(
      "/proxy/8767/sessions/session-test/events/stream?after=2",
    );
    expect(received[0]?.streamingText).toBe("First streamed response");
    expect(FakeEventSource.instances[0]?.close).toHaveBeenCalled();
  });

  it("reports connection recovery without treating fallback as command failure", () => {
    vi.stubGlobal("WebSocket", FakeWebSocket);
    vi.stubGlobal("EventSource", FakeEventSource);
    const states: string[] = [];
    const onError = vi.fn();

    const cleanup = new GatewayClient().streamSession(
      "session-test",
      undefined,
      {
        onError,
        onProjection: vi.fn(),
        onState: (state) => states.push(state),
      },
    );
    FakeWebSocket.instances[0]?.open();
    FakeWebSocket.instances[0]?.fail();
    FakeEventSource.instances[0]?.open();
    cleanup();

    expect(states).toEqual([
      "connecting",
      "connected",
      "reconnecting",
      "connected",
    ]);
    expect(onError).not.toHaveBeenCalled();
  });

  it("distinguishes an EventSource retry from a closed live-update stream", () => {
    const websocketDescriptor = Object.getOwnPropertyDescriptor(
      globalThis,
      "WebSocket",
    );
    Reflect.deleteProperty(globalThis, "WebSocket");
    vi.stubGlobal("EventSource", FakeEventSource);
    const states: string[] = [];
    const errors: Error[] = [];

    try {
      const cleanup = new GatewayClient().streamSession(
        "session-test",
        undefined,
        {
          onError: (error) => errors.push(error),
          onProjection: vi.fn(),
          onState: (state) => states.push(state),
        },
      );
      FakeEventSource.instances[0]?.fail();
      FakeEventSource.instances[0]?.open();
      FakeEventSource.instances[0]?.fail(true);
      cleanup();

      expect(states).toEqual([
        "connecting",
        "reconnecting",
        "connected",
        "degraded",
      ]);
      expect(errors.map((error) => error.message)).toEqual([
        expect.stringContaining("stopped"),
      ]);
    } finally {
      if (websocketDescriptor !== undefined) {
        Object.defineProperty(globalThis, "WebSocket", websocketDescriptor);
      }
    }
  });

  it("falls back to server-sent events after an abnormal WebSocket close", () => {
    vi.stubGlobal("WebSocket", FakeWebSocket);
    vi.stubGlobal("EventSource", FakeEventSource);
    const received: SessionProjection[] = [];
    const client = new GatewayClient("/proxy/8767");

    const cleanup = client.streamSession("session-test", 2, {
      onProjection: (projection) => received.push(projection),
    });
    FakeWebSocket.instances[0]?.closeWith(1011);
    FakeEventSource.instances[0]?.emit(syntheticProjection({ revision: 6 }));
    cleanup();

    expect(FakeEventSource.instances[0]?.url).toBe(
      "/proxy/8767/sessions/session-test/events/stream?after=2",
    );
    expect(received[0]?.revision).toBe(6);
  });

  it("recovers through server-sent events after a graceful server close", () => {
    vi.stubGlobal("WebSocket", FakeWebSocket);
    vi.stubGlobal("EventSource", FakeEventSource);
    const states: string[] = [];
    const client = new GatewayClient("/proxy/8767");

    const cleanup = client.streamSession("session-test", 2, {
      onProjection: vi.fn(),
      onState: (state) => states.push(state),
    });
    FakeWebSocket.instances[0]?.open();
    FakeWebSocket.instances[0]?.closeWith(1000);

    expect(FakeEventSource.instances).toHaveLength(1);
    expect(states).toEqual(["connecting", "connected", "reconnecting"]);
    cleanup();
  });

  it("opens the fallback only once for repeated WebSocket failures", () => {
    vi.stubGlobal("WebSocket", FakeWebSocket);
    vi.stubGlobal("EventSource", FakeEventSource);
    const client = new GatewayClient("/proxy/8767");

    const cleanup = client.streamSession("session-test", undefined, {
      onProjection: vi.fn(),
    });
    FakeWebSocket.instances[0]?.closeWith(1011);
    FakeWebSocket.instances[0]?.fail();
    FakeWebSocket.instances[0]?.closeWith(1000);
    cleanup();

    expect(FakeEventSource.instances).toHaveLength(1);
  });

  it("streams complete projections over server-sent events without WebSocket support", () => {
    const websocketDescriptor = Object.getOwnPropertyDescriptor(
      globalThis,
      "WebSocket",
    );
    Reflect.deleteProperty(globalThis, "WebSocket");
    vi.stubGlobal("EventSource", FakeEventSource);
    const received: SessionProjection[] = [];

    try {
      const client = new GatewayClient("/proxy/8767");
      const cleanup = client.streamSession("session-test", undefined, {
        onProjection: (projection) => received.push(projection),
      });
      FakeEventSource.instances[0]?.emit(emptyProjection());
      cleanup();

      expect(FakeEventSource.instances[0]?.url).toBe(
        "/proxy/8767/sessions/session-test/events/stream",
      );
      expect(received).toEqual([emptyProjection()]);
    } finally {
      if (websocketDescriptor !== undefined) {
        Object.defineProperty(globalThis, "WebSocket", websocketDescriptor);
      }
    }
  });

  it("returns a no-op cleanup when browser streaming is unavailable", () => {
    const websocketDescriptor = Object.getOwnPropertyDescriptor(
      globalThis,
      "WebSocket",
    );
    const eventSourceDescriptor = Object.getOwnPropertyDescriptor(
      globalThis,
      "EventSource",
    );
    Reflect.deleteProperty(globalThis, "WebSocket");
    Reflect.deleteProperty(globalThis, "EventSource");

    const onError = vi.fn();
    const onState = vi.fn();
    try {
      const cleanup = new GatewayClient().streamSession(
        "session-test",
        undefined,
        { onError, onProjection: vi.fn(), onState },
      );
      expect(cleanup).not.toThrow();
      expect(onState).toHaveBeenLastCalledWith("degraded");
      expect(onError).toHaveBeenCalledOnce();
    } finally {
      if (websocketDescriptor !== undefined) {
        Object.defineProperty(globalThis, "WebSocket", websocketDescriptor);
      }
      if (eventSourceDescriptor !== undefined) {
        Object.defineProperty(globalThis, "EventSource", eventSourceDescriptor);
      }
    }
  });

  it("streams complete projections over WebSocket when the upgrade succeeds", () => {
    vi.stubGlobal("WebSocket", FakeWebSocket);
    const received: SessionProjection[] = [];
    const client = new GatewayClient();

    const cleanup = client.streamSession("session-test", undefined, {
      onProjection: (projection) => received.push(projection),
    });
    FakeWebSocket.instances[0]?.emit(
      syntheticProjection({ streamingText: "Working" }),
    );
    cleanup();

    expect(received[0]?.streamingText).toBe("Working");
    expect(FakeWebSocket.instances[0]?.close).toHaveBeenCalled();
  });

  it("reports malformed WebSocket projections and falls back to SSE", () => {
    vi.stubGlobal("WebSocket", FakeWebSocket);
    vi.stubGlobal("EventSource", FakeEventSource);
    const onError = vi.fn();
    const client = new GatewayClient();

    const cleanup = client.streamSession("session-test", undefined, {
      onError,
      onProjection: vi.fn(),
    });
    FakeWebSocket.instances[0]?.emitRaw(
      JSON.stringify({ projection: emptyProjection() }),
    );
    cleanup();

    expect(onError).toHaveBeenCalledOnce();
    expect(FakeWebSocket.instances[0]?.close).toHaveBeenCalledWith(
      1002,
      "invalid Heartwood projection",
    );
    expect(FakeEventSource.instances).toHaveLength(1);
  });

  it("does not misclassify a WebSocket projection consumer failure", () => {
    vi.stubGlobal("WebSocket", FakeWebSocket);
    vi.stubGlobal("EventSource", FakeEventSource);
    const consumerFailure = new Error("Synthetic projection consumer failure");
    const onError = vi.fn();
    const client = new GatewayClient();

    const cleanup = client.streamSession("session-test", undefined, {
      onError,
      onProjection: () => {
        throw consumerFailure;
      },
    });

    expect(() =>
      FakeWebSocket.instances[0]?.emit(syntheticProjection()),
    ).toThrow(consumerFailure);
    expect(onError).not.toHaveBeenCalled();
    expect(FakeWebSocket.instances[0]?.close).not.toHaveBeenCalled();
    expect(FakeEventSource.instances).toHaveLength(0);
    cleanup();
  });

  it("reports malformed streamed JSON with recovery guidance", () => {
    vi.stubGlobal("WebSocket", FakeWebSocket);
    vi.stubGlobal("EventSource", FakeEventSource);
    const onError = vi.fn();
    const client = new GatewayClient();

    const cleanup = client.streamSession("session-test", undefined, {
      onError,
      onProjection: vi.fn(),
    });
    FakeWebSocket.instances[0]?.emitRaw("{not-json");
    cleanup();

    expect(onError).toHaveBeenCalledWith(
      expect.objectContaining({
        message:
          "Heartwood could not read the session update. Refresh the page to reconnect.",
      }),
    );
    expect(FakeEventSource.instances).toHaveLength(1);
  });

  it("reports malformed SSE projections and closes the stale stream", () => {
    const websocketDescriptor = Object.getOwnPropertyDescriptor(
      globalThis,
      "WebSocket",
    );
    Reflect.deleteProperty(globalThis, "WebSocket");
    vi.stubGlobal("EventSource", FakeEventSource);
    const onError = vi.fn();

    try {
      new GatewayClient().streamSession("session-test", undefined, {
        onError,
        onProjection: vi.fn(),
      });
      FakeEventSource.instances[0]?.emitRaw(
        JSON.stringify({ projection: emptyProjection() }),
      );

      expect(onError).toHaveBeenCalledOnce();
      expect(FakeEventSource.instances[0]?.close).toHaveBeenCalled();
    } finally {
      if (websocketDescriptor !== undefined) {
        Object.defineProperty(globalThis, "WebSocket", websocketDescriptor);
      }
    }
  });

  it("does not misclassify an SSE projection consumer failure", () => {
    const websocketDescriptor = Object.getOwnPropertyDescriptor(
      globalThis,
      "WebSocket",
    );
    Reflect.deleteProperty(globalThis, "WebSocket");
    vi.stubGlobal("EventSource", FakeEventSource);
    const consumerFailure = new Error("Synthetic projection consumer failure");
    const onError = vi.fn();

    try {
      const cleanup = new GatewayClient().streamSession(
        "session-test",
        undefined,
        {
          onError,
          onProjection: () => {
            throw consumerFailure;
          },
        },
      );

      expect(() =>
        FakeEventSource.instances[0]?.emit(syntheticProjection()),
      ).toThrow(consumerFailure);
      expect(onError).not.toHaveBeenCalled();
      expect(FakeEventSource.instances[0]?.close).not.toHaveBeenCalled();
      cleanup();
    } finally {
      if (websocketDescriptor !== undefined) {
        Object.defineProperty(globalThis, "WebSocket", websocketDescriptor);
      }
    }
  });
});
