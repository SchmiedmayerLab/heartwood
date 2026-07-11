/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

import {
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
  ModelArtifacts,
  ModelDownload,
  ModelProfile,
  ModelSettings,
  ModelValidation,
  SessionCommand,
  SessionEvent,
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
  replayCalls = 0;
  savedProfile: ModelProfile | null = null;
  currentSettings = settings();
  currentActions = actions();
  downloadedArtifact: string | null = null;
  installedSkill: string | null = null;

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
    _onEvents: (events: SessionEvent[]) => void,
  ): () => void {
    return vi.fn();
  }

  getModelSettings(): Promise<ModelSettings> {
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
      downloads: [],
    });
  }

  downloadModelArtifact(artifactId: string): Promise<ModelDownload> {
    this.downloadedArtifact = artifactId;
    return Promise.resolve({
      artifact_id: artifactId,
      status: "downloading",
      path: null,
      error: null,
    });
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
  it("renders session state and sends detection commands", async () => {
    const client = new FakeClient();
    render(<App client={client} initialSessionId="session-test" />);

    await waitFor(() =>
      expect(screen.getByLabelText("Session ID")).toHaveValue("session-test"),
    );
    fireEvent.click(screen.getByLabelText("Detect environment"));

    await waitFor(() => expect(client.commands).toHaveLength(1));
    expect(client.commands[0]?.kind).toBe("detect");
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
    fireEvent.click(screen.getByLabelText("Settings"));

    await screen.findByRole("heading", { name: "Settings" });
    fireEvent.click(
      screen.getByRole("button", { name: "Auto-Approve Low Risk" }),
    );
    await waitFor(() =>
      expect(client.currentActions.confirmation_mode).toBe("confirm-risky"),
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
    expect(await screen.findByText("downloading")).toBeInTheDocument();
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

    fireEvent.click(screen.getByLabelText("Settings"));

    expect(
      await screen.findByRole("button", { name: "Auto-Approve Low Risk" }),
    ).toBeDisabled();
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

    fireEvent.click(screen.getByLabelText("Show activity"));
    expect(
      await screen.findByRole("heading", { name: "Activity" }),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("Replay events"));
    await waitFor(() => expect(client.replayCalls).toBeGreaterThan(1));
    fireEvent.click(screen.getByLabelText("Close activity"));

    fireEvent.click(screen.getByLabelText("Settings"));
    await screen.findByRole("heading", { name: "Settings" });
    fireEvent.click(screen.getByLabelText("Refresh settings"));
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
    fireEvent.click(screen.getByLabelText("Close settings"));
    expect(
      screen.queryByRole("heading", { name: "Settings" }),
    ).not.toBeInTheDocument();
  });

  it("inspects and explicitly approves a mounted Skill extension", async () => {
    const client = new FakeClient();
    render(<App client={client} initialSessionId="session-test" />);

    fireEvent.click(screen.getByLabelText("Skills"));
    expect(await screen.findByText("aggregate-export")).toBeInTheDocument();
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
    fireEvent.click(screen.getByLabelText("Close Skills"));
  });
});

class PendingClient extends FakeClient {
  override replayEvents(): Promise<SessionEventResponse> {
    this.replayCalls += 1;
    return Promise.resolve({ events: syntheticEvents() });
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
    fireEvent.click(screen.getByLabelText("Export audit"));

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
