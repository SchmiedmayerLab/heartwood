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
import type { HeartwoodClient, SessionProjectionResponse } from "./client";
import {
  emptyProjection,
  syntheticEvents,
  syntheticProjection,
} from "./test/fixtures";
import type {
  ActionConfirmationMode,
  ActionSettings,
  AuditExport,
  CustomLocalModelDownloadRequest,
  CredentialSettings,
  LocalModelChoice,
  LocalModelImportRequest,
  LocalModelImportResult,
  ModelArtifacts,
  ModelCatalog,
  ModelCatalogRequest,
  ModelConnectRequest,
  ModelConnection,
  ModelDownload,
  ModelProfile,
  ModelProfileDraft,
  ModelRepositoryPlan,
  ModelRepositoryRequest,
  ModelSource,
  ModelSettings,
  ModelValidation,
  ProjectReadiness,
  SessionCommand,
  SessionList,
  SessionProjection,
  SessionSummary,
  SkillSettings,
  SkillSummary,
  StartupPlan,
  SubscriptionDeviceLogin,
} from "./types";

const settings = (): ModelSettings => ({
  schema_version: "heartwood.model-settings.v1",
  active_profile: null,
  model_source: null,
  profiles: [],
  connections: [
    modelConnection(
      "heartwood",
      "Run with Heartwood",
      "built-in",
      "configured",
      false,
    ),
    modelConnection(
      "research-ai",
      "Research AI Service",
      "platform",
      "configured",
      false,
    ),
    modelConnection(
      "openai-subscription",
      "Sign in with ChatGPT",
      "built-in",
      "missing",
      false,
    ),
    modelConnection("openai", "OpenAI API", "built-in", "missing", true),
    modelConnection("anthropic", "Anthropic", "built-in", "missing", true),
    modelConnection(
      "custom-api",
      "Other compatible service",
      "user",
      "missing",
      true,
    ),
  ],
  presets: [
    {
      preset_id: "heartwood-managed",
      label: "Heartwood-managed model",
      model_prefix: "openai/",
      credential_kind: "none",
      api_key_env: null,
      base_url: "http://127.0.0.1:8765/v1",
      policy_endpoint: "http://127.0.0.1:8765/v1/chat/completions",
      description: "Heartwood-managed model",
    },
  ],
  source_options: [
    modelSource("heartwood", "heartwood", "Run with Heartwood"),
    modelSource(
      "openai-subscription",
      "openai-subscription",
      "Sign in with ChatGPT",
    ),
    modelSource("openai", "openai", "OpenAI API"),
    modelSource("anthropic", "anthropic", "Anthropic"),
    modelSource(
      "stanford-ai-api-gateway",
      "stanford-ai-api-gateway",
      "Stanford AI API Gateway",
    ),
  ],
  credential_store: {
    backends: ["process", "keyring"],
    default_backend: "process",
    persistence_available: true,
    persistence_description:
      "System credential store for this Heartwood environment",
  },
  credential_bindings: [],
});

const actions = (): ActionSettings => ({
  schema_version: "heartwood.action-settings.v1",
  confirmation_mode: "always-confirm",
  scope_description:
    "Shared by every Heartwood interface in this project and applied to future action sets.",
  presentation: {
    risk_labels: {
      high: "High Risk",
      low: "Low Risk",
      medium: "Medium Risk",
      unknown: "Not Classified",
    },
    tool_labels: {
      file_editor: "File Change",
      terminal: "Terminal Command",
    },
    other_tool_label_template: "{tool_name} Action",
    unknown_risk_label: "Not Classified",
    unknown_tool_label: "Tool Action",
  },
  change_allowed: true,
  change_blocked_reason: null,
  modes: [
    {
      mode: "always-confirm",
      command_value: "ask-every-time",
      label: "Review Every Action",
      description:
        "Heartwood pauses before every proposed action set so you can inspect it before anything runs.",
      automatic_risks: [],
      reviewed_risks: ["low", "medium", "high", "unknown"],
      recommended: true,
      allowed: true,
      unavailable_reason: null,
    },
    {
      mode: "confirm-risky",
      command_value: "auto-approve-low-risk",
      label: "Low-Risk Automation",
      description:
        "An action set continues automatically only when every action is low risk. Any medium-, high-, or unclassified-risk action pauses the complete set for review.",
      automatic_risks: ["low"],
      reviewed_risks: ["medium", "high", "unknown"],
      recommended: false,
      allowed: true,
      unavailable_reason: null,
    },
  ],
});

const localModelChoice = (
  overrides: Partial<LocalModelChoice> = {},
): LocalModelChoice => ({
  model_id: "stories260k",
  label: "Stories 260K",
  purpose: "Synthetic smoke-test model.",
  runtime: "llama-cpp",
  source_repository: "example/stories260k",
  source_revision: "0".repeat(40),
  source_path: "model.gguf",
  size_bytes: 256 * 1024 * 1024,
  minimum_free_bytes: 256 * 1024 * 1024,
  license_id: "test-fixture",
  license_posture: "Test fixture",
  catalog_source: "catalog",
  model_type: null,
  context_window: 32_768,
  maximum_context_window: 32_768,
  precision: "GGUF Q4_K_M",
  tier: "standard",
  qualification: "qualified",
  minimum_gpu_count: 0,
  minimum_gpu_memory_bytes: 0,
  recommended_cpu_count: 8,
  recommended_ram_bytes: 16 * 1024 * 1024 * 1024,
  recommended_disk_bytes: 1024 * 1024 * 1024,
  tool_call_parser: null,
  tensor_parallel_size: 1,
  startup_seconds_min: 5,
  startup_seconds_max: 30,
  download_policy: null,
  allow_patterns: [],
  ignore_patterns: [],
  validated_platforms: ["ci"],
  qualification_test: "synthetic-browser-e2e-v1",
  qualification_date: "2026-07-22",
  qualification_evidence: "https://example.test/qualification",
  artifact_sha256: "a".repeat(64),
  minimum_resource_envelope: "Minimum: 4 CPU cores and 8 GB RAM.",
  recommended_resource_envelope: "Recommended: 8 CPU cores and 16 GB RAM.",
  active: false,
  available: true,
  selected: false,
  availability_reason: "Available on this deployment",
  recommended: true,
  ...overrides,
});

class FakeClient implements HeartwoodClient {
  commands: SessionCommand[] = [];
  auditExportCalls = 0;
  listCalls = 0;
  replayCalls = 0;
  artifactCalls = 0;
  artifactFailures = 0;
  activeManagedModel = false;
  savedProfile: ModelProfileDraft | null = null;
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
  localImportRequest: LocalModelImportRequest | null = null;
  inspectedRepository: ModelRepositoryRequest | null = null;
  repositoryError: Error | null = null;
  customModel: LocalModelChoice | null = null;
  installedSkill: string | null = null;
  currentSessions: SessionSummary[] = [sessionSummary("session-test")];
  projections = new Map<string, SessionProjection>();
  streamListener: ((projection: SessionProjection) => void) | null = null;
  retiredStreamListener: ((projection: SessionProjection) => void) | null =
    null;
  commandFailure: { code: string; message: string } | null = null;
  subscriptionPolls = 0;

  getProjectReadiness(): Promise<ProjectReadiness> {
    return Promise.resolve(this.currentReadiness);
  }

  getStartupPlan(): Promise<StartupPlan> {
    return Promise.resolve(startupPlan(this.currentReadiness));
  }

  initializeProject(): Promise<StartupPlan> {
    this.currentReadiness = projectReadiness("setup-required");
    return Promise.resolve(startupPlan(this.currentReadiness));
  }

  listSessions(): Promise<SessionList> {
    this.listCalls += 1;
    return Promise.resolve({ sessions: this.currentSessions });
  }

