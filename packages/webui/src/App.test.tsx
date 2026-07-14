/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { App } from "./App";
import type { HeartwoodClient, SessionEventResponse } from "./client";
import { event, syntheticEvents } from "./test/fixtures";
import type {
  ActionConfirmationMode,
  ActionSettings,
  AuditExport,
  CustomLocalModelDownloadRequest,
  LocalModelChoice,
  ModelArtifacts,
  ModelCatalog,
  ModelCatalogRequest,
  ModelConnectRequest,
  ModelConnection,
  ModelDownload,
  ModelProfile,
  ModelRepositoryPlan,
  ModelRepositoryRequest,
  ModelSource,
  ModelSettings,
  ModelValidation,
  ProjectReadiness,
  SessionCommand,
  SessionEvent,
  SessionList,
  SessionSummary,
  SkillSettings,
  SkillSummary,
} from "./types";

const settings = (): ModelSettings => ({
  schema_version: "heartwood.model-settings.v1",
  active_profile: null,
  model_source: null,
  profiles: [],
  connections: [
    modelConnection("local", "Local", "built-in", "configured", false),
    modelConnection(
      "research-ai",
      "Research AI Service",
      "platform",
      "configured",
      false,
    ),
    modelConnection("openai", "OpenAI", "built-in", "missing", true),
    modelConnection("anthropic", "Anthropic", "built-in", "missing", true),
    modelConnection("custom-api", "Custom API", "user", "missing", true),
  ],
  presets: [
    {
      preset_id: "local-openai-compatible",
      label: "Local OpenAI-Compatible",
      model_prefix: "openai/",
      credential_kind: "none",
      api_key_env: null,
      base_url: "http://127.0.0.1:8765/v1",
      policy_endpoint: "http://127.0.0.1:8765/v1/chat/completions",
      description: "Local runtime",
    },
  ],
  source_options: [
    modelSource("local", "local", "On this device"),
    modelSource("openai", "openai", "OpenAI"),
    modelSource("anthropic", "anthropic", "Anthropic"),
    modelSource(
      "stanford-ai-api-gateway",
      "stanford-ai-api-gateway",
      "Stanford AI API Gateway",
    ),
  ],
});

const actions = (): ActionSettings => ({
  schema_version: "heartwood.action-settings.v1",
  confirmation_mode: "always-confirm",
  modes: [
    { mode: "always-confirm", label: "Ask Every Time", allowed: true },
    {
      mode: "confirm-risky",
      label: "Auto-Approve Low Risk",
      allowed: true,
    },
  ],
});

class FakeClient implements HeartwoodClient {
  commands: SessionCommand[] = [];
  auditExportCalls = 0;
  listCalls = 0;
  replayCalls = 0;
  artifactCalls = 0;
  artifactFailures = 0;
  savedProfile: ModelProfile | null = null;
  catalogRequest: ModelCatalogRequest | null = null;
  catalogError: Error | null = null;
  validationError: Error | null = null;
  modelConnectionRequest: ModelConnectRequest | null = null;
  currentSettings = settings();
  currentActions = actions();
  currentReadiness = projectReadiness();
  currentDownloads: ModelDownload[] = [];
  downloadedArtifact: string | null = null;
  customDownloadRequest: CustomLocalModelDownloadRequest | null = null;
  inspectedRepository: ModelRepositoryRequest | null = null;
  repositoryError: Error | null = null;
  customModel: LocalModelChoice | null = null;
  installedSkill: string | null = null;
  currentSessions: SessionSummary[] = [sessionSummary("session-test")];
  streamListener: ((events: SessionEvent[]) => void) | null = null;

  getProjectReadiness(): Promise<ProjectReadiness> {
    return Promise.resolve(this.currentReadiness);
  }

  listSessions(): Promise<SessionList> {
    this.listCalls += 1;
    return Promise.resolve({ sessions: this.currentSessions });
  }

  createSession(): Promise<SessionSummary> {
    const created = sessionSummary(
      `session-${this.currentSessions.length + 1}`,
      "Untitled session",
    );
    this.currentSessions = [created, ...this.currentSessions];
    return Promise.resolve(created);
  }

  getSession(sessionId: string): Promise<SessionSummary> {
    const existing = this.currentSessions.find(
      (session) => session.session_id === sessionId,
    );
    if (existing) return Promise.resolve(existing);
    const created = sessionSummary(sessionId);
    this.currentSessions = [created, ...this.currentSessions];
    return Promise.resolve(created);
  }

  renameSession(sessionId: string, title: string): Promise<SessionSummary> {
    const updated = {
      ...(this.currentSessions.find(
        (session) => session.session_id === sessionId,
      ) ?? sessionSummary(sessionId)),
      title,
    };
    this.currentSessions = this.currentSessions.map((session) =>
      session.session_id === sessionId ? updated : session,
    );
    return Promise.resolve(updated);
  }

  getAuditExport(sessionId: string): Promise<AuditExport> {
    this.auditExportCalls += 1;
    return Promise.resolve({
      filename: `${sessionId}-audit.jsonl`,
      content: '{"kind":"audit.export.recorded"}\n',
    });
  }

  postCommand(command: SessionCommand): Promise<SessionEventResponse> {
    this.commands.push(command);
    return Promise.resolve({
      events:
        command.kind === "detect" ? syntheticEvents().slice(0, 2)
        : command.kind === "chat" || command.kind === "run" ?
          [
            event(0, "user_message.recorded", {
              actor_id: command.actor_id,
              command_id: command.command_id,
              content:
                typeof command.payload.prompt === "string" ?
                  command.payload.prompt
                : "",
            }),
          ]
        : [],
    });
  }

  replayEvents(): Promise<SessionEventResponse> {
    this.replayCalls += 1;
    return Promise.resolve({ events: [] });
  }

