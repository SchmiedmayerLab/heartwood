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
import { describe, expect, it } from "vitest";
import { App } from "./App";
import type { HeartwoodClient, SessionEventResponse } from "./client";
import { event, syntheticEvents } from "./test/fixtures";
import type {
  ActionConfirmationMode,
  ActionSettings,
  AuditExport,
  ModelArtifacts,
  ModelDownload,
  ModelProfile,
  ModelSettings,
  ModelValidation,
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
  profiles: [],
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
  savedProfile: ModelProfile | null = null;
  connectedProvider: { presetId: string; modelName: string } | null = null;
  currentSettings = settings();
  currentActions = actions();
  currentDownloads: ModelDownload[] = [];
  downloadedArtifact: string | null = null;
  installedSkill: string | null = null;
  currentSessions: SessionSummary[] = [sessionSummary("session-test")];
  streamListener: ((events: SessionEvent[]) => void) | null = null;

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

  connectModelProvider(
    presetId: string,
    modelName: string,
  ): Promise<ModelSettings> {
    this.connectedProvider = { presetId, modelName };
    const profile = {
      ...localProfile(),
      profile_id: presetId,
      model:
        modelName.startsWith("openai/") ? modelName : `openai/${modelName}`,
    };
    this.currentSettings = {
      ...this.currentSettings,
      active_profile: presetId,
      profiles: [profile],
    };
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
    }
    return Promise.resolve({
      schema_version: "heartwood.local-model-catalog.v1",
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
        },
      ],
      downloads: this.currentDownloads,
    });
  }

  downloadModelArtifact(artifactId: string): Promise<ModelDownload> {
    this.downloadedArtifact = artifactId;
    const download: ModelDownload = {
      artifact_id: artifactId,
      status: "downloading",
      bytes_downloaded: 64 * 1024 * 1024,
      bytes_total: 256 * 1024 * 1024,
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

describe("App", () => {
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
    render(<App client={client} initialSessionId="session-test" />);
    await waitFor(() => expect(client.replayCalls).toBe(1));

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

  it("renders pending OpenHands actions inline and sends allow once", async () => {
    const client = new PendingClient();
    render(<App client={client} initialSessionId="session-test" />);

    const allow = await screen.findByLabelText("Allow session-test-toolcall-0");
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
    fireEvent.click(screen.getByRole("button", { name: "Model setup" }));

    await screen.findByRole("heading", { name: "Model setup" });
    fireEvent.click(
      screen.getByRole("button", { name: "Auto-Approve Low Risk" }),
    );
    await waitFor(() =>
      expect(client.currentActions.confirmation_mode).toBe("confirm-risky"),
    );
    fireEvent.change(screen.getByLabelText("Model name"), {
      target: { value: "local-model" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save and use" }));
    await waitFor(() =>
      expect(client.connectedProvider).toEqual({
        presetId: "local-openai-compatible",
        modelName: "local-model",
      }),
    );
    expect(
      screen.getByRole("option", {
        name: "Local OpenAI-Compatible · local-model",
      }),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByText("More options"));
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
    fireEvent.change(screen.getByLabelText("Active model profile"), {
      target: { value: "local" },
    });
    await waitFor(() =>
      expect(client.currentSettings.active_profile).toBe("local"),
    );
    fireEvent.click(screen.getByLabelText("Validate active model profile"));
    expect(await screen.findByText("allow")).toBeInTheDocument();
    expect(screen.getByText("configured")).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("Download Stories 260K"));
    await waitFor(() => expect(client.downloadedArtifact).toBe("stories260k"));
    const progress = await screen.findByRole("progressbar", {
      name: "Download progress for Stories 260K",
    });
    expect(progress).toHaveAttribute("value", String(64 * 1024 * 1024));
    expect(
      await screen.findByText("Ready in model storage"),
    ).toBeInTheDocument();
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

    fireEvent.click(screen.getByRole("button", { name: "Model setup" }));

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
      await screen.findByText("Model setup is incomplete."),
    ).toBeInTheDocument();
  });

  it("supports secondary activity, settings, rejection, and pause controls", async () => {
    const client = new PendingClient();
    client.currentSettings = {
      ...settings(),
      active_profile: "local",
      profiles: [localProfile()],
    };
    render(<App client={client} initialSessionId="session-test" />);

    fireEvent.click(
      await screen.findByLabelText("Reject session-test-toolcall-0"),
    );
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

    fireEvent.click(screen.getByRole("button", { name: "Model setup" }));
    await screen.findByRole("heading", { name: "Model setup" });
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
      screen.queryByRole("heading", { name: "Model setup" }),
    ).not.toBeInTheDocument();
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
