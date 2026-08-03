/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

import { sessionProjectionResponseSchema } from "./projectionSchema";
import type {
  ActionConfirmationRequest,
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
  ModelDownloadRequest,
  ModelProfileDraft,
  ModelRepositoryPlan,
  ModelRepositoryRequest,
  ModelSelectionRequest,
  ModelSource,
  ModelSourceRequest,
  ModelSettings,
  ModelTransfer,
  ModelTransferExportRequest,
  ModelTransferImportRequest,
  ModelTransferInspectRequest,
  ModelTransferPlan,
  ModelValidation,
  ProjectReadiness,
  SessionCreateRequest,
  StartupPlan,
  SessionCommand,
  SessionEvent,
  SessionList,
  SessionProjection,
  SessionRenameRequest,
  SessionSummary,
  SkillInspectRequest,
  SkillInstallRequest,
  SkillSettings,
  SkillSummary,
  SpecialistSettings,
  SubscriptionDeviceLoginRequest,
  SubscriptionDeviceLogin,
  SubscriptionDevicePollRequest,
  WorkspaceChanges,
  WorkspaceDiff,
  WorkspaceFile,
  WorkspaceTree,
} from "./types";

const noopCleanup = (): void => undefined;

export type SessionStreamState =
  "connecting" | "connected" | "reconnecting" | "degraded";

