/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

import type {
  ActionConfirmationMode,
  ActionSettings,
  CommandKind,
  JsonValue,
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

const noopCleanup = (): void => undefined;

export interface SessionEventResponse {
  events: SessionEvent[];
}

export interface HeartwoodClient {
  postCommand(command: SessionCommand): Promise<SessionEventResponse>;
  replayEvents(
    sessionId: string,
    afterSequence?: number,
  ): Promise<SessionEventResponse>;
  streamEvents(
    sessionId: string,
    afterSequence: number | undefined,
    onEvents: (events: SessionEvent[]) => void,
  ): () => void;
  getActionSettings(): Promise<ActionSettings>;
  selectActionConfirmationMode(
    mode: ActionConfirmationMode,
  ): Promise<ActionSettings>;
  getModelSettings(): Promise<ModelSettings>;
  saveModelProfile(profile: ModelProfile): Promise<ModelSettings>;
  selectModelProfile(profileId: string): Promise<ModelSettings>;
  removeModelProfile(profileId: string): Promise<ModelSettings>;
  validateModelProfile(profileId?: string): Promise<ModelValidation>;
  getModelArtifacts(): Promise<ModelArtifacts>;
  downloadModelArtifact(artifactId: string): Promise<ModelDownload>;
  getSkillSettings(): Promise<SkillSettings>;
  inspectSkill(source: string): Promise<SkillSummary>;
  installSkill(source: string): Promise<SkillSettings>;
  removeSkill(name: string): Promise<SkillSettings>;
}

export const createCommand = (
  sessionId: string,
  kind: CommandKind,
  sequence: number,
  payload: Record<string, JsonValue> = {},
): SessionCommand => ({
  schema_version: "heartwood.session-command.v1",
  command_id: `${sessionId}-${kind}-${String(sequence).padStart(6, "0")}`,
  session_id: sessionId,
  kind,
  actor_id: "human",
  created_at: new Date().toISOString().replace(/\.\d{3}Z$/, "Z"),
  payload,
});

export class GatewayClient implements HeartwoodClient {
  constructor(private readonly basePath = gatewayBasePath()) {}

  async postCommand(command: SessionCommand): Promise<SessionEventResponse> {
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
  ): Promise<SessionEventResponse> {
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

  async downloadModelArtifact(artifactId: string): Promise<ModelDownload> {
    return parseJsonResponse<ModelDownload>(
      await fetch(this.url("/settings/models/downloads"), {
        body: JSON.stringify({ artifact_id: artifactId }),
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

  streamEvents(
    sessionId: string,
    afterSequence: number | undefined,
    onEvents: (events: SessionEvent[]) => void,
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
        cleanup = this.openSse(sessionId, afterSequence, onEvents);
      };
      socket.onmessage = (message): void => {
        onEvents(parseEventPayload(String(message.data)).events);
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
    cleanup = this.openSse(sessionId, afterSequence, onEvents);
    return (): void => {
      closed = true;
      cleanup();
    };
  }

  private openSse(
    sessionId: string,
    afterSequence: number | undefined,
    onEvents: (events: SessionEvent[]) => void,
  ): () => void {
    if (!("EventSource" in window)) {
      return noopCleanup;
    }
    const query = afterSequence === undefined ? "" : `?after=${afterSequence}`;
    const source = new EventSource(
      this.url(`/sessions/${sessionId}/events/stream${query}`),
    );
    source.addEventListener("heartwood-session-events", (message): void => {
      onEvents(
        parseEventPayload((message as MessageEvent<string>).data).events,
      );
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
): Promise<SessionEventResponse> => {
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
  const payload = (await response.json()) as Partial<SessionEventResponse> & {
    error?: string;
  };
  return { events: payload.events ?? [] };
};

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

const parseEventPayload = (payload: string): SessionEventResponse => {
  const parsed = JSON.parse(payload) as Partial<SessionEventResponse>;
  return { events: parsed.events ?? [] };
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
  const match = /^(.*?\/proxy\/[^/]+)(?:\/.*)?$/.exec(window.location.pathname);
  return match?.[1] ?? "";
};

const joinPath = (basePath: string, path: string): string => {
  const base = basePath.endsWith("/") ? basePath.slice(0, -1) : basePath;
  return `${base}${path}`;
};
