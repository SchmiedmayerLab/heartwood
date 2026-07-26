/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

import { sessionProjectionResponseSchema } from "./projectionSchema";
import type {
  ActionConfirmationMode,
  ActionSettings,
  AuditExport,
  CommandKind,
  CustomLocalModelDownloadRequest,
  CredentialSettings,
  JsonValue,
  LocalModelImportRequest,
  LocalModelImportResult,
  ModelCatalog,
  ModelCatalogRequest,
  ModelConnectRequest,
  ModelArtifacts,
  ModelDownload,
  ModelProfile,
  ModelRepositoryPlan,
  ModelRepositoryRequest,
  ModelSource,
  ModelSettings,
  ModelValidation,
  ProjectReadiness,
  StartupPlan,
  SessionCommand,
  SessionEvent,
  SessionList,
  SessionProjection,
  SessionSummary,
  SkillSettings,
  SkillSummary,
} from "./types";

const noopCleanup = (): void => undefined;

export interface SessionProjectionResponse {
  events: SessionEvent[];
  projection: SessionProjection;
}

export interface HeartwoodClient {
  getProjectReadiness(): Promise<ProjectReadiness>;
  getStartupPlan(): Promise<StartupPlan>;
  initializeProject(): Promise<StartupPlan>;
  listSessions(): Promise<SessionList>;
  ensureDefaultSession(): Promise<SessionSummary>;
  createSession(title?: string): Promise<SessionSummary>;
  getSession(sessionId: string): Promise<SessionSummary>;
  renameSession(sessionId: string, title: string): Promise<SessionSummary>;
  getAuditExport(sessionId: string): Promise<AuditExport>;
  postCommand(command: SessionCommand): Promise<SessionProjectionResponse>;
  replayEvents(
    sessionId: string,
    afterSequence?: number,
  ): Promise<SessionProjectionResponse>;
  streamSession(
    sessionId: string,
    afterSequence: number | undefined,
    onProjection: (projection: SessionProjection) => void,
    onError?: (error: Error) => void,
  ): () => void;
  getActionSettings(): Promise<ActionSettings>;
  selectActionConfirmationMode(
    mode: ActionConfirmationMode,
  ): Promise<ActionSettings>;
  getModelSettings(): Promise<ModelSettings>;
  forgetCredential(connectionId: string): Promise<CredentialSettings>;
  configureModelSource(sourceId: ModelSource): Promise<ModelSettings>;
  discoverModels(request: ModelCatalogRequest): Promise<ModelCatalog>;
  connectModel(request: ModelConnectRequest): Promise<ModelSettings>;
  saveModelProfile(profile: ModelProfile): Promise<ModelSettings>;
  selectModelProfile(profileId: string): Promise<ModelSettings>;
  removeModelProfile(profileId: string): Promise<ModelSettings>;
  validateModelProfile(profileId?: string): Promise<ModelValidation>;
  getModelArtifacts(): Promise<ModelArtifacts>;
  inspectModelRepository(
    request: ModelRepositoryRequest,
  ): Promise<ModelRepositoryPlan>;
  downloadLocalModel(modelId: string): Promise<ModelDownload>;
  downloadCustomLocalModel(
    request: CustomLocalModelDownloadRequest,
  ): Promise<ModelDownload>;
  importLocalModel(
    request: LocalModelImportRequest,
  ): Promise<LocalModelImportResult>;
  getSkillSettings(): Promise<SkillSettings>;
  inspectSkill(source: string): Promise<SkillSummary>;
  installSkill(source: string): Promise<SkillSettings>;
  removeSkill(name: string): Promise<SkillSettings>;
}

export const createCommand = (
  sessionId: string,
  kind: CommandKind,
  payload: Record<string, JsonValue> = {},
): SessionCommand => ({
  schema_version: "heartwood.session-command.v1",
  command_id: `${sessionId}-${kind}-${crypto.randomUUID().replaceAll("-", "")}`,
  session_id: sessionId,
  kind,
  actor_id: "human",
  created_at: new Date().toISOString().replace(/\.\d{3}Z$/, "Z"),
  payload,
});

export class GatewayClient implements HeartwoodClient {
  constructor(private readonly basePath = gatewayBasePath()) {}

  async getProjectReadiness(): Promise<ProjectReadiness> {
    return parseJsonResponse<ProjectReadiness>(
      await fetch(this.url("/project/readiness")),
    );
  }