  streamEvents(
    _sessionId: string,
    _afterSequence: number | undefined,
    onEvents: (events: SessionEvent[]) => void,
  ): () => void {
    this.streamListener = onEvents;
    return () => {
      if (this.streamListener === onEvents) this.streamListener = null;
    };
  }

  emitStream(events: SessionEvent[]): void {
    this.streamListener?.(events);
  }

  getModelSettings(): Promise<ModelSettings> {
    return Promise.resolve(this.currentSettings);
  }

  configureModelSource(sourceId: ModelSource): Promise<ModelSettings> {
    const source = this.currentSettings.source_options.find(
      (option) => option.source_id === sourceId,
    );
    if (!source) return Promise.reject(new Error("unknown source"));
    const sourceChanged = this.currentSettings.model_source !== sourceId;
    const connections =
      (
        this.currentSettings.connections.some(
          (connection) => connection.connection_id === source.connection_id,
        )
      ) ?
        this.currentSettings.connections
      : [
          ...this.currentSettings.connections,
          modelConnection(
            source.connection_id,
            source.label,
            "platform",
            "missing",
            true,
          ),
        ];
    this.currentSettings = {
      ...this.currentSettings,
      active_profile:
        sourceChanged ? null : this.currentSettings.active_profile,
      connections,
      model_source: sourceId,
      source_options: this.currentSettings.source_options.map((option) => ({
        ...option,
        selected: option.source_id === sourceId,
      })),
    };
    if (sourceChanged)
      this.currentReadiness = projectReadiness("setup-required");
    return Promise.resolve(this.currentSettings);
  }

  discoverModels(request: ModelCatalogRequest): Promise<ModelCatalog> {
    this.catalogRequest = request;
    if (this.catalogError) return Promise.reject(this.catalogError);
    const connection = this.currentSettings.connections.find(
      (candidate) => candidate.connection_id === request.connection_id,
    );
    if (!connection) return Promise.reject(new Error("unknown connection"));
    return Promise.resolve({
      schema_version: "heartwood.model-catalog.v1",
      connection: { ...connection, credential_status: "available" },
      models: [
        {
          model_id: "provider-coder",
          display_name: "Provider Coder",
          execution_model:
            connection.connection_id === "research-ai" ?
              "litellm_proxy/provider-coder"
            : "openai/provider-coder",
          availability: "available",
          reason: "Verified by the pinned OpenHands SDK",
          context_window: 128_000,
          supports_tools: true,
        },
        {
          model_id: "provider-experimental",
          display_name: "Provider Experimental",
          execution_model: "openai/provider-experimental",
          availability: "experimental",
          reason: "Not verified by the pinned OpenHands SDK",
          context_window: null,
          supports_tools: null,
        },
      ],
      refreshed_at: 1_783_683_200,
    });
  }

  connectModel(request: ModelConnectRequest): Promise<ModelSettings> {
    this.modelConnectionRequest = request;
    const connection = this.currentSettings.connections.find(
      (candidate) => candidate.connection_id === request.connection_id,
    );
    if (!connection) return Promise.reject(new Error("unknown connection"));
    const profile: ModelProfile = {
      ...localProfile(),
      profile_id: connection.connection_id,
      model:
        connection.connection_id === "research-ai" ?
          `litellm_proxy/${request.model_id}`
        : `openai/${request.model_id}`,
      credential_kind: connection.credential_kind,
      api_key_env: connection.api_key_env,
    };
    this.currentSettings = {
      ...this.currentSettings,
      active_profile: profile.profile_id,
      model_source: connection.connection_id,
      profiles: [profile],
    };
    this.currentReadiness = projectReadiness("ready");
    return Promise.resolve(this.currentSettings);
  }

  getActionSettings(): Promise<ActionSettings> {
    return Promise.resolve(this.currentActions);
  }

  selectActionConfirmationMode(
    mode: ActionConfirmationMode,
  ): Promise<ActionSettings> {
    this.currentActions = { ...this.currentActions, confirmation_mode: mode };
    return Promise.resolve(this.currentActions);
  }

  getModelArtifacts(): Promise<ModelArtifacts> {
    this.artifactCalls += 1;
    if (this.artifactFailures > 0) {
      this.artifactFailures -= 1;
      return Promise.reject(new Error("temporary model status failure"));
    }
    if (
      this.downloadedArtifact !== null &&
      this.currentDownloads[0]?.status === "downloading" &&
      this.artifactCalls > 1
    ) {
      this.currentDownloads = [
        {
          ...this.currentDownloads[0],
          status: "ready",
          bytes_downloaded: 256 * 1024 * 1024,
          path: "/models/stories260k/model.gguf",
        },
      ];
      this.currentSettings = {
        ...this.currentSettings,
        active_profile: "local",
        model_source: "local",
        profiles: [
          {
            ...localProfile(),
            model: "openai/heartwood-local-model",
            description: "Heartwood-managed local model",
          },
        ],
        source_options: this.currentSettings.source_options.map((source) => ({
          ...source,
          selected: source.source_id === "local",
        })),
      };
    }
    return Promise.resolve({
      schema_version: "heartwood.local-model-catalog.v1",
      snapshot_schema_version: "heartwood.model-snapshot-catalog.v1",
      artifacts: [
        {
          artifact_id: "stories260k",
          runtime_profile: "llama-cpp-cpu",
          purpose: "Synthetic smoke-test model.",
          source_repository: "example/stories260k",
          source_path: "model.gguf",
          source_revision: "0123456789abcdef",
          artifact_format: "GGUF",
          artifact_size_bytes: 256 * 1024 * 1024,
          artifact_sha256: "a".repeat(64),
          license_posture: "Test fixture",
          model_alias: "Stories 260K",
          minimum_resource_envelope: null,
          recommended_resource_envelope: null,
          recommended: true,
        },
      ],
      snapshots: [],
      models: [
        {
          model_id: "stories260k",
          label: "Stories 260K",
          purpose: "Synthetic smoke-test model.",
          runtime: "llama-cpp",
          source_repository: "example/stories260k",
          source_revision: "0".repeat(40),
          source_path: "model.gguf",
          size_bytes: 256 * 1024 * 1024,
          minimum_free_bytes: 256 * 1024 * 1024,
          license_posture: "Test fixture",
          catalog_source: "recommended",
          artifact_sha256: "a".repeat(64),
          minimum_resource_envelope: "Minimum: 4 CPU cores and 8 GB RAM.",
          recommended_resource_envelope:
            "Recommended: 8 CPU cores and 16 GB RAM.",
          available: true,
          availability_reason: "Available on this deployment",
        },
        ...(this.customModel === null ? [] : [this.customModel]),
      ],
      downloads: this.currentDownloads,
    });
  }