  ensureDefaultSession(): Promise<SessionSummary> {
    const existing = this.currentSessions.find(
      (session) => session.session_id === "session-main",
    );
    if (existing) return Promise.resolve(existing);
    const created = sessionSummary("session-main", "Main session");
    this.currentSessions = [created, ...this.currentSessions];
    return Promise.resolve(created);
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

  postCommand(command: SessionCommand): Promise<SessionProjectionResponse> {
    this.commands.push(command);
    const current = this.projectionFor(command.session_id);
    const prompt =
      typeof command.payload.prompt === "string" ? command.payload.prompt : "";
    const commandFailure = this.commandFailure;
    this.commandFailure = null;
    const next: SessionProjection = {
      ...current,
      eventCount: current.eventCount + 1,
      revision: current.revision + 1,
      lastCommandOutcome: {
        commandId: command.command_id,
        commandKind: command.kind,
        status: commandFailure === null ? "accepted" : "rejected",
        errorCode: commandFailure?.code ?? null,
        message: commandFailure?.message ?? null,
      },
      conversation:
        command.kind === "chat" && prompt ?
          [
            ...current.conversation,
            {
              id: `local-${command.command_id}`,
              sequence: current.revision + 1,
              role: "user",
              label: "You",
              content: prompt,
              detail: null,
              technicalDetail: null,
            },
          ]
        : current.conversation,
      pendingApproval:
        command.kind === "approve" || command.kind === "deny" ?
          null
        : current.pendingApproval,
      paused:
        command.kind === "pause" ? true
        : command.kind === "resume" ? false
        : current.paused,
      lifecycle:
        command.kind === "pause" ?
          {
            status: "paused",
            canPause: false,
            canResume: true,
            canSteer: true,
          }
        : command.kind === "resume" ?
          {
            status: "running",
            canPause: true,
            canResume: false,
            canSteer: true,
          }
        : command.kind === "approve" || command.kind === "deny" ?
          {
            status: "idle",
            canPause: false,
            canResume: false,
            canSteer: true,
          }
        : current.lifecycle,
      availableCommands:
        command.kind === "pause" ? ["chat", "resume"]
        : command.kind === "resume" ? ["chat", "pause"]
        : command.kind === "approve" || command.kind === "deny" ? ["chat"]
        : current.availableCommands,
    };
    this.projections.set(command.session_id, next);
    return Promise.resolve({ events: [], projection: next });
  }

  replayEvents(sessionId: string): Promise<SessionProjectionResponse> {
    this.replayCalls += 1;
    return Promise.resolve({
      events: [],
      projection: this.projectionFor(sessionId),
    });
  }

  streamSession(
    _sessionId: string,
    _afterSequence: number | undefined,
    onProjection: (projection: SessionProjection) => void,
  ): () => void {
    this.streamListener = onProjection;
    return () => {
      if (this.streamListener === onProjection) {
        this.retiredStreamListener = onProjection;
        this.streamListener = null;
      }
    };
  }

  emitStream(projection: SessionProjection): void {
    this.projections.set(projection.sessionId, projection);
    this.streamListener?.(projection);
  }

  emitRetiredStream(projection: SessionProjection): void {
    this.retiredStreamListener?.(projection);
  }

  projectionFor(sessionId: string): SessionProjection {
    const existing = this.projections.get(sessionId);
    if (existing) return existing;
    const created = emptyProjection(sessionId);
    this.projections.set(sessionId, created);
    return created;
  }

  getModelSettings(): Promise<ModelSettings> {
    return Promise.resolve(this.currentSettings);
  }

  importLocalModel(
    request: LocalModelImportRequest,
  ): Promise<LocalModelImportResult> {
    this.localImportRequest = request;
    return Promise.resolve({
      model: localModelChoice({
        model_id: "imported-model",
        label: "Imported model",
        purpose: "User-imported model",
        source_repository: request.repository,
        source_revision: request.revision,
        size_bytes: 1024,
        minimum_free_bytes: 2048,
        license_id: request.license,
        license_posture: request.license,
        catalog_source: "user-selected",
        minimum_resource_envelope: "4 GiB RAM",
        recommended_resource_envelope: "8 GiB RAM",
        selected: true,
      }),
      path: "/project/.heartwood/models/imported/model.gguf",
      status: "ready",
    });
  }

  forgetCredential(connectionId: string): Promise<CredentialSettings> {
    const connection = this.currentSettings.connections.find(
      (candidate) => candidate.connection_id === connectionId,
    );
    this.currentSettings = {
      ...this.currentSettings,
      credential_bindings: this.currentSettings.credential_bindings.filter(
        (binding) => binding.binding_id !== connection?.api_key_env,
      ),
    };
    return Promise.resolve({
      store: this.currentSettings.credential_store,
      bindings: this.currentSettings.credential_bindings,
    });
  }

  startSubscriptionDeviceLogin(
    connectionId: string,
  ): Promise<SubscriptionDeviceLogin> {
    return Promise.resolve({
      schema_version: "heartwood.subscription-login.v1",
      login_id: "login-test",
      connection_id: connectionId,
      verification_url: "https://auth.openai.test/device",
      user_code: "TEST-CODE",
      poll_interval_seconds: 1,
      status: "pending",
    });
  }

  pollSubscriptionDeviceLogin(
    connectionId: string,
    loginId: string,
  ): Promise<SubscriptionDeviceLogin> {
    this.subscriptionPolls += 1;
    const complete = this.subscriptionPolls > 1;
    if (complete) {
      this.currentSettings = {
        ...this.currentSettings,
        connections: this.currentSettings.connections.map((connection) =>
          connection.connection_id === connectionId ?
            { ...connection, credential_status: "available" }
          : connection,
        ),
      };
    }
    return Promise.resolve({
      schema_version: "heartwood.subscription-login.v1",
      login_id: loginId,
      connection_id: connectionId,
      verification_url: "https://auth.openai.test/device",
      user_code: "TEST-CODE",
      poll_interval_seconds: 1,
      status: complete ? "complete" : "pending",
    });
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
      auth_type: connection.auth_type,
      subscription_vendor: connection.subscription_vendor,
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
        active_profile: "heartwood",
        model_source: "heartwood",
        profiles: [
          {
            ...localProfile(),
            model: "openai/heartwood-managed-model",
            description: "Stories 260K",
          },
        ],
        source_options: this.currentSettings.source_options.map((source) => ({
          ...source,
          selected: source.source_id === "heartwood",
        })),
      };
    }
    return Promise.resolve({
      schema_version: "heartwood.local-model-catalog.v2",
      snapshot_schema_version: "heartwood.model-snapshot-catalog.v3",
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
          minimum_free_bytes: 256 * 1024 * 1024,
          artifact_sha256: "a".repeat(64),
          license_posture: "Test fixture",
          model_alias: "Stories 260K",
          context_window: 32_768,
          minimum_resource_envelope: null,
          recommended_resource_envelope: null,
          qualification: "qualified",
          validated_platforms: ["generic"],
          qualification_test: "synthetic-browser-e2e-v1",
          qualification_date: "2026-07-22",
          qualification_evidence: "https://example.test/qualification",
          recommended: true,
        },
      ],
      snapshots: [],
      models: [
        localModelChoice({
          active: this.activeManagedModel,
          selected: this.currentSettings.model_source === "heartwood",
        }),
        ...(this.customModel === null ? [] : [this.customModel]),
      ],
      downloads: this.currentDownloads,
      gpu_environment: {
        platform_id: "generic",
        capacities: [],
      },
    });
  }

  inspectModelRepository(
    request: ModelRepositoryRequest,
  ): Promise<ModelRepositoryPlan> {
    this.inspectedRepository = request;
    if (this.repositoryError) return Promise.reject(this.repositoryError);
    const candidate = localModelChoice({
      model_id: "hf-research-model-123456789abc",
      label: "Research Model Q4_K_M",
      purpose: "User-selected Hugging Face model.",
      source_repository: request.repository,
      source_revision: "1".repeat(40),
      source_path: "research-model-q4_k_m.gguf",
      size_bytes: 4 * 1024 * 1024 * 1024,
      minimum_free_bytes: 4 * 1024 * 1024 * 1024,
      license_id: "Apache-2.0",
      license_posture: "Source model card reports apache-2.0.",
      catalog_source: "user-selected",
      artifact_sha256: "b".repeat(64),
      minimum_resource_envelope:
        "Estimated minimum: 4 CPU cores and 12 GB RAM.",
      recommended_resource_envelope: "Recommended: 8 CPU cores and 16 GB RAM.",
    });
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
    const customModel = localModelChoice({
      model_id: "hf-research-model-123456789abc",
      label: "Research Model Q4_K_M",
      purpose: "User-selected Hugging Face model.",
      source_repository: request.repository,
      source_revision: request.revision ?? "1".repeat(40),
      source_path: "research-model-q4_k_m.gguf",
      size_bytes: 4 * 1024 * 1024 * 1024,
      minimum_free_bytes: 4 * 1024 * 1024 * 1024,
      license_id: "Apache-2.0",
      license_posture: "Source model card reports apache-2.0.",
      catalog_source: "user-selected",
      artifact_sha256: "b".repeat(64),
      minimum_resource_envelope:
        "Estimated minimum: 4 CPU cores and 12 GB RAM.",
      recommended_resource_envelope: "Recommended: 8 CPU cores and 16 GB RAM.",
    });
    this.customModel = customModel;
    const download: ModelDownload = {
      model_id: customModel.model_id,
      status: "downloading",
      bytes_downloaded: 0,
      bytes_total: customModel.size_bytes,
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

  saveModelProfile(profile: ModelProfileDraft): Promise<ModelSettings> {
    this.savedProfile = profile;
    this.currentSettings = {
      ...this.currentSettings,
      profiles: [profileResponseFromDraft(profile)],
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
        schema_version: "heartwood.model-call-decision.v1",
        decision_id: "decision-test",
        policy_profile_id: "policy-test",
        decision: "allow",
        endpoint: "http://127.0.0.1:8765/v1/chat/completions",
        capability_tier: "supervised",
        reason: "allowlisted",
      },
    });
  }
}