  async getStartupPlan(): Promise<StartupPlan> {
    return parseJsonResponse<StartupPlan>(
      await fetch(this.url("/project/startup?interface=web")),
    );
  }

  async initializeProject(): Promise<StartupPlan> {
    return parseJsonResponse<StartupPlan>(
      await fetch(this.url("/project/initialize"), { method: "POST" }),
    );
  }

  async listSessions(): Promise<SessionList> {
    return parseJsonResponse<SessionList>(await fetch(this.url("/sessions")));
  }

  async ensureDefaultSession(): Promise<SessionSummary> {
    return parseJsonResponse<SessionSummary>(
      await fetch(this.url("/sessions/default"), { method: "POST" }),
    );
  }

  async createSession(title?: string): Promise<SessionSummary> {
    return parseJsonResponse<SessionSummary>(
      await fetch(this.url("/sessions"), {
        body: JSON.stringify(title === undefined ? {} : { title }),
        headers: { "Content-Type": "application/json" },
        method: "POST",
      }),
    );
  }

  async getSession(sessionId: string): Promise<SessionSummary> {
    return parseJsonResponse<SessionSummary>(
      await fetch(this.url(`/sessions/${encodeURIComponent(sessionId)}`)),
    );
  }

  async renameSession(
    sessionId: string,
    title: string,
  ): Promise<SessionSummary> {
    return parseJsonResponse<SessionSummary>(
      await fetch(this.url(`/sessions/${encodeURIComponent(sessionId)}`), {
        body: JSON.stringify({ title }),
        headers: { "Content-Type": "application/json" },
        method: "PATCH",
      }),
    );
  }

  async getAuditExport(sessionId: string): Promise<AuditExport> {
    return parseJsonResponse<AuditExport>(
      await fetch(
        this.url(`/sessions/${encodeURIComponent(sessionId)}/audit-export`),
      ),
    );
  }

  async postCommand(
    command: SessionCommand,
  ): Promise<SessionProjectionResponse> {
    const response = await fetch(
      this.url(`/sessions/${command.session_id}/commands`),
      {
        body: JSON.stringify(command),
        headers: { "Content-Type": "application/json" },
        method: "POST",
      },
    );
    return parseResponse(response);
  }

  async replayEvents(
    sessionId: string,
    afterSequence?: number,
  ): Promise<SessionProjectionResponse> {
    const query = afterSequence === undefined ? "" : `?after=${afterSequence}`;
    const response = await fetch(
      this.url(`/sessions/${sessionId}/events${query}`),
    );
    return parseResponse(response);
  }

  async getActionSettings(): Promise<ActionSettings> {
    return parseJsonResponse<ActionSettings>(
      await fetch(this.url("/settings/actions")),
    );
  }

  async selectActionConfirmationMode(
    mode: ActionConfirmationMode,
  ): Promise<ActionSettings> {
    return parseJsonResponse<ActionSettings>(
      await fetch(this.url("/settings/actions/confirmation"), {
        body: JSON.stringify({ mode }),
        headers: { "Content-Type": "application/json" },
        method: "PUT",
      }),
    );
  }

  async getModelSettings(): Promise<ModelSettings> {
    return parseJsonResponse<ModelSettings>(
      await fetch(this.url("/settings/models")),
    );
  }

  async forgetCredential(connectionId: string): Promise<CredentialSettings> {
    return parseJsonResponse<CredentialSettings>(
      await fetch(
        this.url(`/settings/credentials/${encodeURIComponent(connectionId)}`),
        { method: "DELETE" },
      ),
    );
  }

  async configureModelSource(sourceId: ModelSource): Promise<ModelSettings> {
    return parseJsonResponse<ModelSettings>(
      await fetch(this.url("/settings/models/source"), {
        body: JSON.stringify({ source_id: sourceId }),
        headers: { "Content-Type": "application/json" },
        method: "PUT",
      }),
    );
  }

  async discoverModels(request: ModelCatalogRequest): Promise<ModelCatalog> {
    return parseJsonResponse<ModelCatalog>(
      await fetch(this.url("/settings/models/catalog"), {
        body: JSON.stringify(request),
        headers: { "Content-Type": "application/json" },
        method: "POST",
      }),
    );
  }