  inspectModelRepository(
    request: ModelRepositoryRequest,
  ): Promise<ModelRepositoryPlan> {
    this.inspectedRepository = request;
    if (this.repositoryError) return Promise.reject(this.repositoryError);
    const candidate: LocalModelChoice = {
      model_id: "hf-research-model-123456789abc",
      label: "Research Model Q4_K_M",
      purpose: "User-selected Hugging Face model.",
      runtime: "llama-cpp",
      source_repository: request.repository,
      source_revision: "1".repeat(40),
      source_path: "research-model-q4_k_m.gguf",
      size_bytes: 4 * 1024 * 1024 * 1024,
      minimum_free_bytes: 4 * 1024 * 1024 * 1024,
      license_posture: "Source model card reports apache-2.0.",
      catalog_source: "user-selected",
      artifact_sha256: "b".repeat(64),
      minimum_resource_envelope:
        "Estimated minimum: 4 CPU cores and 12 GB RAM.",
      recommended_resource_envelope: "Recommended: 8 CPU cores and 16 GB RAM.",
      available: true,
      availability_reason: "Available on this deployment",
    };
    return Promise.resolve({
      model: candidate,
      selection_reason: "Selected a balanced GGUF model for the CPU runtime.",
    });
  }

  downloadLocalModel(modelId: string): Promise<ModelDownload> {
    this.downloadedArtifact = modelId;
    const download: ModelDownload = {
      model_id: modelId,
      status: "downloading",
      bytes_downloaded: 64 * 1024 * 1024,
      bytes_total: 256 * 1024 * 1024,
      path: null,
      error: null,
    };
    this.currentDownloads = [download];
    this.currentReadiness = projectReadiness("compute-required");
    return Promise.resolve(download);
  }

  downloadCustomLocalModel(
    request: CustomLocalModelDownloadRequest,
  ): Promise<ModelDownload> {
    this.customDownloadRequest = request;
    this.customModel = {
      model_id: "hf-research-model-123456789abc",
      label: "Research Model Q4_K_M",
      purpose: "User-selected Hugging Face model.",
      runtime: "llama-cpp",
      source_repository: request.repository,
      source_revision: request.revision ?? "1".repeat(40),
      source_path: "research-model-q4_k_m.gguf",
      size_bytes: 4 * 1024 * 1024 * 1024,
      minimum_free_bytes: 4 * 1024 * 1024 * 1024,
      license_posture: "Source model card reports apache-2.0.",
      catalog_source: "user-selected",
      artifact_sha256: "b".repeat(64),
      minimum_resource_envelope:
        "Estimated minimum: 4 CPU cores and 12 GB RAM.",
      recommended_resource_envelope: "Recommended: 8 CPU cores and 16 GB RAM.",
      available: true,
      availability_reason: "Available on this deployment",
    };
    const download: ModelDownload = {
      model_id: this.customModel.model_id,
      status: "downloading",
      bytes_downloaded: 0,
      bytes_total: this.customModel.size_bytes,
      path: null,
      error: null,
    };
    this.currentDownloads = [download];
    return Promise.resolve(download);
  }

  getSkillSettings(): Promise<SkillSettings> {
    return Promise.resolve({ skills: [bundledSkill()] });
  }

  inspectSkill(source: string): Promise<SkillSummary> {
    return Promise.resolve({
      ...bundledSkill(),
      name: "community-summary",
      source: "candidate",
      approval_summary: `Reads mounted source ${source}`,
    });
  }

  installSkill(source: string): Promise<SkillSettings> {
    this.installedSkill = source;
    return Promise.resolve({
      skills: [
        bundledSkill(),
        { ...bundledSkill(), name: "community-summary", source: "installed" },
      ],
    });
  }

  removeSkill(name: string): Promise<SkillSettings> {
    this.installedSkill = `removed:${name}`;
    return Promise.resolve({ skills: [bundledSkill()] });
  }

  saveModelProfile(profile: ModelProfile): Promise<ModelSettings> {
    this.savedProfile = profile;
    this.currentSettings = {
      ...this.currentSettings,
      profiles: [profile],
    };
    return Promise.resolve(this.currentSettings);
  }

  selectModelProfile(profileId: string): Promise<ModelSettings> {
    this.currentSettings = {
      ...this.currentSettings,
      active_profile: profileId,
    };
    return Promise.resolve(this.currentSettings);
  }

  removeModelProfile(profileId: string): Promise<ModelSettings> {
    this.currentSettings = {
      ...this.currentSettings,
      active_profile:
        this.currentSettings.active_profile === profileId ?
          null
        : this.currentSettings.active_profile,
      profiles: this.currentSettings.profiles.filter(
        (profile) => profile.profile_id !== profileId,
      ),
    };
    return Promise.resolve(this.currentSettings);
  }