class DeferredCommandClient extends FakeClient {
  private complete: ((response: SessionProjectionResponse) => void) | null =
    null;
  private pendingSessionId: string | null = null;

  override postCommand(
    command: SessionCommand,
  ): Promise<SessionProjectionResponse> {
    this.commands.push(command);
    this.pendingSessionId = command.session_id;
    return new Promise((resolve) => {
      this.complete = resolve;
    });
  }

  completeCommand(projection?: SessionProjection): void {
    const sessionId = this.pendingSessionId ?? "session-test";
    this.complete?.({
      events: [],
      projection: projection ?? this.projectionFor(sessionId),
    });
    this.complete = null;
    this.pendingSessionId = null;
  }
}

class DeferredModelImportClient extends FakeClient {
  private releaseImport: (() => void) | null = null;

  override async importLocalModel(
    request: LocalModelImportRequest,
  ): Promise<LocalModelImportResult> {
    this.localImportRequest = request;
    await new Promise<void>((resolve) => {
      this.releaseImport = resolve;
    });
    return super.importLocalModel(request);
  }

  completeImport(): void {
    this.releaseImport?.();
    this.releaseImport = null;
  }
}

class DeferredActivityClient extends FakeClient {
  private deferReplay = false;
  private completeReplay:
    ((response: SessionProjectionResponse) => void) | null = null;

  deferNextReplay(): void {
    this.deferReplay = true;
  }

  override replayEvents(sessionId: string): Promise<SessionProjectionResponse> {
    this.replayCalls += 1;
    if (!this.deferReplay) {
      return Promise.resolve({
        events: [],
        projection: this.projectionFor(sessionId),
      });
    }
    this.deferReplay = false;
    return new Promise((resolve) => {
      this.completeReplay = resolve;
    });
  }

  completeDeferredReplay(projection: SessionProjection): void {
    this.completeReplay?.({ events: [], projection });
    this.completeReplay = null;
  }
}