  async connectModel(request: ModelConnectRequest): Promise<ModelSettings> {
    return parseJsonResponse<ModelSettings>(
      await fetch(this.url("/settings/models/connect"), {
        body: JSON.stringify(request),
        headers: { "Content-Type": "application/json" },
        method: "POST",
      }),
    );
  }

  async saveModelProfile(profile: ModelProfile): Promise<ModelSettings> {
    return parseJsonResponse<ModelSettings>(
      await fetch(this.url("/settings/models/profiles"), {
        body: JSON.stringify(profile),
        headers: { "Content-Type": "application/json" },
        method: "POST",
      }),
    );
  }

  async selectModelProfile(profileId: string): Promise<ModelSettings> {
    return parseJsonResponse<ModelSettings>(
      await fetch(this.url("/settings/models/active"), {
        body: JSON.stringify({ profile_id: profileId }),
        headers: { "Content-Type": "application/json" },
        method: "PUT",
      }),
    );
  }

  async removeModelProfile(profileId: string): Promise<ModelSettings> {
    return parseJsonResponse<ModelSettings>(
      await fetch(
        this.url(`/settings/models/profiles/${encodeURIComponent(profileId)}`),
        { method: "DELETE" },
      ),
    );
  }

  async validateModelProfile(profileId?: string): Promise<ModelValidation> {
    const query =
      profileId === undefined ? "" : (
        `?profile_id=${encodeURIComponent(profileId)}`
      );
    return parseJsonResponse<ModelValidation>(
      await fetch(this.url(`/settings/models/validation${query}`)),
    );
  }

  async getModelArtifacts(): Promise<ModelArtifacts> {
    return parseJsonResponse<ModelArtifacts>(
      await fetch(this.url("/settings/models/artifacts")),
    );
  }

  async inspectModelRepository(
    request: ModelRepositoryRequest,
  ): Promise<ModelRepositoryPlan> {
    return parseJsonResponse<ModelRepositoryPlan>(
      await fetch(this.url("/settings/models/repository"), {
        body: JSON.stringify(request),
        headers: { "Content-Type": "application/json" },
        method: "POST",
      }),
    );
  }

  async downloadLocalModel(modelId: string): Promise<ModelDownload> {
    return parseJsonResponse<ModelDownload>(
      await fetch(this.url("/settings/models/downloads"), {
        body: JSON.stringify({ model_id: modelId }),
        headers: { "Content-Type": "application/json" },
        method: "POST",
      }),
    );
  }

  async downloadCustomLocalModel(
    request: CustomLocalModelDownloadRequest,
  ): Promise<ModelDownload> {
    return parseJsonResponse<ModelDownload>(
      await fetch(this.url("/settings/models/downloads/custom"), {
        body: JSON.stringify(request),
        headers: { "Content-Type": "application/json" },
        method: "POST",
      }),
    );
  }

  async importLocalModel(
    request: LocalModelImportRequest,
  ): Promise<LocalModelImportResult> {
    return parseJsonResponse<LocalModelImportResult>(
      await fetch(this.url("/settings/models/imports"), {
        body: JSON.stringify(request),
        headers: { "Content-Type": "application/json" },
        method: "POST",
      }),
    );
  }

  async getSkillSettings(): Promise<SkillSettings> {
    return parseJsonResponse<SkillSettings>(
      await fetch(this.url("/settings/skills")),
    );
  }

  async inspectSkill(source: string): Promise<SkillSummary> {
    return parseJsonResponse<SkillSummary>(
      await fetch(this.url("/settings/skills/inspect"), {
        body: JSON.stringify({ source }),
        headers: { "Content-Type": "application/json" },
        method: "POST",
      }),
    );
  }

  async installSkill(source: string): Promise<SkillSettings> {
    return parseJsonResponse<SkillSettings>(
      await fetch(this.url("/settings/skills/install"), {
        body: JSON.stringify({ approved: true, source }),
        headers: { "Content-Type": "application/json" },
        method: "POST",
      }),
    );
  }

  async removeSkill(name: string): Promise<SkillSettings> {
    return parseJsonResponse<SkillSettings>(
      await fetch(this.url(`/settings/skills/${encodeURIComponent(name)}`), {
        method: "DELETE",
      }),
    );
  }