  validateModelProfile(): Promise<ModelValidation> {
    if (this.validationError) return Promise.reject(this.validationError);
    return Promise.resolve({
      profile: this.currentSettings.profiles[0] ?? localProfile(),
      credential_status: "configured",
      action_confirmation_mode: this.currentActions.confirmation_mode,
      policy_decision: {
        decision: "allow",
        endpoint: "http://127.0.0.1:8765/v1/chat/completions",
        reason: "allowlisted",
      },
    });
  }
}

class DeferredCommandClient extends FakeClient {
  private complete: ((response: SessionEventResponse) => void) | null = null;

  override postCommand(command: SessionCommand): Promise<SessionEventResponse> {
    this.commands.push(command);
    return new Promise((resolve) => {
      this.complete = resolve;
    });
  }

  completeCommand(): void {
    this.complete?.({ events: [] });
    this.complete = null;
  }
}

describe("App", () => {
  it("opens first-run setup and configures a shared research model source", async () => {
    const client = new FakeClient();
    client.currentReadiness = projectReadiness("setup-required");
    render(<App client={client} initialSessionId="session-test" />);

    expect(
      await screen.findByRole("heading", { name: "Set up Heartwood" }),
    ).toBeInTheDocument();
    expect(screen.getByText("synthetic-analysis")).toBeInTheDocument();
    const stanford = screen
      .getByText("Stanford AI API Gateway")
      .closest(".connection-row");
    expect(stanford).not.toBeNull();
    fireEvent.click(
      within(stanford as HTMLElement).getByRole("button", { name: "Set up" }),
    );
    fireEvent.change(await screen.findByLabelText("API token"), {
      target: { value: "runtime-only-token" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Load models" }));
    await screen.findByLabelText(
      "Models available from Stanford AI API Gateway",
    );
    fireEvent.click(screen.getByRole("button", { name: "Use model" }));

    await waitFor(() =>
      expect(client.currentSettings.model_source).toBe(
        "stanford-ai-api-gateway",
      ),
    );
    await waitFor(() => expect(client.currentReadiness.state).toBe("ready"));
    const project = screen
      .getByRole("heading", { name: "This project" })
      .closest<HTMLElement>("section");
    if (project === null)
      throw new Error("project readiness section is missing");
    expect(within(project).getByText("Ready")).toBeInTheDocument();
  });

  it("creates, renames, and switches persisted sessions", async () => {
    const client = new FakeClient();
    render(<App client={client} initialSessionId="session-test" />);
    await screen.findByRole("heading", { name: "Synthetic analysis" });

    fireEvent.click(screen.getByRole("button", { name: "New analysis" }));
    await screen.findByRole("heading", { name: "Untitled session" });
    fireEvent.click(screen.getByLabelText("Rename session"));
    fireEvent.change(screen.getByLabelText("Session title"), {
      target: { value: "Renamed analysis" },
    });
    fireEvent.keyDown(screen.getByLabelText("Session title"), { key: "Enter" });

    expect(
      await screen.findByRole("heading", { name: "Renamed analysis" }),
    ).toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", { name: /Synthetic analysis/u }),
    );
    expect(
      await screen.findByRole("heading", { name: "Synthetic analysis" }),
    ).toBeInTheDocument();
  });

  it("keeps a new session selected when initialization resolves later", async () => {
    const client = new DeferredInitializationClient();
    render(<App client={client} />);
    await waitFor(() => expect(client.listCalls).toBe(1));

    fireEvent.click(screen.getByRole("button", { name: "New analysis" }));
    await screen.findByRole("heading", { name: "Untitled session" });
    await act(async () => {
      client.completeInitialization([sessionSummary("session-test")]);
      await Promise.resolve();
    });

    expect(
      screen.getByRole("heading", { name: "Untitled session" }),
    ).toBeInTheDocument();
  });

  it("renders session state and sends detection commands", async () => {
    const client = new FakeClient();
    render(<App client={client} initialSessionId="session-test" />);

    await screen.findByRole("heading", { name: "Synthetic analysis" });
    fireEvent.click(screen.getByLabelText("Detect environment"));

    await waitFor(() => expect(client.commands).toHaveLength(1));
    expect(client.commands[0]?.kind).toBe("detect");
  });

  it("generates and retrieves a scrubbed audit export", async () => {
    const client = new FakeClient();
    render(<App client={client} initialSessionId="session-test" />);

    await screen.findByRole("heading", { name: "Synthetic analysis" });
    fireEvent.click(screen.getByRole("button", { name: "Export audit" }));

    await waitFor(() => expect(client.auditExportCalls).toBe(1));
    expect(client.commands.at(-1)?.kind).toBe("audit.export");
  });

  it("submits a coding-agent task from the conversation composer", async () => {
    const client = new FakeClient();
    client.currentSettings = {
      ...settings(),
      active_profile: "local",
      profiles: [localProfile()],
    };
    render(<App client={client} initialSessionId="session-test" />);
    await waitFor(() => expect(client.replayCalls).toBe(1));
    await waitFor(() => expect(screen.getByLabelText("Task")).toBeEnabled());

    fireEvent.change(screen.getByLabelText("Task"), {
      target: { value: "Inspect the synthetic cohort" },
    });
    fireEvent.click(screen.getByLabelText("Send task"));

    await waitFor(() => expect(client.commands.at(-1)?.kind).toBe("chat"));
    expect(client.commands.at(-1)?.payload).toEqual({
      prompt: "Inspect the synthetic cohort",
    });
    expect(
      within(
        screen.getByRole("log", { name: "Conversation transcript" }),
      ).getAllByText("Inspect the synthetic cohort"),
    ).toHaveLength(1);
  });

  it("keeps a delayed task visibly active without inventing workflow steps", async () => {
    const client = new DeferredCommandClient();
    client.currentSettings = {
      ...settings(),
      active_profile: "local",
      profiles: [localProfile()],
    };
    render(<App client={client} initialSessionId="session-test" />);
    await waitFor(() => expect(screen.getByLabelText("Task")).toBeEnabled());

    vi.useFakeTimers();
    try {
      fireEvent.change(screen.getByLabelText("Task"), {
        target: { value: "Inspect the synthetic cohort" },
      });
      fireEvent.click(screen.getByLabelText("Send task"));

      expect(
        screen.getByRole("status", {
          name: "Heartwood is working on your task",
        }),
      ).toBeInTheDocument();
      expect(screen.getByLabelText("Task")).toBeDisabled();

      await act(async () => {
        vi.advanceTimersByTime(11_000);
        await Promise.resolve();
      });
      expect(
        screen.getByRole("status", {
          name: /Heartwood is still working on your task.*Response time depends/u,
        }),
      ).toBeInTheDocument();
      expect(screen.getByText("11s elapsed")).toBeInTheDocument();

      await act(async () => {
        client.completeCommand();
        await Promise.resolve();
      });
    } finally {
      vi.useRealTimers();
    }
    await waitFor(() =>
      expect(
        screen.queryByText("Heartwood is still working on your task"),
      ).not.toBeInTheDocument(),
    );
    expect(screen.getByLabelText("Task")).toBeEnabled();
  });

  it("refreshes shared project configuration when the browser regains focus", async () => {
    const client = new FakeClient();
    client.currentSettings = {
      ...settings(),
      active_profile: "local",
      model_source: "local",
      profiles: [localProfile()],
    };
    render(<App client={client} initialSessionId="session-test" />);
    await waitFor(() => expect(screen.getByLabelText("Task")).toBeEnabled());

    client.currentSettings = settings();
    client.currentReadiness = projectReadiness("setup-required");
    await act(async () => {
      window.dispatchEvent(new Event("focus"));
      await Promise.resolve();
    });

    await waitFor(() => expect(screen.getByLabelText("Task")).toBeDisabled());
    expect(screen.getByText("Choose a model to begin.")).toBeInTheDocument();
  });

  it("refreshes shared project configuration when settings opens", async () => {
    const client = new FakeClient();
    client.currentSettings = {
      ...settings(),
      active_profile: "local",
      model_source: "local",
      profiles: [localProfile()],
    };
    render(<App client={client} initialSessionId="session-test" />);
    await waitFor(() => expect(screen.getByLabelText("Task")).toBeEnabled());

    client.currentSettings = settings();
    client.currentReadiness = projectReadiness("setup-required");
    fireEvent.click(screen.getByRole("button", { name: "Settings" }));

    await waitFor(() => expect(screen.getByLabelText("Task")).toBeDisabled());
    expect(
      screen.getByRole("heading", { name: "Set up Heartwood" }),
    ).toBeInTheDocument();
  });

  it("coalesces session refreshes for streamed event batches", async () => {
    const client = new FakeClient();
    render(<App client={client} initialSessionId="session-test" />);
    await screen.findByRole("heading", { name: "Synthetic analysis" });
    const initialListCalls = client.listCalls;

    act(() => {
      client.emitStream([
        event(7, "agent_message.emitted", { content: "First update" }),
      ]);
      client.emitStream([
        event(8, "agent_message.emitted", { content: "Second update" }),
      ]);
    });

    await waitFor(() => expect(client.listCalls).toBe(initialListCalls + 1));
  });

  it("renders the pending OpenHands action set and sends one batch decision", async () => {
    const client = new PendingClient();
    render(<App client={client} initialSessionId="session-test" />);

    const allow = await screen.findByLabelText("Allow all 1 action once");
    fireEvent.click(allow);

    await waitFor(() => expect(client.commands.at(-1)?.kind).toBe("approve"));
    expect(client.commands.at(-1)?.payload).toEqual({
      target_id: "session-test-toolcall-0",
      target_type: "tool-call",
    });
  });

  it("configures and validates model profiles in the settings panel", async () => {
    const client = new FakeClient();
    render(<App client={client} initialSessionId="session-test" />);
    fireEvent.click(screen.getByRole("button", { name: "Settings" }));

    await screen.findByRole("heading", { name: "Settings" });
    const researchConnection = screen
      .getByText("Research AI Service")
      .closest(".connection-row");
    expect(researchConnection).not.toBeNull();
    fireEvent.click(
      within(researchConnection as HTMLElement).getByRole("button", {
        name: "Choose",
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Load models" }));
    const modelSelect = await screen.findByLabelText(
      "Models available from Research AI Service",
    );
    expect(modelSelect).toHaveTextContent("Provider Coder");
    fireEvent.click(screen.getByRole("button", { name: "Use model" }));
    await waitFor(() =>
      expect(client.modelConnectionRequest).toEqual({
        connection_id: "research-ai",
        model_id: "provider-coder",
      }),
    );
    expect(client.catalogRequest).toEqual({
      connection_id: "research-ai",
      refresh: true,
    });
    expect(screen.getByLabelText("Active model profile")).toHaveTextContent(
      "Research AI Service · provider-coder",
    );
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Approvals" }), {
      button: 0,
    });
    fireEvent.click(
      screen.getByRole("button", { name: "Auto-Approve Low Risk" }),
    );
    await waitFor(() =>
      expect(client.currentActions.confirmation_mode).toBe("confirm-risky"),
    );
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Models" }), {
      button: 0,
    });
    fireEvent.click(screen.getByText("More options"));
    fireEvent.click(
      screen.getByRole("button", {
        name: /research-ai.*litellm_proxy\/provider-coder/u,
      }),
    );
    fireEvent.change(screen.getByLabelText("Provider preset"), {
      target: { value: "local-openai-compatible" },
    });
    fireEvent.change(screen.getByLabelText("Model"), {
      target: { value: "openai/local-model" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save profile" }));

    await waitFor(() =>
      expect(client.savedProfile?.model).toBe("openai/local-model"),
    );
    fireEvent.click(screen.getByLabelText("Validate active model profile"));
    expect(await screen.findByText("Authorized")).toBeInTheDocument();
    expect(screen.getByText("Allowed by this environment")).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("Download Stories 260K"));
    await waitFor(() => expect(client.downloadedArtifact).toBe("stories260k"));
    const progress = await screen.findByRole("progressbar", {
      name: "Download progress for Stories 260K",
    });
    expect(progress).toHaveAttribute("aria-valuenow", String(64 * 1024 * 1024));
    expect(
      await screen.findByText("Ready for Heartwood launch"),
    ).toBeInTheDocument();
    expect(screen.getByText("Model runtime needed")).toBeInTheDocument();
    expect(screen.getByText("heartwood launch --web")).toBeInTheDocument();
    expect(screen.getByLabelText("Active model profile")).toHaveTextContent(
      "Local · heartwood-local-model",
    );
    expect(screen.getByLabelText("Task")).toBeDisabled();
    expect(
      screen.getByText("Start the selected model with heartwood launch --web."),
    ).toBeInTheDocument();
  });

  it("uses a transient cloud token to discover and select a model", async () => {
    const client = new FakeClient();
    render(<App client={client} initialSessionId="session-test" />);
    fireEvent.click(screen.getByRole("button", { name: "Settings" }));

    const openAiConnection = (await screen.findByText("OpenAI")).closest(
      ".connection-row",
    );
    expect(openAiConnection).not.toBeNull();
    fireEvent.click(
      within(openAiConnection as HTMLElement).getByRole("button", {
        name: "Connect",
      }),
    );
    fireEvent.change(await screen.findByLabelText("API token"), {
      target: { value: "runtime-only-token" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Load models" }));
    await screen.findByLabelText("Models available from OpenAI");
    expect(client.currentSettings.model_source).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Use model" }));

    await waitFor(() =>
      expect(client.modelConnectionRequest).toEqual({
        connection_id: "openai",
        model_id: "provider-coder",
      }),
    );
    expect(client.catalogRequest).toEqual({
      connection_id: "openai",
      token: "runtime-only-token",
      refresh: true,
    });
    expect(screen.getByLabelText("API token")).toHaveValue("");
    expect(JSON.stringify(client.currentSettings)).not.toContain(
      "runtime-only-token",
    );
  });

  it("plans and downloads another Hugging Face model through the shared gateway", async () => {
    const client = new FakeClient();
    render(<App client={client} initialSessionId="session-test" />);
    fireEvent.click(screen.getByRole("button", { name: "Settings" }));
    fireEvent.click(await screen.findByText("Other model"));
    fireEvent.change(screen.getByLabelText("Model repository"), {
      target: { value: "research/research-model-gguf" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Check model" }));

    expect(
      await screen.findByText("Research Model Q4_K_M"),
    ).toBeInTheDocument();
    const selectionReason = screen.getByText(
      "Selected a balanced GGUF model for the CPU runtime.",
    );
    const modelPlan = selectionReason.closest(".local-model-plan");
    expect(modelPlan).not.toBeNull();
    expect(
      within(modelPlan as HTMLElement).getByText(/Recommended: 8 CPU cores/u),
    ).toBeInTheDocument();
    expect(
      within(modelPlan as HTMLElement).getByText(`Revision: ${"1".repeat(40)}`),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Download model" }));

    await waitFor(() =>
      expect(client.customDownloadRequest).toEqual({
        repository: "research/research-model-gguf",
        revision: "1".repeat(40),
      }),
    );
    expect(client.inspectedRepository).toEqual({
      repository: "research/research-model-gguf",
    });
    expect(
      within(modelPlan as HTMLElement).getByRole("progressbar", {
        name: "Download progress for Research Model Q4_K_M",
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByLabelText("Download Research Model Q4_K_M"),
    ).toBeDisabled();
  });

  it("continues polling a model download after a transient status failure", async () => {
    const client = new FakeClient();
    render(<App client={client} initialSessionId="session-test" />);
    fireEvent.click(screen.getByRole("button", { name: "Settings" }));
    await screen.findByLabelText("Download Stories 260K");
    client.artifactFailures = 1;

    fireEvent.click(screen.getByLabelText("Download Stories 260K"));

    expect(
      await screen.findByText("temporary model status failure"),
    ).toBeInTheDocument();
    expect(
      await screen.findByText(
        "Ready for Heartwood launch",
        {},
        { timeout: 5_000 },
      ),
    ).toBeInTheDocument();
    expect(client.artifactCalls).toBeGreaterThanOrEqual(3);
    expect(
      screen.queryByText("temporary model status failure"),
    ).not.toBeInTheDocument();
  });

  it("links unsupported Hugging Face models to the issue chooser", async () => {
    const client = new FakeClient();
    client.repositoryError = new Error(
      "Heartwood does not yet support this model. Report it at https://github.com/SchmiedmayerLab/heartwood/issues/new/choose",
    );
    render(<App client={client} initialSessionId="session-test" />);
    fireEvent.click(screen.getByRole("button", { name: "Settings" }));
    fireEvent.click(await screen.findByText("Other model"));
    fireEvent.change(screen.getByLabelText("Model repository"), {
      target: { value: "research/unsupported-model" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Check model" }));

    expect(
      await screen.findByRole("link", { name: "Report an unsupported model" }),
    ).toHaveAttribute(
      "href",
      "https://github.com/SchmiedmayerLab/heartwood/issues/new/choose",
    );
  });

  it("allows a manual identifier only when a custom catalog is unavailable", async () => {
    const client = new FakeClient();
    client.catalogError = new Error("model provider catalog is unavailable");
    render(<App client={client} initialSessionId="session-test" />);
    fireEvent.click(screen.getByRole("button", { name: "Settings" }));

    const customConnection = (await screen.findByText("Custom API")).closest(
      ".connection-row",
    );
    expect(customConnection).not.toBeNull();
    fireEvent.click(
      within(customConnection as HTMLElement).getByRole("button", {
        name: "Connect",
      }),
    );
    fireEvent.change(screen.getByLabelText("Server URL"), {
      target: { value: "https://models.example/v1" },
    });
    fireEvent.change(screen.getByLabelText("Token (optional for local)"), {
      target: { value: "runtime-only-token" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Load models" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "model provider catalog is unavailable",
    );
    fireEvent.change(screen.getByLabelText("Model identifier"), {
      target: { value: "custom-coder" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Use model" }));

    await waitFor(() =>
      expect(client.modelConnectionRequest).toEqual({
        connection_id: "custom-api",
        model_id: "custom-coder",
        base_url: "https://models.example/v1",
        manual: true,
      }),
    );
  });

  it("disables action modes blocked by platform policy", async () => {
    const client = new FakeClient();
    client.currentActions = {
      ...client.currentActions,
      modes: client.currentActions.modes.map((option) => ({
        ...option,
        allowed: option.mode === "always-confirm",
      })),
    };
    render(<App client={client} initialSessionId="session-test" />);

    fireEvent.click(screen.getByRole("button", { name: "Settings" }));
    fireEvent.mouseDown(await screen.findByRole("tab", { name: "Approvals" }), {
      button: 0,
    });

    expect(
      await screen.findByRole("button", { name: "Auto-Approve Low Risk" }),
    ).toBeDisabled();
  });

  it("keeps setup incomplete when the selected credential is unavailable", async () => {
    const client = new FakeClient();
    client.currentSettings = {
      ...settings(),
      active_profile: "local",
      profiles: [{ ...localProfile(), credential_status: "missing" }],
    };

    render(<App client={client} initialSessionId="session-test" />);

    expect(
      await screen.findByText(
        "Add the credential required by the selected model.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Task")).toBeDisabled();
    expect(screen.getByLabelText("Pause agent")).toBeDisabled();
    expect(screen.getByText("Setup needed")).toBeInTheDocument();
  });

  it("uses shared compute readiness before a launch materializes the local profile", async () => {
    const client = new FakeClient();
    client.currentSettings = {
      ...settings(),
      model_source: "local",
    };
    client.currentReadiness = projectReadiness("compute-required");

    render(<App client={client} initialSessionId="session-test" />);

    expect(
      await screen.findByText(
        "Start the selected model with heartwood launch --web.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Task")).toBeDisabled();
  });

  it("keeps the composer unavailable when route validation fails", async () => {
    const client = new FakeClient();
    client.currentSettings = {
      ...settings(),
      active_profile: "local",
      profiles: [localProfile()],
    };
    client.validationError = new Error("validation unavailable");

    render(<App client={client} initialSessionId="session-test" />);

    expect(
      await screen.findByText(
        "Access to the selected model could not be verified.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Task")).toBeDisabled();
    expect(screen.getByText("Needs attention")).toBeInTheDocument();
  });

  it("supports secondary activity, settings, rejection, and pause controls", async () => {
    const client = new PendingClient();
    client.currentSettings = {
      ...settings(),
      active_profile: "local",
      profiles: [localProfile()],
    };
    render(<App client={client} initialSessionId="session-test" />);

    fireEvent.click(await screen.findByLabelText("Reject all 1 action"));
    await waitFor(() => expect(client.commands.at(-1)?.kind).toBe("deny"));
    fireEvent.click(screen.getByLabelText("Pause agent"));
    await waitFor(() => expect(client.commands.at(-1)?.kind).toBe("pause"));

    fireEvent.click(screen.getByRole("button", { name: "Activity & audit" }));
    expect(
      await screen.findByRole("heading", { name: "Activity & audit" }),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(client.replayCalls).toBeGreaterThan(1));
    fireEvent.click(screen.getByRole("button", { name: "Close" }));

    fireEvent.click(screen.getByRole("button", { name: "Settings" }));
    await screen.findByRole("heading", { name: "Settings" });
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    fireEvent.click(screen.getByText("More options"));
    fireEvent.click(
      screen.getByRole("button", { name: /local.*openai\/local-model/u }),
    );
    fireEvent.change(screen.getByLabelText("Provider preset"), {
      target: { value: "" },
    });
    fireEvent.change(screen.getByLabelText("Credential kind"), {
      target: { value: "environment" },
    });
    fireEvent.change(screen.getByLabelText("API key environment variable"), {
      target: { value: "OPENAI_API_KEY" },
    });
    fireEvent.change(screen.getByLabelText("Credential kind"), {
      target: { value: "file" },
    });
    fireEvent.change(screen.getByLabelText("API key file"), {
      target: { value: "/run/secrets/model" },
    });
    fireEvent.click(screen.getByLabelText("Remove local"));
    await waitFor(() =>
      expect(client.currentSettings.profiles).toHaveLength(0),
    );
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(
      screen.queryByRole("heading", { name: "Settings" }),
    ).not.toBeInTheDocument();
  });

  it("submits only one decision for a pending OpenHands action set", async () => {
    const client = new PendingClient();
    client.currentSettings = {
      ...settings(),
      active_profile: "local",
      profiles: [localProfile()],
    };
    render(<App client={client} initialSessionId="session-test" />);

    const allow = await screen.findByLabelText("Allow all 1 action once");
    const reject = screen.getByLabelText("Reject all 1 action");
    expect(
      screen.getByText(
        "OpenHands proposed these actions together. One decision applies to every action below.",
      ),
    ).toBeVisible();

    fireEvent.click(allow);
    fireEvent.click(reject);

    await waitFor(() =>
      expect(client.commands.map((command) => command.kind)).toEqual([
        "approve",
      ]),
    );
  });

  it("inspects and explicitly approves a mounted Skill extension", async () => {
    const client = new FakeClient();
    render(<App client={client} initialSessionId="session-test" />);

    fireEvent.click(screen.getByRole("button", { name: "Skills" }));
    expect(await screen.findByText("aggregate-export")).toBeInTheDocument();
    fireEvent.click(screen.getByText("Install an extension"));
    fireEvent.change(screen.getByLabelText("Mounted source directory"), {
      target: { value: "/mnt/community-summary" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Inspect" }));
    expect(await screen.findByText("community-summary")).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("Approve this installation"));
    fireEvent.click(screen.getByRole("button", { name: "Install" }));

    await waitFor(() =>
      expect(client.installedSkill).toBe("/mnt/community-summary"),
    );
    fireEvent.click(await screen.findByLabelText("Remove community-summary"));
    await waitFor(() =>
      expect(client.installedSkill).toBe("removed:community-summary"),
    );
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
  });
});

class PendingClient extends FakeClient {
  override replayEvents(): Promise<SessionEventResponse> {
    this.replayCalls += 1;
    return Promise.resolve({ events: syntheticEvents() });
  }
}

class DeferredInitializationClient extends FakeClient {
  private readonly initialization: Promise<SessionList>;
  private resolveInitialization: (sessions: SessionList) => void = () =>
    undefined;

  constructor() {
    super();
    this.initialization = new Promise((resolve) => {
      this.resolveInitialization = resolve;
    });
  }

  override listSessions(): Promise<SessionList> {
    this.listCalls += 1;
    return this.initialization;
  }

  completeInitialization(sessions: SessionSummary[]): void {
    this.resolveInitialization({ sessions });
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

    await screen.findByRole("heading", { name: "Synthetic analysis" });
    fireEvent.click(screen.getByRole("button", { name: "Export audit" }));

    expect(await screen.findByText("synthetic gateway failure")).toBeVisible();
  });
});

const localProfile = (): ModelProfile => ({
  profile_id: "local",
  model: "openai/local-model",
  policy_endpoint: "http://127.0.0.1:8765/v1/chat/completions",
  capability_tier: "supervised",
  base_url: "http://127.0.0.1:8765/v1",
  credential_kind: "none",
  api_key_env: null,
  api_key_file: null,
  api_version: null,
  aws_region_name: null,
  aws_profile_name: null,
  description: "Local model",
});

const modelConnection = (
  connectionId: string,
  label: string,
  source: ModelConnection["source"],
  credentialStatus: ModelConnection["credential_status"],
  acceptsToken: boolean,
): ModelConnection => ({
  connection_id: connectionId,
  label,
  protocol:
    connectionId === "anthropic" ? "anthropic"
    : connectionId === "research-ai" ? "static"
    : "openai-compatible",
  model_prefix: connectionId === "research-ai" ? "litellm_proxy/" : "openai/",
  source,
  credential_kind:
    connectionId === "local" ? "none"
    : connectionId === "research-ai" ? "managed-identity"
    : "environment",
  policy_endpoint:
    connectionId === "custom-api" ? null : (
      "http://127.0.0.1:8765/v1/chat/completions"
    ),
  catalog_endpoint:
    connectionId === "custom-api" ? null : "http://127.0.0.1:8765/v1/models",
  base_url: connectionId === "local" ? "http://127.0.0.1:8765/v1" : null,
  api_key_env:
    acceptsToken ?
      connectionId === "custom-api" ?
        "HEARTWOOD_CUSTOM_MODEL_API_KEY"
      : "OPENAI_API_KEY"
    : null,
  api_key_file: null,
  api_version: null,
  aws_region_name: null,
  aws_profile_name: null,
  description: `${label} models`,
  static_models: [],
  accepts_token: acceptsToken,
  credential_status: credentialStatus,
});

const modelSource = (
  sourceId: ModelSource,
  connectionId: string,
  label: string,
) => ({
  source_id: sourceId,
  connection_id: connectionId,
  label,
  description: `${label} models`,
  selected: false,
});

const projectReadiness = (
  state: ProjectReadiness["state"] = "ready",
): ProjectReadiness => ({
  state,
  platform_id: "generic",
  project_root: "/projects/synthetic-analysis",
  state_root: "/projects/synthetic-analysis/.heartwood",
  evidence: ["synthetic test"],
  checks: [
    {
      check_id: "configuration",
      status: state === "ready" ? "pass" : "warning",
      summary:
        state === "ready" ?
          "Project configuration is valid"
        : "Setup is incomplete",
    },
  ],
});

const bundledSkill = (): SkillSummary => ({
  name: "aggregate-export",
  skill_id: "heartwood.synthetic.aggregate-export",
  description: "Aggregate export Skill",
  trust_tier: "verified",
  source: "bundled",
  approval_summary: "Writes reviewed aggregate output.",
  declared_tools: ["write-aggregate-json"],
  requires_network: false,
});

const sessionSummary = (
  sessionId: string,
  title = "Synthetic analysis",
): SessionSummary => ({
  session_id: sessionId,
  title,
  status: "idle",
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  event_count: 0,
});