export interface SessionStreamObserver {
  onProjection: (projection: SessionProjection) => void;
  onState?: (state: SessionStreamState) => void;
  onError?: (error: Error) => void;
}

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
  getWorkspaceTree(
    sessionId: string,
    path?: string,
    depth?: number,
  ): Promise<WorkspaceTree>;
  getWorkspaceFile(sessionId: string, path: string): Promise<WorkspaceFile>;
  getWorkspaceChanges(sessionId: string): Promise<WorkspaceChanges>;
  getWorkspaceDiff(sessionId: string, path: string): Promise<WorkspaceDiff>;
  postCommand(command: SessionCommand): Promise<SessionProjectionResponse>;
  replayEvents(
    sessionId: string,
    afterSequence?: number,
  ): Promise<SessionProjectionResponse>;
  streamSession(
    sessionId: string,
    afterSequence: number | undefined,
    observer: SessionStreamObserver,
  ): () => void;
  getActionSettings(): Promise<ActionSettings>;
  selectActionConfirmationMode(
    mode: ActionConfirmationMode,
  ): Promise<ActionSettings>;
  getModelSettings(): Promise<ModelSettings>;
  forgetCredential(connectionId: string): Promise<CredentialSettings>;
  startSubscriptionDeviceLogin(
    connectionId: string,
  ): Promise<SubscriptionDeviceLogin>;
  pollSubscriptionDeviceLogin(
    connectionId: string,
    loginId: string,
  ): Promise<SubscriptionDeviceLogin>;
  configureModelSource(sourceId: ModelSource): Promise<ModelSettings>;
  discoverModels(request: ModelCatalogRequest): Promise<ModelCatalog>;
  connectModel(request: ModelConnectRequest): Promise<ModelSettings>;
  saveModelProfile(profile: ModelProfileDraft): Promise<ModelSettings>;
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
  inspectModelBundle(
    request: ModelTransferInspectRequest,
  ): Promise<ModelTransferPlan>;
  exportLocalModel(request: ModelTransferExportRequest): Promise<ModelTransfer>;
  importModelBundle(
    request: ModelTransferImportRequest,
  ): Promise<ModelTransfer>;
  cancelModelTransfer(transferId: string): Promise<ModelTransfer>;
  getSkillSettings(): Promise<SkillSettings>;
  getSpecialistSettings(): Promise<SpecialistSettings>;
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
    const request: SessionCreateRequest = title === undefined ? {} : { title };
    return parseJsonResponse<SessionSummary>(
      await fetch(this.url("/sessions"), {
        body: JSON.stringify(request),
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
    const request: SessionRenameRequest = { title };
    return parseJsonResponse<SessionSummary>(
      await fetch(this.url(`/sessions/${encodeURIComponent(sessionId)}`), {
        body: JSON.stringify(request),
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

  async getWorkspaceTree(
    sessionId: string,
    path = ".",
    depth?: number,
  ): Promise<WorkspaceTree> {
    const query = new URLSearchParams({ path });
    if (depth !== undefined) query.set("depth", String(depth));
    return parseJsonResponse<WorkspaceTree>(
      await fetch(
        this.url(
          `/sessions/${encodeURIComponent(sessionId)}/workspace/tree?${query.toString()}`,
        ),
      ),
    );
  }

  async getWorkspaceFile(
    sessionId: string,
    path: string,
  ): Promise<WorkspaceFile> {
    const query = new URLSearchParams({ path });
    return parseJsonResponse<WorkspaceFile>(
      await fetch(
        this.url(
          `/sessions/${encodeURIComponent(sessionId)}/workspace/file?${query.toString()}`,
        ),
      ),
    );
  }

  async getWorkspaceChanges(sessionId: string): Promise<WorkspaceChanges> {
    return parseJsonResponse<WorkspaceChanges>(
      await fetch(
        this.url(
          `/sessions/${encodeURIComponent(sessionId)}/workspace/changes`,
        ),
      ),
    );
  }

  async getWorkspaceDiff(
    sessionId: string,
    path: string,
  ): Promise<WorkspaceDiff> {
    const query = new URLSearchParams({ path });
    return parseJsonResponse<WorkspaceDiff>(
      await fetch(
        this.url(
          `/sessions/${encodeURIComponent(sessionId)}/workspace/diff?${query.toString()}`,
        ),
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
    const request: ActionConfirmationRequest = { mode };
    return parseJsonResponse<ActionSettings>(
      await fetch(this.url("/settings/actions/confirmation"), {
        body: JSON.stringify(request),
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

  async startSubscriptionDeviceLogin(
    connectionId: string,
  ): Promise<SubscriptionDeviceLogin> {
    const request: SubscriptionDeviceLoginRequest = {
      connection_id: connectionId,
      terms_accepted: true,
    };
    return parseJsonResponse<SubscriptionDeviceLogin>(
      await fetch(this.url("/settings/models/subscription/device"), {
        body: JSON.stringify(request),
        headers: { "Content-Type": "application/json" },
        method: "POST",
      }),
    );
  }

  async pollSubscriptionDeviceLogin(
    connectionId: string,
    loginId: string,
  ): Promise<SubscriptionDeviceLogin> {
    const request: SubscriptionDevicePollRequest = {
      connection_id: connectionId,
      login_id: loginId,
    };
    return parseJsonResponse<SubscriptionDeviceLogin>(
      await fetch(this.url("/settings/models/subscription/device/poll"), {
        body: JSON.stringify(request),
        headers: { "Content-Type": "application/json" },
        method: "POST",
      }),
    );
  }

  async configureModelSource(sourceId: ModelSource): Promise<ModelSettings> {
    const request: ModelSourceRequest = { source_id: sourceId };
    return parseJsonResponse<ModelSettings>(
      await fetch(this.url("/settings/models/source"), {
        body: JSON.stringify(request),
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

  async saveModelProfile(profile: ModelProfileDraft): Promise<ModelSettings> {
    return parseJsonResponse<ModelSettings>(
      await fetch(this.url("/settings/models/profiles"), {
        body: JSON.stringify(profile),
        headers: { "Content-Type": "application/json" },
        method: "POST",
      }),
    );
  }

  async selectModelProfile(profileId: string): Promise<ModelSettings> {
    const request: ModelSelectionRequest = { profile_id: profileId };
    return parseJsonResponse<ModelSettings>(
      await fetch(this.url("/settings/models/active"), {
        body: JSON.stringify(request),
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
    const request: ModelDownloadRequest = { model_id: modelId };
    return parseJsonResponse<ModelDownload>(
      await fetch(this.url("/settings/models/downloads"), {
        body: JSON.stringify(request),
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

  async inspectModelBundle(
    request: ModelTransferInspectRequest,
  ): Promise<ModelTransferPlan> {
    return parseJsonResponse<ModelTransferPlan>(
      await fetch(this.url("/settings/models/transfers/inspect"), {
        body: JSON.stringify(request),
        headers: { "Content-Type": "application/json" },
        method: "POST",
      }),
    );
  }

  async exportLocalModel(
    request: ModelTransferExportRequest,
  ): Promise<ModelTransfer> {
    return parseJsonResponse<ModelTransfer>(
      await fetch(this.url("/settings/models/transfers/exports"), {
        body: JSON.stringify(request),
        headers: { "Content-Type": "application/json" },
        method: "POST",
      }),
    );
  }

  async importModelBundle(
    request: ModelTransferImportRequest,
  ): Promise<ModelTransfer> {
    return parseJsonResponse<ModelTransfer>(
      await fetch(this.url("/settings/models/transfers/imports"), {
        body: JSON.stringify(request),
        headers: { "Content-Type": "application/json" },
        method: "POST",
      }),
    );
  }

  async cancelModelTransfer(transferId: string): Promise<ModelTransfer> {
    return parseJsonResponse<ModelTransfer>(
      await fetch(
        this.url(
          `/settings/models/transfers/${encodeURIComponent(transferId)}`,
        ),
        { method: "DELETE" },
      ),
    );
  }

  async getSkillSettings(): Promise<SkillSettings> {
    return parseJsonResponse<SkillSettings>(
      await fetch(this.url("/settings/skills")),
    );
  }

  async getSpecialistSettings(): Promise<SpecialistSettings> {
    return parseJsonResponse<SpecialistSettings>(
      await fetch(this.url("/settings/specialists")),
    );
  }

  async inspectSkill(source: string): Promise<SkillSummary> {
    const request: SkillInspectRequest = { source };
    return parseJsonResponse<SkillSummary>(
      await fetch(this.url("/settings/skills/inspect"), {
        body: JSON.stringify(request),
        headers: { "Content-Type": "application/json" },
        method: "POST",
      }),
    );
  }

  async installSkill(source: string): Promise<SkillSettings> {
    const request: SkillInstallRequest = { approved: true, source };
    return parseJsonResponse<SkillSettings>(
      await fetch(this.url("/settings/skills/install"), {
        body: JSON.stringify(request),
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
    observer: SessionStreamObserver,
  ): () => void {
    const onError = observer.onError ?? noopCleanup;
    const onState = observer.onState ?? noopCleanup;
    const query = afterSequence === undefined ? "" : `?after=${afterSequence}`;
    const path = `/sessions/${sessionId}/events${query}`;
    let closed = false;
    let cleanup = (): void => {
      closed = true;
    };
    onState("connecting");
    if ("WebSocket" in window) {
      const socket = new WebSocket(this.websocketUrl(path));
      let fallbackOpen = false;
      const openFallback = (): void => {
        if (closed || fallbackOpen) {
          return;
        }
        fallbackOpen = true;
        onState("reconnecting");
        cleanup = this.openSse(sessionId, afterSequence, observer);
      };
      socket.onopen = (): void => onState("connected");
      socket.onmessage = (message): void => {
        let payload: SessionProjectionResponse;
        try {
          payload = parseProjectionPayload(String(message.data));
        } catch (caught) {
          onError(asError(caught));
          socket.close(1002, "invalid Heartwood projection");
          openFallback();
          return;
        }
        onState("connected");
        observer.onProjection(payload.projection);
      };
      socket.onclose = (): void => openFallback();
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
    cleanup = this.openSse(sessionId, afterSequence, observer);
    return (): void => {
      closed = true;
      cleanup();
    };
  }

  private openSse(
    sessionId: string,
    afterSequence: number | undefined,
    observer: SessionStreamObserver,
  ): () => void {
    const onError = observer.onError ?? noopCleanup;
    const onState = observer.onState ?? noopCleanup;
    if (!("EventSource" in window)) {
      onState("degraded");
      onError(
        new Error(
          "Live session updates are unavailable in this browser. Reload the session to check for updates.",
        ),
      );
      return noopCleanup;
    }
    const query = afterSequence === undefined ? "" : `?after=${afterSequence}`;
    const source = new EventSource(
      this.url(`/sessions/${sessionId}/events/stream${query}`),
    );
    source.onopen = (): void => onState("connected");
    source.onerror = (): void => {
      if (source.readyState === EventSource.CLOSED) {
        onState("degraded");
        onError(
          new Error(
            "Live session updates stopped. Reload the session to reconnect.",
          ),
        );
      } else {
        onState("reconnecting");
      }
    };
    source.addEventListener("heartwood-session-events", (message): void => {
      let payload: SessionProjectionResponse;
      try {
        payload = parseProjectionPayload(
          (message as MessageEvent<string>).data,
        );
      } catch (caught) {
        source.close();
        onState("degraded");
        onError(asError(caught));
        return;
      }
      onState("connected");
      observer.onProjection(payload.projection);
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
  let decoded: unknown;
  try {
    decoded = JSON.parse(payload) as unknown;
  } catch {
    throw new Error(
      "Heartwood could not read the session update. Refresh the page to reconnect.",
    );
  }
  const parsed = sessionProjectionResponseSchema.safeParse(decoded);
  if (!parsed.success) {
    throw new Error("Gateway response included an invalid session projection");
  }
  return parsed.data;
};

const gatewayBasePath = (): string => {
  const env = import.meta.env as unknown;
  if (typeof env !== "object" || env === null) {
    return gatewayBaseFromDocument();
  }
  const value = (env as { VITE_HEARTWOOD_GATEWAY_BASE?: unknown })
    .VITE_HEARTWOOD_GATEWAY_BASE;
  if (typeof value === "string" && value !== "") {
    return value;
  }
  return gatewayBaseFromDocument();
};

const gatewayBaseFromDocument = (): string => {
  if (typeof document === "undefined") {
    return "";
  }
  const value = document
    .querySelector<HTMLMetaElement>('meta[name="heartwood-gateway-base"]')
    ?.content.trim();
  if (value === undefined || value === "" || value === "/") {
    return "";
  }
  return value.startsWith("/") && !value.endsWith("/") ? value : "";
};

const joinPath = (basePath: string, path: string): string => {
  const base = basePath.endsWith("/") ? basePath.slice(0, -1) : basePath;
  return `${base}${path}`;
};