  streamSession(
    sessionId: string,
    afterSequence: number | undefined,
    onProjection: (projection: SessionProjection) => void,
    onError: (error: Error) => void = noopCleanup,
  ): () => void {
    const query = afterSequence === undefined ? "" : `?after=${afterSequence}`;
    const path = `/sessions/${sessionId}/events${query}`;
    let closed = false;
    let cleanup = (): void => {
      closed = true;
    };
    if ("WebSocket" in window) {
      const socket = new WebSocket(this.websocketUrl(path));
      let fallbackOpen = false;
      const openFallback = (): void => {
        if (closed || fallbackOpen) {
          return;
        }
        fallbackOpen = true;
        cleanup = this.openSse(
          sessionId,
          afterSequence,
          onProjection,
          onError,
        );
      };
      socket.onmessage = (message): void => {
        try {
          onProjection(parseProjectionPayload(String(message.data)).projection);
        } catch (caught) {
          onError(asError(caught));
          socket.close(1002, "invalid Heartwood projection");
          openFallback();
        }
      };
      socket.onclose = (event): void => {
        if (event.code !== 1000) {
          openFallback();
        }
      };
      socket.onerror = (): void => {
        socket.close();
        openFallback();
      };
      cleanup = (): void => {
        closed = true;
        socket.close();
      };
      return (): void => {
        closed = true;
        cleanup();
      };
    }
    cleanup = this.openSse(sessionId, afterSequence, onProjection, onError);
    return (): void => {
      closed = true;
      cleanup();
    };
  }

  private openSse(
    sessionId: string,
    afterSequence: number | undefined,
    onProjection: (projection: SessionProjection) => void,
    onError: (error: Error) => void,
  ): () => void {
    if (!("EventSource" in window)) {
      return noopCleanup;
    }
    const query = afterSequence === undefined ? "" : `?after=${afterSequence}`;
    const source = new EventSource(
      this.url(`/sessions/${sessionId}/events/stream${query}`),
    );
    source.addEventListener("heartwood-session-events", (message): void => {
      try {
        onProjection(
          parseProjectionPayload((message as MessageEvent<string>).data)
            .projection,
        );
      } catch (caught) {
        source.close();
        onError(asError(caught));
      }
    });
    return (): void => {
      source.close();
    };
  }

  private url(path: string): string {
    return joinPath(this.basePath, path);
  }

  private websocketUrl(path: string): string {
    const url = new URL(this.url(path), window.location.href);
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    return url.toString();
  }
}

const parseResponse = async (
  response: Response,
): Promise<SessionProjectionResponse> => {
  if (!response.ok) {
    let error = `Gateway request failed with status ${response.status}`;
    try {
      const payload = (await response.json()) as { error?: string };
      error = payload.error ?? error;
    } catch {
      // Preserve the gateway status when an upstream proxy returns HTML/text.
    }
    throw new Error(error);
  }
  return parseProjectionPayload(await response.text());
};

const asError = (value: unknown): Error =>
  value instanceof Error ? value : new Error(String(value));

const parseJsonResponse = async <Value>(response: Response): Promise<Value> => {
  if (!response.ok) {
    let error = `Gateway request failed with status ${response.status}`;
    try {
      const payload = (await response.json()) as { error?: string };
      error = payload.error ?? error;
    } catch {
      // Preserve the gateway status when an upstream proxy returns HTML/text.
    }
    throw new Error(error);
  }
  return (await response.json()) as Value;
};

const parseProjectionPayload = (payload: string): SessionProjectionResponse => {
  const parsed = sessionProjectionResponseSchema.safeParse(JSON.parse(payload));
  if (!parsed.success) {
    throw new Error("Gateway response included an invalid session projection");
  }
  return parsed.data;
};

const gatewayBasePath = (): string => {
  const env = import.meta.env as unknown;
  if (typeof env !== "object" || env === null) {
    return inferGatewayBasePath();
  }
  const value = (env as { VITE_HEARTWOOD_GATEWAY_BASE?: unknown })
    .VITE_HEARTWOOD_GATEWAY_BASE;
  if (typeof value === "string" && value !== "") {
    return value;
  }
  return inferGatewayBasePath();
};

const inferGatewayBasePath = (): string => {
  if (typeof window === "undefined") {
    return "";
  }
  const match = /^(.*\/proxy\/[^/]+)(?:\/.*)?$/.exec(window.location.pathname);
  return match?.[1] ?? "";
};

const joinPath = (basePath: string, path: string): string => {
  const base = basePath.endsWith("/") ? basePath.slice(0, -1) : basePath;
  return `${base}${path}`;
};