describe("App", () => {
  it("waits for explicit project confirmation before creating a session", async () => {
    const client = new FirstRunClient();
    render(<App client={client} />);

    expect(
      await screen.findByRole("button", { name: "Use this project" }),
    ).toBeInTheDocument();
    expect(client.currentSessions).toHaveLength(0);
    expect(
      screen.queryByRole("heading", { name: "Synthetic analysis" }),
    ).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Use this project" }));

    await waitFor(() => expect(client.initialized).toBe(true));
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(
      await screen.findByRole("heading", { name: "Main session" }),
    ).toBeInTheDocument();
    expect(client.currentSessions).toHaveLength(1);
  });

  it("opens first-run setup and configures a shared research model source", async () => {
    const client = new FakeClient();
    client.currentReadiness = projectReadiness("setup-required");
    render(<App client={client} initialSessionId="session-test" />);

    expect(
      await screen.findByRole("heading", { name: "Set up Heartwood" }),
    ).toBeInTheDocument();
    expect(screen.getAllByText("synthetic-analysis")).toHaveLength(2);
    const stanford = screen
      .getByText("Stanford AI API Gateway")
      .closest(".connection-row");
    expect(stanford).not.toBeNull();
    fireEvent.click(
      within(stanford as HTMLElement).getByRole("button", { name: "Set up" }),
    );
    fireEvent.change(await screen.findByLabelText("API key"), {
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

  it("ignores a delayed command response after selecting another session", async () => {
    const client = new DeferredCommandClient();
    client.currentSettings = {
      ...settings(),
      active_profile: "heartwood",
      profiles: [localProfile()],
    };
    client.currentSessions = [
      sessionSummary("session-test", "First analysis"),
      sessionSummary("session-second", "Second analysis"),
    ];
    render(<App client={client} initialSessionId="session-test" />);
    await screen.findByRole("heading", { name: "First analysis" });
    fireEvent.change(screen.getByLabelText("Task"), {
      target: { value: "Run the first analysis" },
    });
    fireEvent.click(screen.getByLabelText("Send task"));
    await waitFor(() => expect(client.commands).toHaveLength(1));

    fireEvent.click(screen.getByRole("button", { name: /Second analysis/u }));
    await screen.findByRole("heading", { name: "Second analysis" });
    expect(screen.getByLabelText("Task")).toBeDisabled();
    const delayedProjection = {
      ...client.projectionFor("session-test"),
      eventCount: 1,
      revision: 0,
      conversation: [
        {
          id: "delayed-first-session",
          sequence: 0,
          role: "agent" as const,
          label: "Agent",
          content: "Delayed result from the first session",
          detail: null,
          technicalDetail: null,
        },
      ],
    };
    await act(async () => {
      client.completeCommand(delayedProjection);
      await Promise.resolve();
    });

    expect(
      screen.getByRole("heading", { name: "Second analysis" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("Delayed result from the first session"),
    ).not.toBeInTheDocument();
    expect(screen.getByLabelText("Task")).toBeEnabled();
  });

  it("ignores a retired stream after selecting another session", async () => {
    const client = new FakeClient();
    client.currentSessions = [
      sessionSummary("session-test", "First analysis"),
      sessionSummary("session-second", "Second analysis"),
    ];
    render(<App client={client} initialSessionId="session-test" />);
    await screen.findByRole("heading", { name: "First analysis" });

    fireEvent.click(screen.getByRole("button", { name: /Second analysis/u }));
    await screen.findByRole("heading", { name: "Second analysis" });
    act(() => {
      client.emitRetiredStream({
        ...client.projectionFor("session-test"),
        eventCount: 1,
        revision: 0,
        conversation: [
          {
            id: "retired-first-session",
            sequence: 0,
            role: "agent",
            label: "Agent",
            content: "Retired first-session stream",
            detail: null,
            technicalDetail: null,
          },
        ],
      });
    });

    expect(
      screen.getByRole("heading", { name: "Second analysis" }),
    ).toBeInTheDocument();
    expect(screen.queryByText("Retired first-session stream")).toBeNull();
  });

  it("rejects a delayed activity refresh after an A-B-A session handoff", async () => {
    const client = new DeferredActivityClient();
    client.currentSessions = [
      sessionSummary("session-test", "First analysis"),
      sessionSummary("session-second", "Second analysis"),
    ];
    render(<App client={client} initialSessionId="session-test" />);
    await screen.findByRole("heading", { name: "First analysis" });
    const current = syntheticProjection({
      sessionId: "session-test",
      streamEpoch: "current-process",
      streamRevision: 1,
      streamingText: "Current response",
      lifecycle: {
        status: "running",
        canPause: true,
        canResume: false,
        canSteer: true,
      },
      availableCommands: ["chat", "pause"],
    });
    act(() => client.emitStream(current));

    client.deferNextReplay();
    fireEvent.click(screen.getByRole("button", { name: "Activity & audit" }));
    fireEvent.click(await screen.findByRole("button", { name: "Refresh" }));
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    fireEvent.click(screen.getByRole("button", { name: /Second analysis/u }));
    await screen.findByRole("heading", { name: "Second analysis" });
    fireEvent.click(screen.getByRole("button", { name: /First analysis/u }));
    await screen.findByRole("heading", { name: "First analysis" });

    await act(async () => {
      client.completeDeferredReplay({
        ...current,
        streamEpoch: "delayed-process",
        streamRevision: 7,
        streamingText: "Delayed stale refresh",
      });
      await Promise.resolve();
    });
    act(() => {
      client.emitStream({
        ...current,
        streamRevision: 2,
        streamingText: "Latest current response",
      });
    });

    expect(screen.queryByText("Delayed stale refresh")).not.toBeInTheDocument();
    expect(
      screen.getByLabelText("Agent response in progress"),
    ).toHaveTextContent("Latest current response");
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

  it("renders the shared project and platform context", async () => {
    const client = new FakeClient();
    render(<App client={client} initialSessionId="session-test" />);

    await screen.findByRole("heading", { name: "Synthetic analysis" });
    expect(screen.getByText("synthetic-analysis")).toBeInTheDocument();
    expect(screen.getByText("Workstation or container")).toBeInTheDocument();
    expect(
      screen.queryByLabelText("Detect environment"),
    ).not.toBeInTheDocument();
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
      active_profile: "heartwood",
      profiles: [localProfile()],
    };
    render(<App client={client} initialSessionId="session-test" />);
    await waitFor(() => expect(client.replayCalls).toBe(1));
    await waitFor(() => expect(screen.getByLabelText("Task")).toBeEnabled());

    const task = screen.getByLabelText("Task");
    fireEvent.change(task, {
      target: { value: "Inspect the synthetic cohort" },
    });
    fireEvent.keyDown(task, { key: "Enter", shiftKey: true });
    expect(client.commands).toEqual([]);
    fireEvent.keyDown(task, { key: "Enter", shiftKey: false });

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
      active_profile: "heartwood",
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
      active_profile: "heartwood",
      model_source: "heartwood",
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
      active_profile: "heartwood",
      model_source: "heartwood",
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

  it("coalesces session refreshes for streamed projection updates", async () => {
    const client = new FakeClient();
    render(<App client={client} initialSessionId="session-test" />);
    await screen.findByRole("heading", { name: "Synthetic analysis" });
    const initialListCalls = client.listCalls;

    act(() => {
      client.emitStream(
        syntheticProjection({
          eventCount: 7,
          revision: 6,
          pendingApproval: null,
        }),
      );
      client.emitStream(
        syntheticProjection({
          eventCount: 8,
          revision: 7,
          pendingApproval: null,
        }),
      );
    });

    await waitFor(() => expect(client.listCalls).toBe(initialListCalls + 1));
  });

  it("does not replace a newer token frame with an older equal-revision response", async () => {
    const client = new FakeClient();
    render(<App client={client} initialSessionId="session-test" />);
    await screen.findByRole("heading", { name: "Synthetic analysis" });
    const running: SessionProjection = {
      ...emptyProjection(),
      eventCount: 7,
      revision: 6,
      streamRevision: 2,
      lifecycle: {
        status: "running",
        canPause: true,
        canResume: false,
        canSteer: true,
      },
      streamingText: "Current streamed response",
      availableCommands: ["chat", "pause"],
    };

    act(() => {
      client.emitStream(running);
      client.emitStream({
        ...running,
        streamRevision: 1,
        streamingText: "",
      });
    });

    expect(
      screen.getByLabelText("Agent response in progress"),
    ).toHaveTextContent("Current streamed response");
  });

  it("accepts an authoritative projection from a restarted stream epoch", async () => {
    const client = new FakeClient();
    render(<App client={client} initialSessionId="session-test" />);
    await screen.findByRole("heading", { name: "Synthetic analysis" });
    const running = syntheticProjection({
      streamEpoch: "first-process",
      streamRevision: 4,
      lifecycle: {
        status: "running",
        canPause: true,
        canResume: false,
        canSteer: true,
      },
      streamingText: "Stale partial response",
      availableCommands: ["chat", "pause"],
    });

    act(() => {
      client.emitStream(running);
      client.emitStream({
        ...running,
        streamEpoch: "restarted-process",
        streamRevision: 0,
        streamingText: "Fresh response after restart",
      });
      client.emitStream({
        ...running,
        streamRevision: 5,
        streamingText: "Delayed stale response",
      });
    });

    expect(
      screen.queryByText("Stale partial response", { exact: true }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText("Delayed stale response", { exact: true }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByLabelText("Agent response in progress"),
    ).toHaveTextContent("Fresh response after restart");
  });

  it("renders gateway-owned lifecycle, streaming, task, usage, and specialist state", async () => {
    const client = new FakeClient();
    client.currentSettings = {
      ...settings(),
      active_profile: "heartwood",
      profiles: [localProfile()],
    };
    render(<App client={client} initialSessionId="session-test" />);
    await waitFor(() => expect(screen.getByLabelText("Task")).toBeEnabled());

    act(() => {
      client.emitStream({
        ...emptyProjection(),
        eventCount: 4,
        revision: 3,
        lifecycle: {
          status: "running",
          canPause: true,
          canResume: false,
          canSteer: true,
        },
        taskPlan: [
          {
            title: "Inspect the analysis",
            status: "in-progress",
          },
          { title: "Verify the result", status: "todo" },
        ],
        usage: {
          usageId: "total",
          modelName: "openai/synthetic-coder",
          callCount: 2,
          promptTokens: 1200,
          completionTokens: 300,
          cacheReadTokens: 0,
          cacheWriteTokens: 0,
          reasoningTokens: 0,
          contextWindow: 32768,
          accumulatedCost: 0,
        },
        usageByPurpose: [
          {
            usageId: "agent",
            modelName: "openai/synthetic-coder",
            callCount: 2,
            promptTokens: 1200,
            completionTokens: 300,
            cacheReadTokens: 0,
            cacheWriteTokens: 0,
            reasoningTokens: 0,
            contextWindow: 32768,
            accumulatedCost: 0,
          },
        ],
        subagents: [
          {
            invocationId: "task-research-plan",
            taskId: "task-research-plan",
            agentName: "research-planner",
            status: "running",
            parentSessionId: "session-test",
            parentActionId: "task-action-1",
          },
        ],
        streamingText: "Reviewing the analysis structure",
        availableCommands: ["chat", "pause"],
      });
    });

    expect(
      screen.getByLabelText("Agent response in progress"),
    ).toHaveTextContent("Reviewing the analysis structure");
    const status = screen.getByRole("status", { name: "Agent status" });
    expect(status).toHaveTextContent("Heartwood is working");
    expect(status).toHaveTextContent("Plan: 0 of 2 complete");
    expect(status).toHaveTextContent("1,500 tokens · openai/synthetic-coder");
    expect(status).toHaveTextContent("2 calls");
    expect(status).toHaveTextContent("agent");
    expect(status).toHaveTextContent("research-planner (running)");
    expect(status).toHaveTextContent(
      "Parent session session-test · action task-action-1",
    );
    expect(screen.getByLabelText("Task")).toBeEnabled();
    expect(screen.getByLabelText("Send guidance")).toBeInTheDocument();
    expect(screen.getByLabelText("Pause agent")).toBeEnabled();

    act(() => {
      const current = client.projectionFor("session-test");
      client.emitStream({
        ...current,
        revision: 4,
        usage:
          current.usage === null ?
            null
          : {
              ...current.usage,
              contextWindow: null,
              accumulatedCost: 1.25,
            },
        subagents: [
          ...current.subagents,
          {
            invocationId: "task-verification",
            taskId: "task-verification",
            agentName: "result-reviewer",
            status: "proposed",
            parentSessionId: "session-test",
            parentActionId: "task-action-2",
          },
        ],
      });
    });
    await waitFor(() => expect(status).toHaveTextContent("$1.25"));
    expect(status).toHaveTextContent("2 specialists");
  });

  it("uses projection capabilities for paused work and resume commands", async () => {
    const client = new FakeClient();
    client.currentSettings = {
      ...settings(),
      active_profile: "heartwood",
      profiles: [localProfile()],
    };
    client.projections.set("session-test", {
      ...emptyProjection(),
      lifecycle: {
        status: "paused",
        canPause: false,
        canResume: true,
        canSteer: true,
      },
      availableCommands: ["chat", "resume"],
      paused: true,
    });
    render(<App client={client} initialSessionId="session-test" />);

    const status = await screen.findByRole("status", { name: "Agent status" });
    expect(status).toHaveTextContent("Agent paused");
    expect(status).not.toHaveTextContent("Plan:");
    const resume = screen.getByLabelText("Resume agent");
    expect(resume).toBeEnabled();
    fireEvent.click(resume);

    await waitFor(() => expect(client.commands.at(-1)?.kind).toBe("resume"));
  });

  it("renders the pending OpenHands action set and sends one batch decision", async () => {
    const client = new PendingClient();
    render(<App client={client} initialSessionId="session-test" />);

    const allow = await screen.findByLabelText("Allow 1 action once");
    const argumentsRegion = screen.getByLabelText(
      "Arguments for Terminal Command",
    );
    expect(argumentsRegion).toHaveAttribute("tabindex", "0");
    expect(argumentsRegion).toHaveTextContent(
      "python run.py --output /project/cohort-summary.json",
    );
    fireEvent.click(allow);

    await waitFor(() => expect(client.commands.at(-1)?.kind).toBe("approve"));
    expect(client.commands.at(-1)?.payload).toEqual({
      target_id: "action-set-session-test",
      target_type: "action-set",
    });
  });

  it("disables grouped decisions that the projection does not allow", async () => {
    const client = new FakeClient();
    client.projections.set(
      "session-test",
      syntheticProjection({ availableCommands: [] }),
    );
    render(<App client={client} initialSessionId="session-test" />);

    expect(await screen.findByLabelText("Allow 1 action once")).toBeDisabled();
    expect(screen.getByLabelText("Reject 1 action")).toBeDisabled();
  });

  it("disables approval-mode changes while session work is active", async () => {
    const client = new FakeClient();
    client.currentActions = {
      ...actions(),
      change_allowed: false,
      change_blocked_reason:
        "Finish or resolve active session work before changing approvals.",
    };
    render(<App client={client} initialSessionId="session-test" />);
    fireEvent.click(screen.getByRole("button", { name: "Settings" }));
    await screen.findByRole("heading", { name: "Settings" });
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Action Review" }), {
      button: 0,
    });

    expect(
      await screen.findByText(
        "Finish or resolve active session work before changing approvals.",
      ),
    ).toBeVisible();
    const mode = screen.getByRole("radio", { name: /Low-Risk Automation/u });
    expect(mode).toBeDisabled();
  });

  it("honors the action-mode change gate without requiring a reason", async () => {
    const client = new FakeClient();
    client.currentActions = {
      ...actions(),
      change_allowed: false,
      change_blocked_reason: null,
    };
    render(<App client={client} initialSessionId="session-test" />);
    fireEvent.click(screen.getByRole("button", { name: "Settings" }));
    await screen.findByRole("heading", { name: "Settings" });
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Action Review" }), {
      button: 0,
    });

    expect(
      screen.getByRole("radio", { name: /Low-Risk Automation/u }),
    ).toBeDisabled();
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
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Action Review" }), {
      button: 0,
    });
    fireEvent.click(
      screen.getByRole("radio", { name: /Low-Risk Automation/u }),
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
      target: { value: "heartwood-managed" },
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
    expect(client.downloadedArtifact).toBeNull();
    fireEvent.click(
      within(
        screen.getByRole("dialog", { name: "Download Stories 260K?" }),
      ).getByRole("button", {
        name: "Download model",
      }),
    );
    await waitFor(() => expect(client.downloadedArtifact).toBe("stories260k"));
    const progress = await screen.findByRole("progressbar", {
      name: "Download progress for Stories 260K",
    });
    expect(progress).toHaveAttribute("aria-valuenow", String(64 * 1024 * 1024));
    expect(
      await screen.findByText(
        "Downloaded. Restart Heartwood to load this model.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("Model runtime needed")).toBeInTheDocument();
    expect(screen.getByLabelText("Active model profile")).toHaveTextContent(
      "Run with Heartwood · Stories 260K",
    );
    expect(screen.getByLabelText("Task")).toBeDisabled();
    expect(
      screen.getByText(
        "Restart with heartwood --interface web to start the selected Heartwood-managed model.",
      ),
    ).toBeInTheDocument();
  });

  it("signs in with ChatGPT through the OpenHands device flow", async () => {
    vi.useFakeTimers();
    try {
      const client = new FakeClient();
      render(<App client={client} initialSessionId="session-test" />);
      await act(async () => Promise.resolve());
      fireEvent.click(screen.getByRole("button", { name: "Settings" }));

      const connection = screen
        .getByText("Sign in with ChatGPT")
        .closest(".connection-row");
      expect(connection).not.toBeNull();
      fireEvent.click(
        within(connection as HTMLElement).getByRole("button", {
          name: "Sign in",
        }),
      );
      const form = screen
        .getAllByText("Sign in with ChatGPT")
        .at(-1)
        ?.closest(".connection-form");
      expect(form).not.toBeNull();
      fireEvent.click(
        within(form as HTMLElement).getByRole("button", {
          name: "Sign in with ChatGPT",
        }),
      );
      await act(async () => Promise.resolve());
      expect(screen.getByText("TEST-CODE")).toBeVisible();
      expect(
        screen.getByRole("link", { name: "Open ChatGPT sign-in" }),
      ).toHaveAttribute("href", "https://auth.openai.test/device");

      await act(async () => {
        vi.advanceTimersByTime(500);
        const current = client.projectionFor("session-test");
        client.emitStream({
          ...current,
          eventCount: 8,
          revision: 7,
          conversation: [
            ...current.conversation,
            {
              id: "session-test-event-000007-agent",
              sequence: 7,
              role: "agent",
              label: "Agent",
              content: "Session update",
              detail: null,
              technicalDetail: null,
            },
          ],
        });
        await Promise.resolve();
        vi.advanceTimersByTime(600);
        await Promise.resolve();
      });
      expect(client.subscriptionPolls).toBe(1);
      expect(screen.getByText("Waiting for sign-in...")).toBeVisible();

      await act(async () => {
        vi.advanceTimersByTime(1_100);
        await Promise.resolve();
      });
      expect(client.subscriptionPolls).toBe(2);
      expect(screen.getByText("Signed in with ChatGPT")).toBeVisible();

      await act(async () => {
        vi.advanceTimersByTime(2_000);
        await Promise.resolve();
      });
      expect(client.subscriptionPolls).toBe(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it("ignores a device-login poll after the connection changes", async () => {
    vi.useFakeTimers();
    try {
      const client = new FakeClient();
      let resolvePoll: ((login: SubscriptionDeviceLogin) => void) | undefined;
      vi.spyOn(client, "pollSubscriptionDeviceLogin").mockImplementation(
        (connectionId, loginId) =>
          new Promise((resolve) => {
            resolvePoll = resolve;
            expect(connectionId).toBe("openai-subscription");
            expect(loginId).toBe("login-test");
          }),
      );
      render(<App client={client} initialSessionId="session-test" />);
      await act(async () => Promise.resolve());
      fireEvent.click(screen.getByRole("button", { name: "Settings" }));

      const subscriptionRow = screen
        .getByText("Sign in with ChatGPT")
        .closest(".connection-row");
      expect(subscriptionRow).not.toBeNull();
      fireEvent.click(
        within(subscriptionRow as HTMLElement).getByRole("button", {
          name: "Sign in",
        }),
      );
      const subscriptionForm = screen
        .getAllByText("Sign in with ChatGPT")
        .at(-1)
        ?.closest<HTMLElement>(".connection-form");
      expect(subscriptionForm).not.toBeNull();
      if (!subscriptionForm)
        throw new Error("subscription form is unavailable");
      fireEvent.click(
        within(subscriptionForm).getByRole("button", {
          name: "Sign in with ChatGPT",
        }),
      );
      await act(async () => Promise.resolve());
      await act(async () => {
        vi.advanceTimersByTime(1_100);
        await Promise.resolve();
      });
      expect(resolvePoll).toBeDefined();

      const openAiRow = screen
        .getByText("OpenAI API")
        .closest(".connection-row");
      expect(openAiRow).not.toBeNull();
      fireEvent.click(
        within(openAiRow as HTMLElement).getByRole("button", {
          name: "Connect",
        }),
      );
      fireEvent.click(
        within(subscriptionRow as HTMLElement).getByRole("button", {
          name: "Sign in",
        }),
      );

      await act(async () => {
        resolvePoll?.({
          schema_version: "heartwood.subscription-login.v1",
          login_id: "login-test",
          connection_id: "openai-subscription",
          verification_url: "https://auth.openai.test/device",
          user_code: "TEST-CODE",
          poll_interval_seconds: 1,
          status: "complete",
        });
        await Promise.resolve();
      });

      expect(screen.queryByText(/^Signed in$/)).not.toBeInTheDocument();
      expect(
        screen.getByRole("button", { name: "Sign in with ChatGPT" }),
      ).toBeVisible();
    } finally {
      vi.useRealTimers();
    }
  });

  it("opens the shared action review setting from the session header", async () => {
    const client = new FakeClient();
    render(<App client={client} initialSessionId="session-test" />);

    await screen.findByRole("heading", { name: "Synthetic analysis" });
    const actionReview = await screen.findByLabelText(
      "Open action review settings",
    );
    await waitFor(() =>
      expect(actionReview).toHaveTextContent("Review Every Action"),
    );
    fireEvent.click(actionReview);

    expect(
      await screen.findByRole("tab", { name: "Action Review" }),
    ).toHaveAttribute("aria-selected", "true");
    expect(
      screen.getByText(
        "Shared by every Heartwood interface in this project and applied to future action sets.",
      ),
    ).toBeVisible();
    expect(
      screen.getByRole("radio", { name: /Review Every Action/u }),
    ).toBeChecked();
  });

  it("uses a transient cloud token to discover and select a model", async () => {
    const client = new FakeClient();
    render(<App client={client} initialSessionId="session-test" />);
    fireEvent.click(screen.getByRole("button", { name: "Settings" }));

    const openAiConnection = (await screen.findByText("OpenAI API")).closest(
      ".connection-row",
    );
    expect(openAiConnection).not.toBeNull();
    fireEvent.click(
      within(openAiConnection as HTMLElement).getByRole("button", {
        name: "Connect",
      }),
    );
    fireEvent.change(await screen.findByLabelText("API key"), {
      target: { value: "runtime-only-token" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Load models" }));
    await screen.findByLabelText("Models available from OpenAI API");
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
    expect(screen.getByLabelText("API key")).toHaveValue("");
    expect(JSON.stringify(client.currentSettings)).not.toContain(
      "runtime-only-token",
    );
  });

  it("remembers and forgets a provider token only after explicit choices", async () => {
    const client = new FakeClient();
    client.currentSettings = {
      ...client.currentSettings,
      credential_bindings: [
        {
          binding_id: "OPENAI_API_KEY",
          configured: true,
          source: "keyring",
          error: null,
        },
      ],
    };
    render(<App client={client} initialSessionId="session-test" />);
    fireEvent.click(screen.getByRole("button", { name: "Settings" }));

    const openAiConnection = (await screen.findByText("OpenAI API")).closest(
      ".connection-row",
    );
    expect(openAiConnection).not.toBeNull();
    fireEvent.click(
      within(openAiConnection as HTMLElement).getByRole("button", {
        name: "Connect",
      }),
    );
    fireEvent.change(await screen.findByLabelText("API key"), {
      target: { value: "remembered-token" },
    });
    fireEvent.click(
      screen.getByRole("checkbox", {
        name: "Remember securely for this project",
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Load models" }));

    await waitFor(() =>
      expect(client.catalogRequest).toEqual({
        connection_id: "openai",
        token: "remembered-token",
        refresh: true,
        remember: true,
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Forget API key" }));
    await waitFor(() =>
      expect(client.currentSettings.credential_bindings).toHaveLength(0),
    );
    expect(JSON.stringify(client.currentSettings)).not.toContain(
      "remembered-token",
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
    fireEvent.click(screen.getByText("Version options"));
    fireEvent.change(screen.getByLabelText("Model revision"), {
      target: { value: "reviewed-release" },
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
    expect(modelPlan).toHaveTextContent("Up to 32,768 tokens");
    expect(
      within(modelPlan as HTMLElement).getByText(`Revision: ${"1".repeat(40)}`),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Download model" }));
    expect(client.customDownloadRequest).toBeNull();
    fireEvent.click(
      within(
        screen.getByRole("dialog", { name: "Download Research Model Q4_K_M?" }),
      ).getByRole("button", {
        name: "Download model",
      }),
    );

    await waitFor(() =>
      expect(client.customDownloadRequest).toEqual({
        repository: "research/research-model-gguf",
        revision: "1".repeat(40),
      }),
    );
    expect(client.inspectedRepository).toEqual({
      repository: "research/research-model-gguf",
      revision: "reviewed-release",
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

  it("imports an existing managed model with explicit provenance", async () => {
    const client = new FakeClient();
    render(<App client={client} initialSessionId="session-test" />);
    fireEvent.click(screen.getByRole("button", { name: "Settings" }));
    fireEvent.click(await screen.findByText("Other model"));
    fireEvent.click(screen.getByText("Import an existing model"));
    fireEvent.change(screen.getByLabelText("Server path"), {
      target: { value: "/models/research-model.gguf" },
    });
    fireEvent.change(screen.getByLabelText("Source repository"), {
      target: { value: "research/research-model" },
    });
    fireEvent.change(screen.getByLabelText("Source revision"), {
      target: { value: "2".repeat(40) },
    });
    fireEvent.change(screen.getByLabelText("License"), {
      target: { value: "Apache-2.0" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Import model" }));

    await waitFor(() =>
      expect(client.localImportRequest).toEqual({
        path: "/models/research-model.gguf",
        repository: "research/research-model",
        revision: "2".repeat(40),
        license: "Apache-2.0",
      }),
    );
    expect(
      await screen.findByText("Model imported and selected"),
    ).toHaveAttribute("role", "status");
  });

  it("keeps a long-running model import visibly pending", async () => {
    const client = new DeferredModelImportClient();
    render(<App client={client} initialSessionId="session-test" />);
    fireEvent.click(screen.getByRole("button", { name: "Settings" }));
    fireEvent.click(await screen.findByText("Other model"));
    fireEvent.click(screen.getByText("Import an existing model"));
    fireEvent.change(screen.getByLabelText("Server path"), {
      target: { value: "/models/research-model.gguf" },
    });
    fireEvent.change(screen.getByLabelText("Source repository"), {
      target: { value: "research/research-model" },
    });
    fireEvent.change(screen.getByLabelText("Source revision"), {
      target: { value: "2".repeat(40) },
    });
    fireEvent.change(screen.getByLabelText("License"), {
      target: { value: "Apache-2.0" },
    });
    const importButton = screen.getByRole("button", { name: "Import model" });
    fireEvent.click(importButton);

    await waitFor(() => expect(client.localImportRequest).not.toBeNull());
    expect(importButton).toBeDisabled();
    expect(
      screen.getByRole("button", { name: "Check model" }),
    ).not.toHaveAttribute("aria-busy", "true");
    expect(
      screen.getByText(
        "Copying and verifying model files. Keep this page open.",
      ),
    ).toHaveAttribute("role", "status");
    expect(
      screen.queryByText("Model imported and selected"),
    ).not.toBeInTheDocument();

    act(() => client.completeImport());

    expect(
      await screen.findByText("Model imported and selected"),
    ).toHaveAttribute("role", "status");
    expect(importButton).not.toBeDisabled();
  });

  it("labels user-selected models without qualification evidence", async () => {
    const client = new FakeClient();
    client.customModel = localModelChoice({
      model_id: "user-selected-model",
      label: "User-selected model",
      qualification: "unvalidated",
      validated_platforms: [],
      qualification_date: null,
      qualification_evidence: null,
      recommended: false,
    });
    render(<App client={client} initialSessionId="session-test" />);
    fireEvent.click(screen.getByRole("button", { name: "Settings" }));
    fireEvent.click(
      await screen.findByText("Models not qualified for this platform"),
    );
    fireEvent.click(
      screen.getByLabelText("Review download for User-selected model"),
    );

    expect(screen.getByText("Not tested")).toBeInTheDocument();
  });

  it("continues polling a model download after a transient status failure", async () => {
    const client = new FakeClient();
    render(<App client={client} initialSessionId="session-test" />);
    fireEvent.click(screen.getByRole("button", { name: "Settings" }));
    await screen.findByLabelText("Download Stories 260K");
    client.artifactFailures = 1;

    fireEvent.click(screen.getByLabelText("Download Stories 260K"));
    fireEvent.click(
      within(
        screen.getByRole("dialog", { name: "Download Stories 260K?" }),
      ).getByRole("button", {
        name: "Download model",
      }),
    );

    expect(
      await screen.findByText("temporary model status failure"),
    ).toBeInTheDocument();
    expect(
      await screen.findByText(
        "Downloaded. Restart Heartwood to load this model.",
        {},
        { timeout: 5_000 },
      ),
    ).toBeInTheDocument();
    expect(client.artifactCalls).toBeGreaterThanOrEqual(3);
    expect(
      screen.queryByText("temporary model status failure"),
    ).not.toBeInTheDocument();
  });

  it("reports only the loaded managed model as running", async () => {
    const client = new FakeClient();
    client.activeManagedModel = true;
    client.currentDownloads = [
      {
        model_id: "stories260k",
        status: "ready",
        bytes_downloaded: 256 * 1024 * 1024,
        bytes_total: 256 * 1024 * 1024,
        path: "/models/stories260k/model.gguf",
        error: null,
      },
    ];
    render(<App client={client} initialSessionId="session-test" />);

    fireEvent.click(screen.getByRole("button", { name: "Settings" }));

    expect(
      await screen.findByText("Downloaded and running."),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("Downloaded. Restart Heartwood to load this model."),
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

    const customConnection = (
      await screen.findByText("Other compatible service")
    ).closest(".connection-row");
    expect(customConnection).not.toBeNull();
    fireEvent.click(
      within(customConnection as HTMLElement).getByRole("button", {
        name: "Connect",
      }),
    );
    fireEvent.change(screen.getByLabelText("Server URL"), {
      target: { value: "https://models.example/v1" },
    });
    fireEvent.change(
      screen.getByLabelText("API key (optional for loopback services)"),
      {
        target: { value: "runtime-only-token" },
      },
    );
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
        unavailable_reason:
          option.mode === "always-confirm" ?
            null
          : "Unavailable under the active platform policy.",
      })),
    };
    render(<App client={client} initialSessionId="session-test" />);

    fireEvent.click(screen.getByRole("button", { name: "Settings" }));
    fireEvent.mouseDown(
      await screen.findByRole("tab", { name: "Action Review" }),
      { button: 0 },
    );

    expect(
      await screen.findByText("Low-Risk Automation", { exact: true }),
    ).toBeVisible();
    expect(
      screen.queryByRole("radio", { name: /Low-Risk Automation/u }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByText("Unavailable under the active platform policy."),
    ).toBeVisible();
  });

  it("keeps setup incomplete when the selected credential is unavailable", async () => {
    const client = new FakeClient();
    client.currentSettings = {
      ...settings(),
      active_profile: "heartwood",
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

  it("uses shared compute readiness before a launch materializes the managed profile", async () => {
    const client = new FakeClient();
    client.currentSettings = {
      ...settings(),
      model_source: "heartwood",
    };
    client.currentReadiness = projectReadiness("compute-required");

    render(<App client={client} initialSessionId="session-test" />);

    expect(
      await screen.findByText(
        "Restart with heartwood --interface web to start the selected Heartwood-managed model.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Task")).toBeDisabled();
  });

  it("keeps the composer unavailable when route validation fails", async () => {
    const client = new FakeClient();
    client.currentSettings = {
      ...settings(),
      active_profile: "heartwood",
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

  it("surfaces sessions that require persistence recovery", async () => {
    const client = new FakeClient();
    client.currentSessions = [
      {
        ...sessionSummary("session-recovery"),
        status: "recovery-required",
      },
    ];

    render(<App client={client} initialSessionId="session-recovery" />);

    expect(
      await screen.findByRole("button", {
        name: /Synthetic analysis, Recovery required/u,
      }),
    ).toBeInTheDocument();
  });

  it("supports secondary activity, settings, rejection, and pause controls", async () => {
    const client = new PendingClient();
    client.currentSettings = {
      ...settings(),
      active_profile: "heartwood",
      profiles: [localProfile(), customProfile()],
    };
    render(<App client={client} initialSessionId="session-test" />);

    fireEvent.click(await screen.findByLabelText("Reject 1 action"));
    await waitFor(() => expect(client.commands.at(-1)?.kind).toBe("deny"));
    act(() => {
      const current = client.projectionFor("session-test");
      client.emitStream({
        ...current,
        eventCount: current.eventCount + 1,
        revision: current.revision + 1,
        lifecycle: {
          status: "running",
          canPause: true,
          canResume: false,
          canSteer: true,
        },
        availableCommands: ["chat", "pause"],
      });
    });
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
      screen.getByRole("button", {
        name: /custom-loopback.*openai\/custom-model/u,
      }),
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
    expect(screen.queryByLabelText("Remove heartwood")).not.toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("Remove custom-loopback"));
    await waitFor(() =>
      expect(client.currentSettings.profiles).toHaveLength(1),
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
      active_profile: "heartwood",
      profiles: [localProfile()],
    };
    render(<App client={client} initialSessionId="session-test" />);

    const allow = await screen.findByLabelText("Allow 1 action once");
    const reject = screen.getByLabelText("Reject 1 action");
    expect(
      screen.getByText(
        "These actions were proposed together. Allowing runs every action once; rejecting runs none of them.",
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

  it("presents a multi-action proposal as one explicit decision", async () => {
    const client = new BatchPendingClient();
    client.currentSettings = {
      ...settings(),
      active_profile: "heartwood",
      profiles: [localProfile()],
    };
    render(<App client={client} initialSessionId="session-test" />);

    const heading = await screen.findByRole("heading", {
      name: "One Decision for This Action Set",
    });
    expect(heading).toBeVisible();
    expect(heading).toHaveFocus();
    expect(
      within(
        screen.getByRole("region", {
          name: "One Decision for This Action Set",
        }),
      ).getByRole("list"),
    ).toBeVisible();
    expect(screen.getAllByText("2 actions", { exact: true })).toHaveLength(2);
    expect(screen.getByText("Not Classified")).toBeVisible();
    expect(
      screen.getByRole("button", { name: "Allow 2 actions once" }),
    ).toBeVisible();

    const argumentDisclosure = screen.getAllByText("Review Exact Arguments")[0];
    if (!argumentDisclosure)
      throw new Error("action argument disclosure is missing");
    argumentDisclosure.focus();
    expect(argumentDisclosure).toHaveFocus();
    act(() => {
      const current = client.projectionFor("session-test");
      client.emitStream({
        ...current,
        streamRevision: current.streamRevision + 1,
        streamingText: "The action set is still awaiting review.",
      });
    });
    expect(argumentDisclosure).toHaveFocus();

    fireEvent.click(screen.getByLabelText("Open action review settings"));
    expect(
      await screen.findByText(
        "Resolve the pending action set before changing this setting.",
      ),
    ).toBeVisible();
    expect(
      screen.getByRole("radio", { name: /Review Every Action/u }),
    ).toBeDisabled();
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
  override replayEvents(sessionId: string): Promise<SessionProjectionResponse> {
    this.replayCalls += 1;
    const projection = {
      ...syntheticProjection(),
      sessionId,
    };
    this.projections.set(sessionId, projection);
    return Promise.resolve({
      events: syntheticEvents(),
      projection,
    });
  }
}

class BatchPendingClient extends PendingClient {
  override replayEvents(sessionId: string): Promise<SessionProjectionResponse> {
    this.replayCalls += 1;
    const projection = syntheticProjection({
      sessionId,
      eventCount: 7,
      revision: 6,
      pendingApproval: {
        groupId: "action-set-session-test",
        actions: [
          {
            targetId: "session-test-toolcall-0",
            toolName: "terminal",
            risk: "low",
            summary: "build the aggregate synthetic target-condition cohort",
            arguments: {
              command: "python run.py --output /project/cohort-summary.json",
            },
          },
          {
            targetId: "session-test-toolcall-1",
            toolName: "file_editor",
            risk: "unknown",
            summary: "Write the aggregate cohort summary",
            arguments: {
              command: "create",
              path: "/project/cohort-summary.md",
            },
          },
        ],
        decision: null,
        decisionScope: "all",
      },
    });
    this.projections.set(sessionId, projection);
    return Promise.resolve({
      events: syntheticEvents(),
      projection,
    });
  }
}

class FirstRunClient extends FakeClient {
  initialized = false;

  constructor() {
    super();
    this.currentSessions = [];
    this.currentReadiness = projectReadiness("setup-required");
  }

  override getStartupPlan(): Promise<StartupPlan> {
    const plan = startupPlan(this.currentReadiness);
    return Promise.resolve(
      this.initialized ? plan : (
        {
          ...plan,
          phase: "project-review",
          summary:
            "Review this project before Heartwood creates private project state.",
          next_action: "Confirm the project and choose a model connection.",
        }
      ),
    );
  }

  override initializeProject(): Promise<StartupPlan> {
    this.initialized = true;
    return super.initializeProject();
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
  override postCommand(): Promise<SessionProjectionResponse> {
    return Promise.reject(new Error("synthetic gateway failure"));
  }
}

class LostResponseClient extends FakeClient {
  override postCommand(
    command: SessionCommand,
  ): Promise<SessionProjectionResponse> {
    this.commands.push(command);
    if (this.commands.length === 1) {
      return Promise.reject(new Error("connection lost after submission"));
    }
    const projection = this.projectionFor(command.session_id);
    const prompt =
      typeof command.payload.prompt === "string" ? command.payload.prompt : "";
    const next = {
      ...projection,
      eventCount: projection.eventCount + 1,
      revision: projection.revision + 1,
      conversation: [
        ...projection.conversation,
        {
          id: `local-${command.command_id}`,
          sequence: projection.revision + 1,
          role: "user" as const,
          label: "You",
          content: prompt,
          detail: null,
          technicalDetail: null,
        },
      ],
    };
    this.projections.set(command.session_id, next);
    return Promise.resolve({ events: [], projection: next });
  }
}

describe("App error handling", () => {
  it("uses the gateway-owned command outcome for rejected commands", async () => {
    const client = new FakeClient();
    client.commandFailure = {
      code: "HW-AGENT-005",
      message: "HW-AGENT-005: The operation is unavailable.",
    };
    render(<App client={client} initialSessionId="session-test" />);

    await screen.findByRole("heading", { name: "Synthetic analysis" });
    fireEvent.click(screen.getByRole("button", { name: "Export audit" }));

    expect(
      await screen.findByText("HW-AGENT-005: The operation is unavailable."),
    ).toBeVisible();
    expect(client.auditExportCalls).toBe(0);
  });

  it("renders gateway command errors", async () => {
    render(
      <App client={new RejectingClient()} initialSessionId="session-test" />,
    );

    await screen.findByRole("heading", { name: "Synthetic analysis" });
    fireEvent.click(screen.getByRole("button", { name: "Export audit" }));

    expect(await screen.findByText("synthetic gateway failure")).toBeVisible();
  });

  it("retries an uncertain command with the original command identifier", async () => {
    const client = new LostResponseClient();
    client.currentSettings = {
      ...settings(),
      active_profile: "heartwood",
      profiles: [localProfile()],
    };
    render(<App client={client} initialSessionId="session-test" />);
    await waitFor(() => expect(screen.getByLabelText("Task")).toBeEnabled());

    fireEvent.change(screen.getByLabelText("Task"), {
      target: { value: "Inspect the synthetic cohort" },
    });
    fireEvent.click(screen.getByLabelText("Send task"));
    expect(
      await screen.findByText("connection lost after submission"),
    ).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: "Retry request" }));
    await waitFor(() => expect(client.commands).toHaveLength(2));

    expect(client.commands[1]).toEqual(client.commands[0]);
    expect(
      within(
        screen.getByRole("log", { name: "Conversation transcript" }),
      ).getAllByText("Inspect the synthetic cohort"),
    ).toHaveLength(1);
  });
});

const localProfile = (): ModelProfile => ({
  profile_id: "heartwood",
  model: "openai/local-model",
  policy_endpoint: "http://127.0.0.1:8765/v1/chat/completions",
  capability_tier: "supervised",
  base_url: "http://127.0.0.1:8765/v1",
  credential_kind: "none",
  auth_type: "api_key",
  subscription_vendor: null,
  api_key_env: null,
  api_key_file: null,
  api_version: null,
  aws_region_name: null,
  aws_profile_name: null,
  max_input_tokens: null,
  max_output_tokens: null,
  description: "Model runtime managed by Heartwood",
});

const profileResponseFromDraft = (
  profile: ModelProfileDraft,
): ModelProfile => ({
  profile_id: profile.profile_id,
  model: profile.model,
  policy_endpoint: profile.policy_endpoint,
  capability_tier: profile.capability_tier ?? "supervised",
  base_url: profile.base_url ?? null,
  credential_kind: profile.credential_kind ?? "environment",
  auth_type: profile.auth_type ?? "api_key",
  subscription_vendor: profile.subscription_vendor ?? null,
  api_key_env: profile.api_key_env ?? null,
  api_key_file: profile.api_key_file ?? null,
  api_version: profile.api_version ?? null,
  aws_region_name: profile.aws_region_name ?? null,
  aws_profile_name: profile.aws_profile_name ?? null,
  max_input_tokens: profile.max_input_tokens ?? null,
  max_output_tokens: profile.max_output_tokens ?? null,
  description: profile.description ?? null,
});

const customProfile = (): ModelProfile => ({
  ...localProfile(),
  profile_id: "custom-loopback",
  model: "openai/custom-model",
  description: "User-managed model service",
});

const modelConnection = (
  connectionId: string,
  label: string,
  source: ModelConnection["source"],
  credentialStatus: ModelConnection["credential_status"],
  acceptsToken: boolean,
): ModelConnection => {
  const group =
    connectionId === "heartwood" ? "heartwood-managed"
    : source === "platform" ? "research-environment"
    : source === "user" ? "compatible-service"
    : "hosted-provider";
  const groupLabel =
    group === "heartwood-managed" ? "Run with Heartwood"
    : group === "research-environment" ? "Institution-managed providers"
    : group === "compatible-service" ? "Other compatible services"
    : "Hosted providers";
  return {
    connection_id: connectionId,
    label,
    protocol:
      connectionId === "anthropic" ? "anthropic"
      : connectionId === "research-ai" ? "static"
      : connectionId === "openai-subscription" ? "subscription"
      : "openai-compatible",
    model_prefix: connectionId === "research-ai" ? "litellm_proxy/" : "openai/",
    source,
    credential_kind:
      connectionId === "heartwood" ? "none"
      : (
        connectionId === "research-ai" || connectionId === "openai-subscription"
      ) ?
        "managed-identity"
      : "environment",
    policy_endpoint:
      connectionId === "custom-api" ? null
      : connectionId === "openai-subscription" ?
        "https://chatgpt.com/backend-api/codex/responses"
      : "http://127.0.0.1:8765/v1/chat/completions",
    catalog_endpoint:
      connectionId === "custom-api" ? null : "http://127.0.0.1:8765/v1/models",
    base_url: connectionId === "heartwood" ? "http://127.0.0.1:8765/v1" : null,
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
    subscription_vendor:
      connectionId === "openai-subscription" ? "openai" : null,
    group,
    group_label: groupLabel,
    accepts_token: acceptsToken,
    supports_login: connectionId === "openai-subscription",
    auth_type:
      connectionId === "openai-subscription" ? "subscription" : "api_key",
    credential_status: credentialStatus,
  };
};

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

const startupPlan = (readiness: ProjectReadiness): StartupPlan => ({
  phase:
    readiness.state === "ready" ? "ready"
    : readiness.state === "compute-required" ? "compute-required"
    : readiness.state === "recovery-required" ? "recovery-required"
    : "connection-required",
  interface: "web",
  platform_id: readiness.platform_id,
  project_root: readiness.project_root,
  state_root: readiness.state_root,
  summary:
    readiness.state === "ready" ?
      "Heartwood is ready in the web interface."
    : "Choose where the model runs.",
  next_action:
    readiness.state === "ready" ?
      "Start or resume a session."
    : "Select a model connection in setup.",
  access_url: "http://127.0.0.1:8767/",
  requires_compute: readiness.state === "compute-required",
  requires_confirmation: false,
  interface_supported: true,
  readiness,
  capabilities: {
    platform_id: readiness.platform_id,
    display_name: "Workstation or container",
    interfaces: ["terminal", "web", "notebook"],
    browser_route: "direct",
    managed_runtimes: ["llama-cpp", "vllm"],
    scheduler: "none",
    persistent_storage: "The project directory",
    credential_backends: ["process", "keyring", "mounted-file"],
    model_sources: ["heartwood", "openai", "anthropic", "custom"],
    managed_model_connections: [],
    validation_level: "ci",
  },
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
