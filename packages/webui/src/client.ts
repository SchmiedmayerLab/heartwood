/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

import type {
  CommandKind,
  JsonValue,
  SessionCommand,
  SessionEvent,
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
      socket.onmessage = (message): void => {
        onEvents(parseEventPayload(String(message.data)).events);
      };
      socket.onerror = (): void => {
        socket.close();
        if (!closed) {
          cleanup = this.openSse(sessionId, afterSequence, onEvents);
        }
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
  const payload = (await response.json()) as Partial<SessionEventResponse> & {
    error?: string;
  };
  if (!response.ok) {
    throw new Error(
      payload.error ?? `Gateway request failed with status ${response.status}`,
    );
  }
  return { events: payload.events ?? [] };
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
